import json
import tempfile
import threading
import unittest
from pathlib import Path

from proctoring.buffer import LatestFrameBuffer
from proctoring.controller import Controller, SessionBlocked
from proctoring.storage import SessionStore
from proctoring.types import Analysis, Frame, Policy, State


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.sqlite3'
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.c = Controller(self.store, Policy(2, 2, 1), self.clock)

    def healthy(self, frame_id=1, captured=None):
        now = self.clock()
        self.c.camera(True, now)
        self.c.focus(True, now)
        self.c.result(Analysis(frame_id, now if captured is None else captured, now))
        self.c.tick()

    def test_starts_paused_until_every_control_is_ready(self):
        self.assertEqual(self.c.state, State.PAUSED)
        with self.assertRaises(SessionBlocked):
            self.c.answer('q1', 'A')
        self.c.camera(True, 100)
        self.c.focus(True, 100)
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)
        self.healthy()
        self.assertEqual(self.c.state, State.RUNNING)

    def test_camera_failure_preserves_committed_answers_on_disk(self):
        self.healthy()
        self.c.answer('q1', 'Ответ')
        self.c.camera(False, self.clock(), 'Camera disconnected')
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)
        with SessionStore(self.path) as reopened:
            self.assertEqual(reopened.answers(), {'q1': 'Ответ'})
        events = self.store.events()
        self.assertTrue(any(e['kind'] == 'technical' and e['code'] == 'camera' for e in events))

    def test_health_is_checked_at_input_boundary_without_waiting_for_tick(self):
        self.healthy()
        self.c.answer('q1', 'A')
        self.clock.now += 3
        with self.assertRaises(SessionBlocked):
            self.c.answer('q1', 'B')
        with self.assertRaises(SessionBlocked):
            self.c.submit()
        self.assertEqual(self.store.answers(), {'q1': 'A'})

    def test_recent_completion_of_old_frame_never_restores_health(self):
        self.c.camera(True, 100)
        self.c.focus(True, 100)
        self.c.result(Analysis(1, 97, 100))
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)
        self.assertIn('cv_stale', self.c.reasons)

    def test_replayed_frame_cannot_refresh_health(self):
        self.healthy()
        self.clock.now = 101
        self.c.result(Analysis(1, 101, 101))
        self.clock.now = 102.1
        self.c.camera(True, self.clock())
        self.c.focus(True, self.clock())
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)

    def test_increasing_id_with_repeated_capture_time_cannot_refresh_health(self):
        self.healthy()
        self.clock.now = 101
        self.c.result(Analysis(2, 100, 101))
        self.clock.now = 102.1
        self.c.camera(True, self.clock())
        self.c.focus(True, self.clock())
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)

    def test_future_or_nonfinite_times_do_not_unpause(self):
        for timestamp in (101, float('nan'), float('inf')):
            with self.subTest(timestamp=timestamp):
                self.c.camera(True, 100)
                self.c.focus(True, 100)
                self.c.result(Analysis(1, timestamp, timestamp))
                self.c.tick()
                self.assertEqual(self.c.state, State.PAUSED)

    def test_recovery_requires_all_controls_and_new_analysis(self):
        self.healthy()
        self.clock.now = 100.1
        self.c.camera(False, self.clock())
        self.c.focus(False, self.clock())
        self.c.tick()
        self.clock.now = 100.2
        self.c.camera(True, self.clock())
        self.c.result(Analysis(2, 100.2, 100.2))
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)
        self.clock.now = 100.3
        self.c.focus(True, self.clock())
        self.c.result(Analysis(3, 100.3, 100.3))
        self.c.tick()
        self.assertEqual(self.c.state, State.RUNNING)

    def test_focus_monitor_silence_is_a_technical_failure(self):
        self.healthy()
        self.clock.now = 101.1
        self.c.camera(True, self.clock())
        self.c.result(Analysis(2, 101.1, 101.1))
        self.c.tick()
        self.assertIn('focus_monitor', self.c.reasons)

    def test_phone_event_is_distinct_from_technical_failure_and_deduplicated(self):
        self.healthy()
        for n in (2, 3):
            self.clock.now += .1
            self.c.result(Analysis(n, self.clock(), self.clock(), ('phone',)))
            self.c.tick()
        phone = [e for e in self.store.events() if e['code'] == 'phone']
        self.assertEqual(len(phone), 1)
        self.assertEqual(phone[0]['kind'], 'violation')
        self.assertEqual(self.c.state, State.RUNNING)

    def test_analysis_error_pauses_and_only_new_success_clears_it(self):
        self.healthy()
        self.c.cv_error('inference failed')
        self.c.tick()
        self.assertIn('cv_error', self.c.reasons)
        self.c.result(Analysis(1, 100, 100))
        self.c.tick()
        self.assertEqual(self.c.state, State.PAUSED)
        self.clock.now += .1
        self.healthy(2)
        self.assertEqual(self.c.state, State.RUNNING)

    def test_stopped_is_terminal_and_submission_is_persisted(self):
        self.healthy()
        self.c.answer('q1', 'B')
        self.c.submit()
        self.assertEqual(self.c.state, State.STOPPED)
        self.clock.now += .1
        self.healthy(2)
        self.assertEqual(self.c.state, State.STOPPED)
        self.assertEqual(self.store.metadata()['outcome'], 'submitted')

    def test_restart_preserves_answers_but_not_healthy_state(self):
        self.healthy()
        self.c.answer('q1', 'C')
        recovered = Controller(self.store, Policy(2, 2, 1), self.clock)
        self.assertEqual(recovered.state, State.PAUSED)
        self.assertEqual(recovered.answers, {'q1': 'C'})

    def test_write_failure_pauses_and_does_not_acknowledge_answer(self):
        self.healthy()
        self.store.connection.execute('PRAGMA query_only = ON')
        with self.assertRaises(SessionBlocked):
            self.c.answer('q1', 'lost')
        self.assertEqual(self.c.state, State.PAUSED)
        self.assertIn('storage', self.c.reasons)
        self.assertEqual(self.c.answers, {})

    def test_failed_submission_can_be_retried_after_storage_recovers(self):
        self.healthy()
        self.c.answer('q1', 'A')
        self.store.connection.execute('PRAGMA query_only = ON')
        with self.assertRaises(SessionBlocked):
            self.c.submit()
        self.assertEqual(self.c.state, State.PAUSED)
        self.assertNotEqual(self.store.metadata().get('outcome'), 'submitted')
        self.store.connection.execute('PRAGMA query_only = OFF')
        self.c.tick()
        self.c.submit()
        self.assertEqual(self.store.metadata()['outcome'], 'submitted')
        self.assertEqual(self.c.state, State.STOPPED)

    def test_recovered_storage_failure_remains_in_audit(self):
        self.healthy()
        self.store.connection.execute('PRAGMA query_only = ON')
        with self.assertRaises(SessionBlocked):
            self.c.answer('q1', 'lost')
        self.store.connection.execute('PRAGMA query_only = OFF')
        self.c.tick()
        self.assertTrue(any(e['kind'] == 'technical' and e['code'] == 'storage' for e in self.store.events()))

    def test_fault_recovered_between_ticks_remains_in_audit(self):
        self.healthy()
        self.clock.now += .1
        self.c.cv_error('transient inference failure')
        self.c.camera(False, self.clock(), 'transient unplug')
        self.clock.now += .1
        self.healthy(2)
        codes = {e['code'] for e in self.store.events() if e['kind'] == 'technical'}
        self.assertTrue({'camera', 'cv_error'} <= codes)

    def test_terminal_controller_ignores_late_worker_results(self):
        self.healthy()
        self.c.submit()
        self.clock.now += .1
        self.assertFalse(self.c.result(Analysis(2, self.clock(), self.clock(), ('phone',))))

    def test_single_fault_episode_has_one_technical_event(self):
        self.healthy()
        self.c.camera(False, self.clock(), 'unplug')
        self.c.cv_error('inference')
        self.c.focus(None, self.clock(), 'window API')
        self.c.tick()
        self.c.tick()
        for code in ('camera', 'cv_error', 'focus_monitor'):
            events = [e for e in self.store.events() if e['kind'] == 'technical' and e['code'] == code]
            self.assertEqual(len(events), 1, code)

    def test_export_contains_answers_and_typed_events(self):
        self.healthy()
        self.c.answer('q1', 'A')
        dest = Path(self.tmp.name) / 'report.json'
        self.store.export(dest)
        report = json.loads(dest.read_text(encoding='utf-8'))
        self.assertEqual(report['answers'], {'q1': 'A'})
        self.assertTrue(report['events'])

    def test_invalid_timeouts_rejected(self):
        for bad in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Policy(bad, 1, 1)


class BufferTests(unittest.TestCase):
    def test_latest_overwrites_old_frames(self):
        buf = LatestFrameBuffer()
        for i in range(1, 101):
            buf.publish(Frame(i, float(i), i))
        self.assertEqual(buf.wait_next(0, .1).frame_id, 100)
        self.assertIsNone(buf.wait_next(100, .001))

    def test_close_wakes_waiting_consumer(self):
        buf = LatestFrameBuffer()
        results = []
        thread = threading.Thread(target=lambda: results.append(buf.wait_next(0, 30)), daemon=True)
        thread.start()
        buf.close()
        thread.join(1)
        self.assertFalse(thread.is_alive(), 'close must wake the consumer')
        self.assertEqual(results, [None])

    def test_buffer_rejects_out_of_order_frames(self):
        buf = LatestFrameBuffer()
        buf.publish(Frame(2, 2, None))
        with self.assertRaises(ValueError):
            buf.publish(Frame(1, 1, None))


if __name__ == '__main__':
    unittest.main()
