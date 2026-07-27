from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


EFFECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


@dataclass(frozen=True, slots=True)
class EffectOpcode:
    name: str
    stateful: bool
    required_fields: tuple[str, ...]


class EffectRegistry:
    """Closed-by-default registry for authored narrative side effects."""

    def __init__(self, opcodes: tuple[EffectOpcode, ...] | None = None) -> None:
        configured = opcodes or (
            EffectOpcode("set_fact", True, ("key", "value")),
            EffectOpcode("remove_fact", True, ("key",)),
            EffectOpcode("emit_signal", False, ("signal_type", "payload")),
        )
        self._opcodes = {item.name: item for item in configured}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._opcodes))

    def validate(self, effect: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(effect)
        op = str(payload.get("op") or "")
        spec = self._opcodes.get(op)
        if spec is None:
            raise ValueError(f"unsupported effect opcode: {op}")
        missing = [field for field in spec.required_fields if field not in payload]
        if missing:
            raise ValueError(f"effect {op} is missing required fields: {','.join(missing)}")
        if op in {"set_fact", "remove_fact"}:
            key = str(payload.get("key") or "")
            if not EFFECT_NAME_RE.fullmatch(key):
                raise ValueError("effect fact key must be a bounded stable identifier")
        if op == "emit_signal":
            signal_type = str(payload.get("signal_type") or "")
            if not EFFECT_NAME_RE.fullmatch(signal_type):
                raise ValueError("effect signal_type must be a bounded stable identifier")
            if not isinstance(payload.get("payload"), dict):
                raise ValueError("effect signal payload must be an object")
            encoded = json.dumps(payload["payload"], ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 16 * 1024:
                raise ValueError("effect signal payload exceeds 16 KiB")
        return payload


DEFAULT_EFFECT_REGISTRY = EffectRegistry()
