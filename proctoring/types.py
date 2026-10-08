from dataclasses import dataclass
from enum import Enum
import math
from typing import Any


class State(str, Enum):
    RUNNING = 'RUNNING'
    PAUSED = 'PAUSED'
    STOPPED = 'STOPPED'


@dataclass(frozen=True)
class Policy:
    max_frame_age: float
    analysis_timeout: float
    monitor_timeout: float

    def __post_init__(self):
        for value in (self.max_frame_age, self.analysis_timeout, self.monitor_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('All timeouts must be finite positive seconds')


@dataclass(frozen=True)
class Frame:
    frame_id: int
    captured_at: float
    image: Any


@dataclass(frozen=True)
class Analysis:
    frame_id: int
    captured_at: float
    completed_at: float
    violations: tuple[str, ...] = ()
    timings: tuple[tuple[str, float], ...] = ()
    description: str = ''
