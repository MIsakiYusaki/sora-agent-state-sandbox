from __future__ import annotations

import math
import re
import threading
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


JsonDict = dict[str, Any]
_FORBIDDEN_LABELS = frozenset(
    {
        "turn_id",
        "request_id",
        "correlation_id",
        "conversation_id",
        "client_instance_id",
        "profile_id",
        "subject_id",
        "user_id",
    }
)
_METRIC_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_LABEL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LABEL_VALUE_MAX_LENGTH = 80


@dataclass
class GatewayMetrics:
    max_samples_per_metric: int = 2_000
    max_distinct_label_values: int = 64
    _counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = field(default_factory=Counter)
    _samples: dict[tuple[str, tuple[tuple[str, str], ...]], deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _label_values: dict[tuple[str, str], set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )

    def __post_init__(self) -> None:
        if self.max_samples_per_metric < 1:
            raise ValueError("max_samples_per_metric must be positive")
        if self.max_distinct_label_values < 2:
            raise ValueError("max_distinct_label_values must be at least 2")

    def increment(self, name: str, *, labels: JsonDict | None = None, value: int = 1) -> None:
        with self._lock:
            key = (self._metric_name(name), self._labels(name, labels))
            self._counters[key] += value

    def observe(self, name: str, value: float, *, labels: JsonDict | None = None) -> None:
        with self._lock:
            key = (self._metric_name(name), self._labels(name, labels))
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
            "schema_version": "ConversationGatewayMetrics.v1",
            "counters": counters,
            "distributions": distributions,
        }

    @staticmethod
    def _metric_name(name: str) -> str:
        if not _METRIC_NAME_PATTERN.fullmatch(name):
            raise ValueError("metric name is invalid")
        return name

    def _labels(
        self, metric_name: str, labels: JsonDict | None
    ) -> tuple[tuple[str, str], ...]:
        safe: dict[str, str] = {}
        for raw_key, raw_value in (labels or {}).items():
            key = str(raw_key)
            if key in _FORBIDDEN_LABELS:
                continue
            if not _LABEL_NAME_PATTERN.fullmatch(key):
                raise ValueError("metric label name is invalid")
            value = str(raw_value)
            if (
                len(value) > _LABEL_VALUE_MAX_LENGTH
                or any(ord(character) < 32 for character in value)
            ):
                value = "__invalid__"
            observed = self._label_values[(metric_name, key)]
            if value not in observed:
                if len(observed) >= self.max_distinct_label_values:
                    value = "__overflow__"
                observed.add(value)
            safe[key] = value
        return tuple(sorted(safe.items()))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = max(0.0, min(float(len(values) - 1), (len(values) - 1) * quantile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(values[lower], 2)
    fraction = position - lower
    return round(values[lower] + (values[upper] - values[lower]) * fraction, 2)
