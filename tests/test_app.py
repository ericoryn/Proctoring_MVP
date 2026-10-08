import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO

from proctoring.__main__ import parse_args
from proctoring.demo import run_self_test
from proctoring.buffer import LatestFrameBuffer
from proctoring.types import Frame


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.output = redirect_stderr(StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_live_requires_models_and_explicit_timeouts(self):
        with self.assertRaises(SystemExit):
            parse_args([])

    def test_demo_is_explicit_and_hooks_are_off(self):
        args = parse_args(['--demo'])
        self.assertTrue(args.demo)
        self.assertFalse(args.lock_keys)

    def test_demo_refuses_real_keyboard_hooks(self):
        with self.assertRaises(SystemExit):
            parse_args(['--demo', '--lock-keys'])

    def test_invalid_live_model_files_fail_before_starting_workers(self):
        with self.assertRaises(SystemExit):
            parse_args(['--yolo-model', 'missing.pt', '--face-model', 'missing.task',
                        '--max-frame-age', '2', '--analysis-timeout', '2', '--monitor-timeout', '1'])

    def test_selftest_exercises_pause_recovery_and_persistent_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_self_test(Path(tmp))
            self.assertEqual(result['states'], ['RUNNING', 'PAUSED', 'PAUSED', 'RUNNING', 'STOPPED'])
            report = json.loads((Path(tmp) / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['answers'], {'q1': 'B'})
            self.assertEqual(report['metadata']['outcome'], 'submitted')
            self.assertTrue(any(e['code'] == 'phone' for e in report['events']))

    def test_preview_does_not_consume_cv_frame(self):
        buffer = LatestFrameBuffer()
        buffer.publish(Frame(1, 1, 'pixels'))
        self.assertEqual(buffer.peek().image, 'pixels')
        self.assertEqual(buffer.wait_next(0, .01).frame_id, 1)


if __name__ == '__main__':
    unittest.main()
