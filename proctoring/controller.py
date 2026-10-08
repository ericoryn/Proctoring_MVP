"""Workers update memory only. The UI thread owns persistence and state transitions."""
import math
import sqlite3
from threading import RLock
from time import perf_counter as monotonic
from .types import Analysis, Policy, State


class SessionBlocked(RuntimeError):
    pass


class Controller:
    def __init__(self, store, policy: Policy, clock=monotonic):
        self.store, self.policy, self.clock = store, policy, clock
        self._lock = RLock()
        self._answers = store.answers()
        self.state = State.STOPPED if store.metadata().get('outcome') == 'submitted' else State.PAUSED
        self.reasons = ('initializing',)
        self._camera = (False, float('-inf'), 'Waiting for camera')
        self._focus = (None, float('-inf'), 'Waiting for focus monitor')
        self._analysis = None
        self._cv_error = ''
        self._invalid_before = float('-inf')
        self._pending = []
        self._active_detections = set()
        self._recorded_reasons = set()
        self._storage_failed = False
        self._overflow = False
        self._fatal_faults = {}
        self._persisted_state = None
        self._queue('session', 'opened', 'Health must be established for this run')

    @property
    def answers(self):
        with self._lock:
            return dict(self._answers)

    @property
    def latest_analysis(self):
        with self._lock:
            return self._analysis

    def _queue(self, kind, code, detail=''):
        if len(self._pending) >= 256:
            self._overflow = True  # Fail closed rather than silently lose audit events.
            return
        self._pending.append((self.clock(), kind, code, detail))

    def camera(self, ok, at, detail=''):
        with self._lock:
            if self.state == State.STOPPED:
                return
            if not ok and self._camera[0]:
                self._queue('technical', 'camera', detail)
            self._camera = (ok, at, detail)
            if not ok:
                self._invalid_before = max(self._invalid_before, at)

    def focus(self, ok, at, detail=''):
        """ok=None means monitoring error; False means an observed focus loss."""
        with self._lock:
            if self.state == State.STOPPED:
                return
            if ok is False and self._focus[0] is True:
                self._queue('violation', 'focus_lost', 'Target exam window is not foreground')
            if ok is None and self._focus[0] is not None:
                self._queue('technical', 'focus_monitor', detail)
            self._focus = (ok, at, detail)
            if ok is not True:
                self._invalid_before = max(self._invalid_before, at)

    def cv_error(self, detail):
        with self._lock:
            if self.state == State.STOPPED:
                return
            if not self._cv_error:
                self._queue('technical', 'cv_error', detail)
            self._cv_error = detail
            self._invalid_before = max(self._invalid_before, self.clock())

    def result(self, analysis: Analysis):
        with self._lock:
            if self.state == State.STOPPED:
                return False
            now = self.clock()
            if not (math.isfinite(analysis.captured_at) and math.isfinite(analysis.completed_at)
                    and 0 <= analysis.captured_at <= analysis.completed_at <= now
                    and analysis.frame_id > 0):
                return False
            old = self._analysis
            if old and (analysis.frame_id <= old.frame_id or analysis.captured_at <= old.captured_at
                        or analysis.completed_at < old.completed_at):
                return False
            self._analysis = analysis
            if analysis.captured_at <= self._invalid_before:
                return False
            self._cv_error = ''
            # Even obsolete observations may be useful evidence, but cannot restore health.
            current = set(analysis.violations)
            for code in sorted(current - self._active_detections):
                self._queue('violation', code, f'frame_id={analysis.frame_id}; {analysis.description}')
            self._active_detections = current
            return True

    def _health_reasons(self, now):
        reasons = []
        camera_ok, camera_at, _ = self._camera
        focus_ok, focus_at, _ = self._focus
        if not camera_ok or not 0 <= now - camera_at <= self.policy.monitor_timeout:
            reasons.append('camera')
        if focus_ok is None or not 0 <= now - focus_at <= self.policy.monitor_timeout:
            reasons.append('focus_monitor')
        elif not focus_ok:
            reasons.append('focus_lost')
        if self._cv_error:
            reasons.append('cv_error')
        result = self._analysis
        if result is None or now - result.completed_at > self.policy.analysis_timeout:
            reasons.append('cv_timeout')
        if result is not None and (now - result.captured_at > self.policy.max_frame_age
                                   or result.captured_at <= self._invalid_before):
            reasons.append('cv_stale')
        if self._overflow:
            reasons.append('audit_overflow')
        reasons.extend(self._fatal_faults)
        return reasons

    def storage_error(self, detail):
        with self._lock:
            if not self._storage_failed:
                self._queue('technical', 'storage', detail)
            self._storage_failed = True
            if self.state != State.STOPPED:
                self.state, self.reasons = State.PAUSED, ('storage',)

    def fatal_error(self, code, detail):
        """Unexpected UI/runtime errors require restart rather than a silent loss of control."""
        with self._lock:
            if code not in self._fatal_faults:
                self._queue('technical', code, detail)
                self._fatal_faults[code] = detail
            if self.state != State.STOPPED:
                self.state, self.reasons = State.PAUSED, (code,)

    def tick(self):
        with self._lock:
            if self.state == State.STOPPED:
                return self.state
            reasons = self._health_reasons(self.clock())
            new_state = State.PAUSED if reasons else State.RUNNING
            for reason in set(reasons) - self._recorded_reasons:
                already_queued = any(e[1] == 'technical' and e[2] == reason for e in self._pending)
                if reason != 'focus_lost' and not already_queued:
                    detail = self._cv_error if reason == 'cv_error' else ''
                    if reason == 'camera':
                        detail = self._camera[2]
                    elif reason == 'focus_monitor':
                        detail = self._focus[2]
                    self._queue('technical', reason, detail)
            self._recorded_reasons = set(reasons)
            if new_state != self._persisted_state or self._pending or self._storage_failed:
                events = list(self._pending)
                if new_state != self._persisted_state:
                    events.append((self.clock(), 'session', new_state.value, ', '.join(reasons)))
                try:
                    self.store.checkpoint(new_state.value, events)
                except (sqlite3.Error, OSError) as exc:
                    self.storage_error(str(exc))
                    self.state, self.reasons = State.PAUSED, tuple(reasons + ['storage'])
                    return self.state
                self._pending.clear()
                self._storage_failed = False
                self._persisted_state = new_state
            self.state, self.reasons = new_state, tuple(reasons)
            return self.state

    def _require_running(self):
        self.tick()
        if self.state != State.RUNNING:
            raise SessionBlocked('Session is not RUNNING: ' + ', '.join(self.reasons))

    def answer(self, question, value):
        if not isinstance(question, str) or not question or not isinstance(value, str):
            raise ValueError('Question and answer must be strings with a nonempty question ID')
        with self._lock:
            self._require_running()
            try:
                self.store.save_answer(question, value)
            except (sqlite3.Error, OSError) as exc:
                self.storage_error(str(exc))
                raise SessionBlocked('Answer was not saved') from exc
            self._answers[question] = value

    def submit(self):
        with self._lock:
            self._require_running()
            self.stop('submitted')

    def stop(self, outcome='interrupted'):
        with self._lock:
            if self.state == State.STOPPED:
                return
            try:
                self.store.checkpoint(State.STOPPED.value,
                                      self._pending + [(self.clock(), 'session', 'STOPPED', outcome)], outcome)
                self._pending.clear()
            except (sqlite3.Error, OSError) as exc:
                self.storage_error(str(exc))
                if outcome != 'submitted':
                    self.state = State.STOPPED
                raise SessionBlocked('Final journal write failed; submission was not confirmed') from exc
            self.state, self.reasons = State.STOPPED, ()
