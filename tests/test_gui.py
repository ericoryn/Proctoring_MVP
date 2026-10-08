"""Opt-in Tk integration test. No webcam and no global keyboard hooks."""
import os
from pathlib import Path
import tempfile
import time
import unittest
import sqlite3
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('PROCTORING_GUI_TEST') == '1', 'opt-in Windows/Tk GUI smoke test')
class GuiTests(unittest.TestCase):
    def test_render_failure_keeps_polling_and_latches_pause(self):
        import tkinter as tk
        from proctoring.app import ProctoringApp
        from proctoring.__main__ import parse_args
        from proctoring.types import State
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            app = ProctoringApp(root, parse_args(['--demo', '--data-dir', tmp]))
            try:
                before = len(root.tk.call('after', 'info'))
                with patch.object(app, '_render_state', side_effect=tk.TclError('widget unavailable')):
                    app._tick()
                self.assertGreater(len(root.tk.call('after', 'info')), before)
                self.assertEqual(app.controller.state, State.PAUSED)
                self.assertIn('ui_error', app.controller.reasons)
            finally:
                app.close()

    def test_demo_controls_pause_and_restore_saved_answer(self):
        import tkinter as tk
        from proctoring.app import ProctoringApp
        from proctoring.__main__ import parse_args
        from proctoring.types import State

        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            app = ProctoringApp(root, parse_args(['--demo', '--data-dir', tmp]))
            try:
                def pump_until(predicate, timeout=4):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        root.update()
                        if predicate():
                            return
                        time.sleep(.01)
                    self.fail('UI state did not arrive before deadline')

                pump_until(lambda: app.controller.state == State.RUNNING)
                app.variables['q1'].set('B')
                app._answer('q1')
                self.assertEqual(app.store.answers(), {'q1': 'B'})
                app.last_refresh = 0
                with patch.object(app.store, 'events', side_effect=sqlite3.OperationalError('disk I/O error')):
                    app._tick()
                    self.assertEqual(app.controller.state, State.PAUSED)
                    self.assertIn('disabled', app.submit_button.state())
                pump_until(lambda: app.controller.state == State.RUNNING)
                app.controls.focus_lost.set()
                pump_until(lambda: app.controller.state == State.PAUSED)
                self.assertIn('disabled', app.submit_button.state())
                app.variables['q1'].set('A')
                app._answer('q1')
                self.assertEqual(app.variables['q1'].get(), 'B')
                app.controls.focus_lost.clear()
                pump_until(lambda: app.controller.state == State.RUNNING)
                app._submit()
                self.assertEqual(app.store.metadata()['outcome'], 'submitted')
            finally:
                app.close()
            self.assertTrue((Path(tmp) / 'report.json').is_file())
            self.assertFalse(any(worker.is_alive() for worker in app.workers))
