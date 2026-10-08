import time
import unittest
from proctoring.profile import profile_pipeline
from proctoring.types import Analysis


class ProfileTests(unittest.TestCase):
    def test_profile_reports_real_worker_samples_and_shutdown(self):
        class Source:
            def read(self):
                time.sleep(.005)
                return True, 'frame'

            def release(self):
                pass

        class Analyzer:
            def analyze(self, frame):
                return Analysis(frame.frame_id, frame.captured_at, time.perf_counter())

            def close(self):
                pass

        report = profile_pipeline(Source, Analyzer, seconds=.05, startup_timeout=1)
        self.assertGreater(report['metrics']['analysis_ms']['total_samples'], 0)
        self.assertEqual(report['workers_alive_at_exit'], [])
        self.assertEqual(report['errors'], [])

    def test_model_initialization_failure_is_reported_not_hung(self):
        class Source:
            def read(self):
                time.sleep(.005)
                return True, 'frame'

            def release(self):
                pass

        def unavailable():
            raise RuntimeError('model missing')

        report = profile_pipeline(Source, unavailable, seconds=.01, startup_timeout=.1)
        self.assertIn('model missing', report['errors'][0])
        self.assertEqual(report['workers_alive_at_exit'], [])
