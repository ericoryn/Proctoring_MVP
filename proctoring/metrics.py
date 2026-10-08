from collections import defaultdict, deque
import json
import math
from pathlib import Path
from threading import Lock


class Metrics:
    """A bounded rolling window, plus lifetime counters; no unbounded raw samples."""
    def __init__(self, capacity=2000):
        if capacity <= 0:
            raise ValueError('capacity must be positive')
        self._lock = Lock()
        self._values = defaultdict(lambda: deque(maxlen=capacity))
        self._counts = defaultdict(int)

    def observe(self, name, value):
        if not math.isfinite(value) or value < 0:
            return
        with self._lock:
            self._values[name].append(value)
            self._counts[name] += 1

    def snapshot(self):
        with self._lock:
            result = {}
            for name, values in self._values.items():
                ordered = sorted(values)
                result[name] = {
                    'samples': len(ordered), 'total_samples': self._counts[name],
                    'mean': sum(ordered) / len(ordered),
                    'p95': ordered[math.ceil(len(ordered) * .95) - 1], 'max': ordered[-1],
                }
            return result

    def export(self, path, context=None):
        Path(path).write_text(json.dumps({'context': context or {}, 'metrics': self.snapshot()},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
