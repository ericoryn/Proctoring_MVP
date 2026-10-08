from threading import Condition
from .types import Frame


class LatestFrameBuffer:
    """One owner publishes immutable snapshots. Inference happens outside this lock."""

    def __init__(self):
        self._condition = Condition()
        self._latest = None
        self._closed = False

    def publish(self, frame: Frame):
        with self._condition:
            if self._closed:
                return
            if self._latest is not None and frame.frame_id <= self._latest.frame_id:
                raise ValueError('Frame IDs must strictly increase')
            self._latest = frame
            self._condition.notify_all()

    def wait_next(self, after_id: int, timeout: float = .2):
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or (self._latest is not None and self._latest.frame_id > after_id),
                timeout,
            )
            return self._latest if ready and not self._closed else None

    def close(self):
        with self._condition:
            self._closed = True
            self._latest = None
            self._condition.notify_all()

    def peek(self):
        with self._condition:
            return self._latest
