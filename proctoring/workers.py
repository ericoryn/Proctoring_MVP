from threading import Thread
from time import perf_counter as monotonic
from .types import Frame


class CaptureWorker(Thread):
    def __init__(self, buffer, source_factory, controller, stop, retry_delay=.5):
        super().__init__(name='camera-capture', daemon=True)
        self.buffer, self.factory, self.controller, self.stop_event = buffer, source_factory, controller, stop
        self.retry_delay = retry_delay

    def run(self):
        source = None
        frame_id = 0
        try:
            while not self.stop_event.is_set():
                try:
                    if source is None:
                        source = self.factory()
                    ok, image = source.read()
                    if not ok or image is None:
                        raise OSError('Camera returned no frame')
                    # Host receipt time: camera drivers may have their own hidden buffering.
                    now = monotonic()
                    frame_id += 1
                    self.buffer.publish(Frame(frame_id, now, image))
                    self.controller.camera(True, now)
                except Exception as exc:
                    self.controller.camera(False, monotonic(), f'{type(exc).__name__}: {exc}')
                    if source is not None:
                        source.release()
                        source = None
                    self.stop_event.wait(self.retry_delay)
        finally:
            if source is not None:
                source.release()


class CVWorker(Thread):
    def __init__(self, buffer, analyzer_factory, controller, stop, metrics):
        super().__init__(name='cv-inference', daemon=True)
        self.buffer, self.factory, self.controller = buffer, analyzer_factory, controller
        self.stop_event, self.metrics = stop, metrics

    def run(self):
        analyzer = None
        try:
            # Model construction belongs to this worker, never the UI thread.
            analyzer = self.factory()
            after_id = 0
            last_completed = None
            while not self.stop_event.is_set():
                frame = self.buffer.wait_next(after_id)
                if frame is None:
                    continue
                skipped = max(0, frame.frame_id - after_id - 1)
                after_id = frame.frame_id
                start = monotonic()
                self.metrics.observe('queue_age_ms', (start - frame.captured_at) * 1000)
                self.metrics.observe('frames_skipped', skipped)
                try:
                    result = analyzer.analyze(frame)
                except Exception as exc:
                    self.controller.cv_error(f'{type(exc).__name__}: {exc}')
                    self.stop_event.wait(.05)
                    continue
                if self.stop_event.is_set():
                    break
                self.controller.result(result)
                self.metrics.observe('analysis_ms', (result.completed_at - start) * 1000)
                self.metrics.observe('frame_age_ms', (result.completed_at - frame.captured_at) * 1000)
                if last_completed is not None:
                    self.metrics.observe('analysis_gap_ms', (result.completed_at - last_completed) * 1000)
                last_completed = result.completed_at
                for name, value in result.timings:
                    self.metrics.observe(name, value)
        except Exception as exc:
            self.controller.cv_error(f'{type(exc).__name__}: {exc}')
        finally:
            if analyzer is not None:
                try:
                    analyzer.close()
                except Exception as exc:
                    self.controller.cv_error(f'Model cleanup failed: {exc}')


def open_camera(index=0, width=640, height=480):
    import cv2
    camera = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not camera.isOpened():
        camera.release()
        raise OSError(f'Cannot open camera {index}')
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    # Best effort only: some Windows drivers ignore this setting.
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return camera
