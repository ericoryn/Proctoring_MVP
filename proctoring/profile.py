"""Profile real models, explicitly choosing a camera or artificial black frames."""
import argparse
from collections import deque
import json
from pathlib import Path
import platform
from threading import Event, Lock
from time import perf_counter as monotonic, sleep

from .__main__ import positive
from .buffer import LatestFrameBuffer
from .metrics import Metrics
from .vision import VisionAnalyzer
from .workers import CaptureWorker, CVWorker, open_camera


class ProfileSink:
    def __init__(self):
        self.ready = Event()
        self.failed = Event()
        self.lock = Lock()
        self.errors = deque(maxlen=20)
        self.results = 0
        self.last_observation = None

    def result(self, result):
        with self.lock:
            self.results += 1
            self.last_observation = {'frame_id': result.frame_id, 'violations': result.violations,
                                     'description': result.description}
        self.ready.set()

    def cv_error(self, detail):
        with self.lock:
            self.errors.append(detail)
        self.failed.set()
        self.ready.set()

    def camera(self, ok, at, detail=''):
        if not ok:
            self.cv_error('Camera: ' + detail)


def profile_pipeline(source_factory, analyzer_factory, seconds, startup_timeout=60):
    stop, buffer, sink, metrics = Event(), LatestFrameBuffer(), ProfileSink(), Metrics()
    workers = [CaptureWorker(buffer, source_factory, sink, stop),
               CVWorker(buffer, analyzer_factory, sink, stop, metrics)]
    started = monotonic()
    for worker in workers:
        worker.start()
    try:
        if not sink.ready.wait(startup_timeout):
            sink.cv_error('No completed analysis before startup deadline')
        startup_seconds = monotonic() - started
        # Includes the first frame in statistics: cold-start outliers remain visible.
        if not sink.failed.is_set():
            sink.failed.wait(seconds)
    finally:
        stop.set()
        buffer.close()
        deadline = monotonic() + 1
        for worker in workers:
            worker.join(max(0, deadline - monotonic()))
    return {'startup_seconds': startup_seconds, 'elapsed_seconds': monotonic() - started,
            'completed_results': sink.results, 'metrics': metrics.snapshot(), 'errors': list(sink.errors),
            'last_observation': sink.last_observation,
            'workers_alive_at_exit': [worker.name for worker in workers if worker.is_alive()]}


class SyntheticSource:
    def __init__(self):
        import numpy as np
        self.image = np.zeros((480, 640, 3), dtype=np.uint8)

    def read(self):
        sleep(1 / 30)
        return True, self.image

    def release(self):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--synthetic', action='store_true', help='Real models, black frames; NOT a camera benchmark')
    source.add_argument('--camera', type=int, help='Explicitly open this camera for profiling')
    parser.add_argument('--yolo-model', type=Path, required=True)
    parser.add_argument('--face-model', type=Path, required=True)
    parser.add_argument('--seconds', type=positive, default=30)
    parser.add_argument('--startup-timeout', type=positive, default=60)
    parser.add_argument('--output', type=Path, default=Path('data/profile.json'))
    args = parser.parse_args(argv)
    for path in (args.yolo_model, args.face_model):
        if not path.is_file():
            parser.error(f'Model file not found: {path}')
    print('Loading real models. Source: ' + ('SYNTHETIC BLACK FRAMES' if args.synthetic else f'CAMERA {args.camera}'), flush=True)
    report = profile_pipeline(SyntheticSource if args.synthetic else lambda: open_camera(args.camera),
                              lambda: VisionAnalyzer(args.yolo_model, args.face_model),
                              args.seconds, args.startup_timeout)
    report['context'] = {'source': 'synthetic_black_frames' if args.synthetic else f'camera_{args.camera}',
                         'real_models': True, 'python': platform.python_version(), 'platform': platform.platform(),
                         'resolution': '640x480 requested', 'device': 'cpu',
                         'yolo_model': str(args.yolo_model), 'face_model': str(args.face_model),
                         'calibration': 'No timeout recommendations are inferred automatically.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['errors'] or report['workers_alive_at_exit'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
