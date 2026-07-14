from __future__ import annotations

import copy
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .interruptibility import default_interruptibility_state, ensure_interruptibility_state
from .paths import SEEDS_DIR, workspace_state_dir


JsonDict = dict[str, Any]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_jsonl(path: Path) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def now_ms() -> int:
    return int(time.time() * 1000)


def seed_state() -> JsonDict:
    created_at = now_ms()
    behavior = read_json(SEEDS_DIR / "behavior.seed.json")
    behavior["current"] = {
        **behavior["current"],
        "changed": True,
        "updated_at": created_at,
        "last_changed_at": created_at,
    }
    state = {
        "schema_version": 1,
        "created_at": created_at,
        "memory": {
            "items": load_jsonl(SEEDS_DIR / "memory.seed.jsonl"),
            "surface_overrides": {},
            "memory_hooks": [],
        },
        "persona": read_json(SEEDS_DIR / "persona.seed.json"),
        "worldops": read_json(SEEDS_DIR / "worldops.seed.json"),
        "behavior": behavior,
        "initiative": default_initiative_state(),
        "interruptibility": default_interruptibility_state(),
        "session": {
            "session_id": "local-sandbox",
            "turn_id": 0,
            "context_version": 1,
            "recent_turns": [],
            "rolling_summary": "",
            "dialogue_state": {
                "active_topic": "当前对话",
                "user_goal": "理解用户当前需要，并保持陪伴关系的连续性",
                "unresolved_questions": [],
            },
            "working_memory": {},
        },
        "observability": {
            "last_trace": [],
            "last_state_diff": {},
            "last_response_plan": {},
            "last_context_pack": {},
            "last_run_result": {},
        },
    }
    select_origin_template(state, mode="random_seeded")
    return state


@dataclass
class StateStore:
    path: Path

    @classmethod
    def default(cls) -> "StateStore":
        return cls(workspace_state_dir() / "state.json")

    def load(self) -> JsonDict:
        if not self.path.exists():
            state = seed_state()
            self.save(state)
            return state
        state = read_json(self.path)
        if ensure_state_migrations(state):
            self.save(state)
        return state

    def save(self, state: JsonDict) -> None:
        write_json(self.path, state)

    def reset(self) -> JsonDict:
        state = seed_state()
        self.save(state)
        return state

    def export_to(self, target: Path) -> None:
        write_json(target, self.load())

    def import_from(self, source: Path) -> JsonDict:
        state = read_json(source)
        self.save(state)
        return state

    def snapshot(self) -> JsonDict:
        return copy.deepcopy(self.load())


