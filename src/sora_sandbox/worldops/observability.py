from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


class TraceSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None:
        ...


@dataclass
class StructuredLoggingTraceSink:
    logger_name: str = "sora.worldops"
    level: int = logging.INFO
    logger: logging.Logger = field(init=False)

    def __post_init__(self) -> None:
        self.logger = logging.getLogger(self.logger_name)

    def emit(self, event: Mapping[str, Any]) -> None:
        self.logger.log(
            self.level,
            json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )


def prometheus_text(metrics: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for item in metrics:
        name = _metric_name(str(item.get("name") or "worldops_metric"))
        labels = dict(item.get("labels") or {})
        rendered_labels = ""
        if labels:
            encoded = ",".join(
                f'{_metric_name(str(key))}="{_escape_label(value)}"'
                for key, value in sorted(labels.items())
            )
            rendered_labels = "{" + encoded + "}"
        lines.append(f"{name}{rendered_labels} {float(item.get('value') or 0.0):.12g}")
    return "\n".join(lines) + ("\n" if lines else "")


def _metric_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_:]", "_", value)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "worldops_" + cleaned
    return cleaned


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
