import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from uuid import uuid4


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('Expected finite positive seconds')
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Local Proctoring MVP — Windows 11')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='Simulated GUI; no camera, models or Windows hooks')
    modes.add_argument('--self-test', action='store_true', help='Deterministic core scenario, no GUI')
    parser.add_argument('--data-dir', type=Path, help='Session directory; interrupted sessions restore answers')
    parser.add_argument('--yolo-model', type=Path)
    parser.add_argument('--face-model', type=Path)
    parser.add_argument('--camera', type=int, default=0)
    parser.add_argument('--max-frame-age', type=positive)
    parser.add_argument('--analysis-timeout', type=positive)
    parser.add_argument('--monitor-timeout', type=positive)
    parser.add_argument('--gaze-dwell', type=positive, default=2)
    parser.add_argument('--lock-keys', action='store_true', help='Opt-in scoped hotkey suppression; Ctrl+Shift+F12 exits')
    args = parser.parse_args(argv)
    if args.demo or args.self_test:
        if args.lock_keys:
            parser.error('Simulation modes do not install real keyboard hooks')
        args.max_frame_age = args.max_frame_age or 2
        args.analysis_timeout = args.analysis_timeout or 2
        args.monitor_timeout = args.monitor_timeout or 1
    else:
        for name in ('max_frame_age', 'analysis_timeout', 'monitor_timeout'):
            if getattr(args, name) is None:
                parser.error('--' + name.replace('_', '-') + ' must be explicitly set after profiling')
        for name in ('yolo_model', 'face_model'):
            path = getattr(args, name)
            if path is None or not path.is_file():
                parser.error('--' + name.replace('_', '-') + ' must point to an existing local model')
        if sys.platform != 'win32':
            parser.error('Live focus monitoring requires Windows')
    if args.camera < 0:
        parser.error('--camera must be nonnegative')
    if args.data_dir is None:
        args.data_dir = Path('data') / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    return args


def main(argv=None):
    args = parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        from .demo import run_self_test
        print(json.dumps(run_self_test(args.data_dir), ensure_ascii=False, indent=2))
        return 0
    from .app import run
    run(args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