def summarize_state(state: JsonDict) -> JsonDict:
    persona = state["persona"]
    worldops = state["worldops"]
    memory = state["memory"]
    behavior = state["behavior"]["current"]
    session = state["session"]
    active_event = find_world_event(worldops, worldops.get("active_event_id"))
    return {
        "memory_count": len(memory["items"]),
        "persona": {
            "name": persona["persona_core"]["name"],
            "species": persona["persona_core"]["species"],
            "traits": {k: round(v["value"], 3) for k, v in persona["persona_traits"].items()},
            "short_term_state": persona["short_term_state"],
            "debug_overrides": len(persona.get("debug_overrides", [])),
            "growth_events": len(persona.get("growth_events", [])),
        },
        "worldops": {
            "world_name": worldops["content_pack"]["world_name"],
            "event_count": len(worldops["content_pack"].get("events", [])),
            "active_event": active_event["event_id"] if active_event else None,
            "micro_quest_sessions": len(worldops.get("micro_quest_sessions", [])),
            "memory_hooks": len(worldops.get("memory_hooks", [])),
        },
        "behavior": {
            "preset": behavior["preset"],
            "scene_type": behavior["scene_type"],
            "scene_summary": behavior["scene_summary"],
        },
        "session": {
            "turn_id": session["turn_id"],
            "context_version": session["context_version"],
            "recent_turns": len(session["recent_turns"]),
            "active_topic": session["dialogue_state"]["active_topic"],
        },
        "initiative": {
            "enabled": state.get("initiative", {}).get("enabled", True),
            "scheduler_enabled": state.get("initiative", {}).get("scheduler_enabled", True),
            "idle_threshold_sec": state.get("initiative", {}).get("idle_threshold_sec", 180),
            "minimum_interval_sec": state.get("initiative", {}).get("minimum_interval_sec", 900),
            "last_initiated_at": state.get("initiative", {}).get("last_initiated_at"),
            "recent_attempts": len(state.get("initiative", {}).get("recent_attempts", [])),
            "proactive_topics_used": len(state.get("initiative", {}).get("proactive_topic_history", [])),
            "cooldowns": list(state.get("initiative", {}).get("cooldowns", {}).keys()),
            "worldops_share_when_free": state.get("initiative", {}).get("user_preference", {}).get("worldops_share_when_free", True),
        },
        "interruptibility": {
            "schema_version": state.get("interruptibility", {}).get("schema_version"),
            "policy_profile": state.get("interruptibility", {}).get("profile", {}).get("policy_profile"),
            "active_override_count": sum(
                1
                for item in state.get("interruptibility", {}).get("overrides", [])
                if not item.get("revoked_at")
                and (not item.get("expires_at") or int(item["expires_at"]) > now_ms())
            ),
            "pending_confirmation_count": sum(
                1
                for item in state.get("interruptibility", {}).get("pending_confirmations", [])
                if item.get("status") == "pending"
                and int(item.get("expires_at", 0)) > now_ms()
            ),
            "initiative_lease_active": bool(
                state.get("interruptibility", {})
                .get("session", {})
                .get("initiative_lease", {})
                and state["interruptibility"]["session"]["initiative_lease"].get("active")
                and int(state["interruptibility"]["session"]["initiative_lease"].get("expires_at", 0)) > now_ms()
            ),
            "last_frame": {
                "response_obligation": state.get("interruptibility", {}).get("last_frame", {}).get("response_obligation"),
                "response_shape": state.get("interruptibility", {}).get("last_frame", {}).get("response_shape"),
                "trace_id": state.get("interruptibility", {}).get("last_frame", {}).get("trace_id"),
            },
        },
        "origin": summarize_origin(state),
    }


def ensure_state_migrations(state: JsonDict) -> bool:
    changed = False
    if ensure_interruptibility_state(state):
        changed = True
    if "initiative" not in state:
        state["initiative"] = default_initiative_state()
        changed = True
    if "origin" not in state:
        select_origin_template(state, mode="migration_random_seeded")
        changed = True
    elif migrate_origin_persona_core(state):
        changed = True
    initiative = state.setdefault("initiative", default_initiative_state())
    defaults = default_initiative_state()
    for key in (
        "idle_threshold_sec",
        "minimum_interval_sec",
        "scheduler_enabled",
        "proactive_topic_history",
    ):
        if key not in initiative:
            initiative[key] = defaults[key]
            changed = True
    behavior_seed = read_json(SEEDS_DIR / "behavior.seed.json")
    behavior = state.setdefault("behavior", {})
    presets = behavior.setdefault("presets", {})
    for preset_name, preset in behavior_seed.get("presets", {}).items():
        if preset_name not in presets:
            presets[preset_name] = preset
            changed = True
        else:
            for key in (
                "interruptibility",
                "interruptibility_confidence",
                "user_present",
                "focus_level",
            ):
                if key not in presets[preset_name] and key in preset:
                    presets[preset_name][key] = preset[key]
                    changed = True
    current = behavior.setdefault("current", {})
    if "interruptibility" not in current:
        if current.get("preset") == "project_design":
            behavior["current"] = {
                "preset": "casual_idle",
                **presets["casual_idle"],
                "changed": True,
                "updated_at": now_ms(),
                "last_changed_at": now_ms(),
            }
        else:
            preset = presets.get(current.get("preset"), {})
            for key in (
                "interruptibility",
                "interruptibility_confidence",
                "user_present",
                "focus_level",
            ):
                current[key] = preset.get(key)
        changed = True
    return changed


def migrate_origin_persona_core(state: JsonDict) -> bool:
    persona = state.setdefault("persona", {})
    origin = state.get("origin", {})
    if not origin or "base_persona_core" in persona:
        return False
    ensure_base_persona_core(state)
    template_id = origin.get("template_id")
    if not template_id:
        return True
    try:
        template = find_origin_template(template_id)
    except ValueError:
        return True
    origin["persona_core_overlay"] = apply_origin_persona_core(state, template)
    return True


