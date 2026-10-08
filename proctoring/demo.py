"""Explicit simulations. These are not measurements of camera or CV model performance."""
from threading import Event
from time import perf_counter as monotonic, sleep
from .controller import Controller, SessionBlocked
from .storage import SessionStore
from .types import Analysis, Policy, State


class DemoControls:
    def __init__(self):
        self.camera_failed = Event()
        self.cv_hung = Event()
        self.focus_lost = Event()
        self.phone = Event()


class DemoSource:
    def __init__(self, controls):
        self.controls = controls

    def read(self):
        sleep(1 / 30)
        return (False, None) if self.controls.camera_failed.is_set() else (True, 'SIMULATED')

    def release(self):
        pass


class DemoAnalyzer:
    def __init__(self, controls, stop):
        self.controls, self.stop = controls, stop

    def analyze(self, frame):
        while self.controls.cv_hung.is_set() and not self.stop.is_set():
            self.stop.wait(.05)
        self.stop.wait(.04)
        return Analysis(frame.frame_id, frame.captured_at, monotonic(),
                        ('phone',) if self.controls.phone.is_set() else (),
                        description='SIMULATION: no camera and no models')

    def close(self):
        pass


def run_self_test(directory):
    """Deterministic end-to-end core scenario with actual SQLite persistence."""
    now = [100.0]
    with SessionStore(directory / 'session.sqlite3') as store:
        if store.metadata():
            raise ValueError('Self-test requires an empty data directory')
        controller = Controller(store, Policy(2, 2, 1), lambda: now[0])
        states = []

        def healthy(frame_id, phone=False):
            controller.camera(True, now[0])
            controller.focus(True, now[0])
            controller.result(Analysis(frame_id, now[0], now[0], ('phone',) if phone else ()))
            controller.tick()
            states.append(controller.state.value)

        healthy(1)
        controller.answer('q1', 'B')
        now[0] += .1
        controller.camera(False, now[0], 'Simulated unplug')
        controller.focus(False, now[0])
        controller.tick()
        states.append(controller.state.value)
        try:
            controller.answer('q1', 'must not be saved')
        except SessionBlocked:
            pass
        else:
            raise AssertionError('Paused session accepted an answer')
        now[0] += .1
        controller.camera(True, now[0])
        controller.result(Analysis(2, now[0], now[0]))
        controller.tick()
        states.append(controller.state.value)
        now[0] += .1
        healthy(3, phone=True)
        controller.submit()
        states.append(controller.state.value)
        if states != ['RUNNING', 'PAUSED', 'PAUSED', 'RUNNING', 'STOPPED']:
            raise AssertionError(states)
        if store.answers() != {'q1': 'B'}:
            raise AssertionError('Persisted answer changed during pause')
        store.export(directory / 'report.json')
    return {'mode': 'SIMULATION', 'states': states, 'report': str(directory / 'report.json')}
