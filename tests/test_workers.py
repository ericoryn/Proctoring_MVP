import threading
import time
import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from proctoring.buffer import LatestFrameBuffer
from proctoring.metrics import Metrics
from proctoring.system_lock import FocusMonitor, ScopedKeyboardHooks, should_block_key
from proctoring.types import Analysis, Frame
from proctoring.vision import AttentionTracker, observations, configure_local_runtime, gaze_direction
from proctoring.workers import CaptureWorker, CVWorker


class Sink:
    def __init__(self):
        self.results = []
        self.errors = []
        self.cameras = []
        self.focuses = []
        self.delivered = threading.Event()

    def result(self, value):
        self.results.append(value)
        self.delivered.set()

    def cv_error(self, value):
        self.errors.append(value)

    def camera(self, *args):
        self.cameras.append(args)

    def focus(self, *args):
        self.focuses.append(args)


class WorkerTests(unittest.TestCase):
    def test_capture_is_not_locked_during_inference_and_next_frame_is_latest(self):
        buf, stop, sink = LatestFrameBuffer(), threading.Event(), Sink()
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        consumed = []

        class Analyzer:
            def analyze(self, frame):
                consumed.append(frame.frame_id)
                if frame.frame_id == 1:
                    entered.set()
                    release.wait(2)
                else:
                    second.set()
                return Analysis(frame.frame_id, frame.captured_at, time.perf_counter())

            def close(self):
                pass

        worker = CVWorker(buf, Analyzer, sink, stop, Metrics())
        worker.start()
        self.addCleanup(lambda: (stop.set(), buf.close(), release.set(), worker.join(2)))
        buf.publish(Frame(1, time.perf_counter(), None))
        self.assertTrue(entered.wait(1))
        producer = threading.Thread(target=lambda: [buf.publish(Frame(n, time.perf_counter(), None))
                                                    for n in range(2, 101)], daemon=True)
        producer.start()
        producer.join(.5)
        self.assertFalse(producer.is_alive(), 'inference must not own the frame lock')
        release.set()
        self.assertTrue(second.wait(1))
        self.assertEqual(consumed[:2], [1, 100])

    def test_inference_failure_reports_error_and_next_frame_can_recover(self):
        buf, stop, sink = LatestFrameBuffer(), threading.Event(), Sink()
        failed = threading.Event()

        class Analyzer:
            def analyze(self, frame):
                if frame.frame_id == 1:
                    failed.set()
                    raise RuntimeError('model failure')
                return Analysis(frame.frame_id, frame.captured_at, time.perf_counter())

            def close(self):
                pass

        worker = CVWorker(buf, Analyzer, sink, stop, Metrics())
        worker.start()
        self.addCleanup(lambda: (stop.set(), buf.close(), worker.join(2)))
        buf.publish(Frame(1, time.perf_counter(), None))
        self.assertTrue(failed.wait(1))
        buf.publish(Frame(2, time.perf_counter(), None))
        self.assertTrue(sink.delivered.wait(1))
        self.assertTrue(sink.errors)
        self.assertEqual(sink.results[0].frame_id, 2)

    def test_camera_reopens_after_read_failure_and_releases_both_handles(self):
        stop, sink, buf = threading.Event(), Sink(), LatestFrameBuffer()
        released = []

        class Source:
            def __init__(self, good):
                self.good = good

            def read(self):
                if self.good:
                    stop.set()
                    return True, 'frame'
                return False, None

            def release(self):
                released.append(self.good)

        sources = iter([Source(False), Source(True)])
        worker = CaptureWorker(buf, lambda: next(sources), sink, stop, retry_delay=.001)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(released, [False, True])
        self.assertEqual([s[0] for s in sink.cameras], [False, True])
        self.assertEqual(buf.wait_next(0, .1).image, 'frame')

    def test_focus_exception_is_technical_and_monitor_recovers(self):
        stop, sink = threading.Event(), Sink()
        calls = []

        def probe():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('Window unavailable')
            stop.set()
            return True

        worker = FocusMonitor(probe, sink, stop, interval=.001)
        worker.start()
        worker.join(1)
        self.assertEqual([x[0] for x in sink.focuses], [None, True])