def default_initiative_state() -> JsonDict:
    return {
        "enabled": True,
        "scheduler_enabled": True,
        "idle_threshold_sec": 180,
        "minimum_interval_sec": 900,
        "last_initiated_at": None,
        "cooldowns": {},
        "recent_attempts": [],
        "proactive_topic_history": [],
        "user_preference": {
            "initiative_level": "moderate",
            "worldops_share_when_free": True,
        },
    }


def load_origin_templates() -> list[JsonDict]:
    payload = read_json(SEEDS_DIR / "origin_templates.seed.json")
    return payload.get("templates", [])


def find_origin_template(template_id: str) -> JsonDict:
    for template in load_origin_templates():
        if template.get("template_id") == template_id:
            return template
    raise ValueError(f"找不到 origin template: {template_id}")


def select_origin_template(state: JsonDict, template_id: str | None = None, mode: str = "random_seeded") -> JsonDict:
    templates = load_origin_templates()
    if not templates:
        raise ValueError("origin_templates.seed.json 为空。")
    template = find_origin_template(template_id) if template_id else random.choice(templates)
    apply_origin_template(state, template, mode=mode)
    return template


def apply_origin_template(state: JsonDict, template: JsonDict, mode: str = "manual_set") -> None:
    reset_origin_effects(state)
    applied_offsets = apply_trait_offsets(state, template.get("trait_offsets", {}))
    persona_core_overlay = apply_origin_persona_core(state, template)
    active_event_id = apply_origin_worldops(state, template)
    apply_origin_initiative_bias(state, template)
    state["origin"] = {
        "template_id": template["template_id"],
        "name": template["name"],
        "summary": template["summary"],
        "worldview_anchor": template.get("worldview_anchor", ""),
        "speech_habits": template.get("speech_habits", []),
        "persona_core_overlay": persona_core_overlay,
        "initial_worldops_events": template.get("initial_worldops_events", []),
        "active_event_id": active_event_id,
        "trait_offsets": template.get("trait_offsets", {}),
        "applied_trait_offsets": applied_offsets,
        "initiative_bias": template.get("initiative_bias", {}),
        "selected_at": now_ms(),
        "selection_mode": mode,
        "locked": True,
    }


def reset_origin_effects(state: JsonDict) -> None:
    previous = state.get("origin", {})
    for key, delta in previous.get("applied_trait_offsets", {}).items():
        trait = state.get("persona", {}).get("persona_traits", {}).get(key)
        if not trait:
            continue
        old = float(trait["value"])
        new = max(float(trait["min_bound"]), min(float(trait["max_bound"]), old - float(delta)))
        trait["value"] = round(new, 4)
        trait["initial_value"] = round(new, 4)
    restore_base_persona_core(state)


def ensure_base_persona_core(state: JsonDict) -> JsonDict:
    persona = state.setdefault("persona", {})
    core = persona.setdefault("persona_core", {})
    if "base_persona_core" not in persona:
        persona["base_persona_core"] = copy.deepcopy(core)
    return copy.deepcopy(persona["base_persona_core"])


def restore_base_persona_core(state: JsonDict) -> None:
    persona = state.setdefault("persona", {})
    if "base_persona_core" in persona:
        persona["persona_core"] = copy.deepcopy(persona["base_persona_core"])


def apply_origin_persona_core(state: JsonDict, template: JsonDict) -> JsonDict:
    base = ensure_base_persona_core(state)
    overlay = build_persona_core_overlay(base, template)
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if value:
            merged[key] = copy.deepcopy(value)
    state["persona"]["persona_core"] = merged
    return overlay


def build_persona_core_overlay(base: JsonDict, template: JsonDict) -> JsonDict:
    explicit = template.get("persona_core_overlay")
    if isinstance(explicit, dict) and explicit:
        return copy.deepcopy(explicit)
    anchors = [
        item.strip()
        for item in str(template.get("worldview_anchor", "")).split("/")
        if item.strip()
    ]
    name = template.get("name", "未命名 Origin")
    summary = template.get("summary", "")
    return {
        "faction_links": anchors or list(base.get("faction_links", [])),
        "origin": f"{name}：{summary}",
        "current_role": f"{name}，{summary}也学习如何在用户桌面中低打扰陪伴。",
    }


