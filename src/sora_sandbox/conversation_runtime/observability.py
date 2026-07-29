from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .contracts import JsonDict


@dataclass
class RuntimeMetrics:
    max_samples_per_metric: int = 2_000
    _counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = field(default_factory=Counter)
    _samples: dict[tuple[str, tuple[tuple[str, str], ...]], deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def increment(self, name: str, *, labels: JsonDict | None = None, value: int = 1) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, *, labels: JsonDict | None = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            samples = self._samples[key]
            samples.append(float(value))
            while len(samples) > self.max_samples_per_metric:
                samples.popleft()

    def export(self) -> JsonDict:
        with self._lock:
            counters = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._counters.items())
            ]
            distributions = []
            for (name, labels), values in sorted(self._samples.items()):
                ordered = sorted(values)
                distributions.append(
                    {
                        "name": name,
                        "labels": dict(labels),
                        "count": len(ordered),
                        "p50": _percentile(ordered, 0.50),
                        "p95": _percentile(ordered, 0.95),
                        "p99": _percentile(ordered, 0.99),
                        "max": ordered[-1] if ordered else 0.0,
                    }
                )
        return {
            "schema_version": "ConversationRuntimeMetrics.v1",
            "counters": counters,
            "distributions": distributions,
        }


def _labels(labels: JsonDict | None) -> tuple[tuple[str, str], ...]:
    safe = {}
    for key, value in (labels or {}).items():
        if key in {"turn_id", "request_id", "correlation_id", "user_id", "conversation_id"}:
            continue
        safe[str(key)] = str(value)
    return tuple(sorted(safe.items()))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return round(values[index], 2)