class VisionLogicTests(unittest.TestCase):
    def test_runtime_creates_local_config_before_importing_ultralytics(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            path = Path(tmp) / 'Модели' / 'yolo11n.pt'
            configure_local_runtime(path)
            self.assertTrue(Path(os.environ['YOLO_CONFIG_DIR']).is_dir())
            self.assertTrue(Path(os.environ['YOLO_CONFIG_DIR']).is_relative_to(Path(tmp)))
            self.assertEqual(os.environ['YOLO_OFFLINE'], 'true')

    def test_blink_does_not_count_as_downward_gaze(self):
        down = [SimpleNamespace(category_name=name, score=.8)
                for name in ('eyeLookDownLeft', 'eyeLookDownRight')]
        self.assertEqual(gaze_direction(down), 'down')
        blink = SimpleNamespace(category_name='eyeBlinkLeft', score=.9)
        self.assertEqual(gaze_direction(down + [blink]), 'center')

    def test_dwell_requires_continuous_deviation_and_resets_after_gap(self):
        tracker = AttentionTracker(dwell=1, max_gap=.5)
        self.assertFalse(tracker.update('left', 0))
        self.assertFalse(tracker.update('left', .4))
        self.assertFalse(tracker.update('left', .8))
        self.assertTrue(tracker.update('left', 1.1))
        self.assertFalse(tracker.update('center', 1.2))
        self.assertFalse(tracker.update('down', 2))
        self.assertFalse(tracker.update('down', 4))

    def test_phone_presence_and_multiple_faces_have_distinct_codes(self):
        self.assertEqual(observations(['cell phone'], 2), ('phone', 'multiple_faces'))
        self.assertEqual(observations([], 0), ('face_absent',))
        self.assertEqual(observations(['person'], 1), ())


class HookPolicyTests(unittest.TestCase):
    def test_probe_failure_caches_allowed_decision_until_key_up(self):
        failed = [True]
        def probe():
            if failed[0]:
                raise OSError('temporary window failure')
            return True
        hook = ScopedKeyboardHooks(probe, threading.Event())
        hook.modifiers.add('ctrl')
        event = SimpleNamespace(name='c', scan_code=46, event_type='down')
        self.assertTrue(hook._event(event))
        failed[0] = False
        self.assertTrue(hook._event(event))
        self.assertTrue(hook._event(SimpleNamespace(name='c', scan_code=46, event_type='up')))

    def test_suppressed_key_repeats_stay_paired_when_focus_moves(self):
        focused = [True]
        hook = ScopedKeyboardHooks(lambda: focused[0], threading.Event())
        hook._event(SimpleNamespace(name='ctrl', scan_code=29, event_type='down'))
        event = SimpleNamespace(name='c', scan_code=46, event_type='down')
        self.assertFalse(hook._event(event))
        focused[0] = False
        self.assertFalse(hook._event(event))
        self.assertFalse(hook._event(SimpleNamespace(name='c', scan_code=46, event_type='up')))

    def test_allowed_key_repeats_stay_paired_when_focus_enters_target(self):
        focused = [False]
        hook = ScopedKeyboardHooks(lambda: focused[0], threading.Event())
        hook._event(SimpleNamespace(name='ctrl', scan_code=29, event_type='down'))
        event = SimpleNamespace(name='c', scan_code=46, event_type='down')
        self.assertTrue(hook._event(event))
        focused[0] = True
        self.assertTrue(hook._event(event))
        self.assertTrue(hook._event(SimpleNamespace(name='c', scan_code=46, event_type='up')))

    def test_hooks_only_block_scoped_shortcuts_and_never_emergency_exit(self):
        for key, modifiers in [('tab', {'alt'}), ('c', {'ctrl'}), ('v', {'ctrl'}),
                               ('tab', {'ctrl'}), ('left windows', set()), ('print screen', set()),
                               ('2', {'ctrl'}), ('pagedown', {'ctrl'})]:
            with self.subTest(key=key, modifiers=modifiers):
                self.assertTrue(should_block_key(key, modifiers, True, True))
                self.assertFalse(should_block_key(key, modifiers, False, True))
                self.assertFalse(should_block_key(key, modifiers, True, False))
        self.assertFalse(should_block_key('f12', {'ctrl', 'shift'}, True, True))
        self.assertFalse(should_block_key('a', set(), True, True))


class MetricTests(unittest.TestCase):
    def test_metrics_bound_memory_and_report_tail_latency(self):
        metrics = Metrics(capacity=3)
        for n in (1, 2, 3, 4):
            metrics.observe('analysis_ms', n)
        stats = metrics.snapshot()['analysis_ms']
        self.assertEqual(stats['samples'], 3)
        self.assertEqual(stats['total_samples'], 4)
        self.assertEqual(stats['max'], 4)
        self.assertEqual(stats['p95'], 4)
        self.assertEqual(stats['mean'], 3)


if __name__ == '__main__':
    unittest.main()