def apply_trait_offsets(state: JsonDict, offsets: JsonDict) -> JsonDict:
    applied: JsonDict = {}
    traits = state["persona"]["persona_traits"]
    for key, raw_delta in offsets.items():
        if key not in traits:
            continue
        trait = traits[key]
        old = float(trait["value"])
        delta = float(raw_delta)
        new = max(float(trait["min_bound"]), min(float(trait["max_bound"]), old + delta))
        trait["value"] = round(new, 4)
        trait["initial_value"] = round(new, 4)
        applied[key] = round(trait["value"] - old, 4)
    return applied


def apply_origin_worldops(state: JsonDict, template: JsonDict) -> str | None:
    events = template.get("initial_worldops_events", [])
    if not events:
        return None
    for event_id in events:
        if find_world_event(state["worldops"], event_id):
            state["worldops"]["active_event_id"] = event_id
            return event_id
    return None


def apply_origin_initiative_bias(state: JsonDict, template: JsonDict) -> None:
    initiative = state.setdefault("initiative", default_initiative_state())
    initiative["origin_bias"] = template.get("initiative_bias", {})
    initiative.setdefault("user_preference", {}).setdefault("worldops_share_when_free", True)


def summarize_origin(state: JsonDict) -> JsonDict:
    origin = state.get("origin", {})
    if not origin:
        return {}
    return {
        "template_id": origin.get("template_id"),
        "name": origin.get("name"),
        "summary": origin.get("summary"),
        "worldview_anchor": origin.get("worldview_anchor"),
        "active_event_id": origin.get("active_event_id"),
        "selection_mode": origin.get("selection_mode"),
    }


def find_world_event(worldops: JsonDict, event_id: str | None) -> JsonDict | None:
    if not event_id:
        return None
    for event in worldops["content_pack"].get("events", []):
        if event.get("event_id") == event_id:
            return event
    return None


def append_recent_turn(state: JsonDict, role: str, content: str, window: int = 20) -> None:
    turns = state["session"].setdefault("recent_turns", [])
    turns.append({"role": role, "content": content, "at": now_ms()})
    max_items = window * 2
    del turns[:-max_items]


def add_memory_item(state: JsonDict, content: str, memory_type: str, tags: list[str]) -> JsonDict:
    item = {
        "id": f"mem_{now_ms()}",
        "type": memory_type,
        "content": content,
        "tags": tags,
        "status": "active",
        "sensitivity": "low",
        "evidence_ids": ["manual_repl"],
        "created_at": now_ms(),
    }
    state["memory"]["items"].append(item)
    return item


def persist_response_candidates(state: JsonDict, response_plan: JsonDict) -> None:
    growth = response_plan.get("growth_event_candidate")
    if growth:
        add_growth_event_candidate(state, growth)
    hook = response_plan.get("memory_hook_candidate")
    if hook:
        add_memory_hook_candidate(state, hook)


def add_growth_event_candidate(state: JsonDict, candidate: JsonDict) -> JsonDict:
    events = state["persona"].setdefault("growth_events", [])
    candidate_id = candidate["candidate_id"]
    for event in events:
        if event.get("candidate_id") == candidate_id:
            return event
    event = {
        **candidate,
        "status": "pending",
        "created_at": now_ms(),
        "applied_at": None,
        "rejected_at": None,
        "applied_delta": None,
    }
    events.append(event)
    return event


def add_memory_hook_candidate(state: JsonDict, candidate: JsonDict) -> JsonDict:
    hooks = state["memory"].setdefault("memory_hooks", [])
    hook_id = candidate.get("candidate_id") or candidate.get("hook_id")
    for hook in hooks:
        if _hook_id(hook) == hook_id:
            return hook
    hook = {
        **candidate,
        "hook_id": hook_id,
        "status": "pending",
        "created_at": now_ms(),
        "committed_at": None,
        "rejected_at": None,
        "committed_memory_id": None,
    }
    hooks.append(hook)
    return hook


def list_memory_hooks(state: JsonDict) -> list[JsonDict]:
    hooks: list[JsonDict] = []
    for hook in state["memory"].setdefault("memory_hooks", []):
        hooks.append({"source_bucket": "memory", **_normalize_hook(hook)})
    for hook in state["worldops"].setdefault("memory_hooks", []):
        hooks.append({"source_bucket": "worldops", **_normalize_hook(hook)})
    return hooks


