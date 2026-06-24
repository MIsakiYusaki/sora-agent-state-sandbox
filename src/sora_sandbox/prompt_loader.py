from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from .paths import PACKAGE_ROOT, PROMPTS_DIR


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PromptRegistry:
    version: str
    system: Path
    user_context: Path
    dialogue_control: Path


def load_prompt_registry(path: Path | None = None) -> PromptRegistry:
    registry_path = path or PROMPTS_DIR / "prompt_registry.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    return PromptRegistry(
        version=payload["version"],
        system=_resolve_prompt_path(payload["system"]),
        user_context=_resolve_prompt_path(payload["user_context"]),
        dialogue_control=_resolve_prompt_path(payload["dialogue_control"]),
    )


def render_template(path: Path, variables: JsonDict) -> str:
    template = Template(path.read_text(encoding="utf-8"))
    return template.substitute({key: _stringify(value) for key, value in variables.items()}).strip()


def render_system_prompt(core: JsonDict) -> str:
    registry = load_prompt_registry()
    return render_template(registry.system, {
        "persona_name": core["name"],
        "persona_species": core["species"],
        "prompt_version": registry.version,
    })


def render_user_prompt(user_input: str, context_pack_json: str) -> str:
    registry = load_prompt_registry()
    dialogue_control_policy = render_template(registry.dialogue_control, {})
    return render_template(registry.user_context, {
        "user_input": user_input,
        "context_pack_json": context_pack_json,
        "dialogue_control_policy": dialogue_control_policy,
        "prompt_version": registry.version,
    })


def _resolve_prompt_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PACKAGE_ROOT / path
    resolved = path.resolve()
    if PROMPTS_DIR.resolve() not in (resolved, *resolved.parents):
        raise ValueError(f"Prompt path must stay under prompts/: {value}")
    return resolved


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)