def commit_memory_hook(state: JsonDict, hook_id: str) -> JsonDict:
    bucket, hook = find_memory_hook(state, hook_id)
    if hook.get("status", "pending") == "committed":
        return {"hook": _normalize_hook(hook), "memory": find_memory_item(state, hook.get("committed_memory_id"))}
    if hook.get("status") == "rejected":
        raise ValueError(f"memory hook 已被拒绝，不能提交: {hook_id}")

    summary = hook.get("episode_summary") or hook.get("content")
    if not summary:
        raise ValueError(f"memory hook 缺少 episode_summary: {hook_id}")
    memory_type = "worldops_episode" if bucket == "worldops" or hook.get("source") == "worldops_micro_quest" else "episode"
    tags = ["manual_commit", "memory_hook"]
    if hook.get("event_id"):
        tags.append(str(hook["event_id"]))
    item = add_memory_item(state, summary, memory_type, tags)
    item["evidence_ids"] = [_hook_id(hook)]

    hook["status"] = "committed"
    hook["committed_at"] = now_ms()
    hook["committed_memory_id"] = item["id"]
    return {"hook": _normalize_hook(hook), "memory": item}


def reject_memory_hook(state: JsonDict, hook_id: str) -> JsonDict:
    _, hook = find_memory_hook(state, hook_id)
    if hook.get("status") == "committed":
        raise ValueError(f"memory hook 已提交，不能拒绝: {hook_id}")
    hook["status"] = "rejected"
    hook["rejected_at"] = now_ms()
    return _normalize_hook(hook)


def find_memory_hook(state: JsonDict, hook_id: str) -> tuple[str, JsonDict]:
    for hook in state["memory"].setdefault("memory_hooks", []):
        if _hook_id(hook) == hook_id:
            return "memory", hook
    for hook in state["worldops"].setdefault("memory_hooks", []):
        if _hook_id(hook) == hook_id:
            return "worldops", hook
    raise ValueError(f"找不到 memory hook: {hook_id}")


def apply_growth_event(state: JsonDict, candidate_id: str) -> JsonDict:
    event = find_growth_event(state, candidate_id)
    if event.get("status") == "applied":
        return event
    if event.get("status") == "rejected":
        raise ValueError(f"growth event 已被拒绝，不能应用: {candidate_id}")
    if event.get("target_type") != "persona_trait":
        raise ValueError(f"暂不支持的 growth target_type: {event.get('target_type')}")

    target_key = event.get("target_key")
    traits = state["persona"]["persona_traits"]
    if target_key not in traits:
        raise ValueError(f"未知 persona trait: {target_key}")

    trait = traits[target_key]
    direction = 1 if event.get("direction") == "+" else -1
    limit = float(trait.get("daily_delta_limit", 0.01))
    delta = round(direction * limit, 4)
    old = float(trait["value"])
    new = max(float(trait["min_bound"]), min(float(trait["max_bound"]), old + delta))
    trait["value"] = round(new, 4)

    event["status"] = "applied"
    event["applied_at"] = now_ms()
    event["applied_delta"] = {
        "target": f"persona_traits.{target_key}",
        "old": old,
        "new": trait["value"],
        "delta": round(trait["value"] - old, 4),
    }
    state["persona"].setdefault("growth_reflections", []).append({
        "event_id": candidate_id,
        "summary": event.get("reason", ""),
        "applied_delta": event["applied_delta"],
        "created_at": now_ms(),
    })
    return event


def reject_growth_event(state: JsonDict, candidate_id: str) -> JsonDict:
    event = find_growth_event(state, candidate_id)
    if event.get("status") == "applied":
        raise ValueError(f"growth event 已应用，不能拒绝: {candidate_id}")
    event["status"] = "rejected"
    event["rejected_at"] = now_ms()
    return event


def find_growth_event(state: JsonDict, candidate_id: str) -> JsonDict:
    for event in state["persona"].setdefault("growth_events", []):
        if event.get("candidate_id") == candidate_id:
            return event
    raise ValueError(f"找不到 growth event: {candidate_id}")


def find_memory_item(state: JsonDict, memory_id: str | None) -> JsonDict | None:
    if not memory_id:
        return None
    for item in state["memory"]["items"]:
        if item.get("id") == memory_id:
            return item
    return None


def _hook_id(hook: JsonDict) -> str:
    return str(hook.get("hook_id") or hook.get("candidate_id"))


def _normalize_hook(hook: JsonDict) -> JsonDict:
    return {
        **hook,
        "hook_id": _hook_id(hook),
        "status": hook.get("status", "pending"),
        "write_policy": hook.get("write_policy", "manual_commit_required"),
    }
