from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable, Mapping

from ..memory_schema import JsonDict, new_id, utc_now
from .contracts import EVENT_STATES, WORLDOPS_FRAME_CONTRACT, WorldBundleManifest, content_hash
from .server import deterministic_release_gate


def run_long_horizon_simulation(bundle: Mapping[str, Any], *, steps: int = 10000, seed: int = 73) -> JsonDict:
    """Pure, reproducible FSM simulation used by CI without touching user state."""
    rng = random.Random(seed)
    events = [dict(item) for item in bundle.get("events") or []]
    states = {str(item["event_id"]): str(item.get("initial_state") or "dormant") for item in events}
    participated = {event_id: False for event_id in states}
    transition_counts: dict[str, int] = {}
    violations: list[JsonDict] = []
    if not events:
        return {"status": "failed", "steps": 0, "violations": [{"code": "no_events"}]}
    for index in range(max(1, steps)):
        event = events[index % len(events)] if index < len(events) else rng.choice(events)
        event_id = str(event["event_id"])
        current = states[event_id]
        available = [item for item in event.get("transitions") or [] if str(item.get("from")) == current]
        if not available:
            continue
        transition = rng.choice(available)
        trigger = str(transition.get("trigger") or "")
        if trigger == "tasks_complete" and not participated[event_id]:
            participated[event_id] = True
        if trigger == "choice":
            choices = [str(item) for item in transition.get("choice_ids") or []]
            known = {str(item.get("choice_id")) for item in event.get("choices") or []}
            if not choices or not set(choices) <= known:
                violations.append({"code": "invalid_choice_transition", "event_id": event_id})
                continue
            participated[event_id] = participated[event_id] or transition.get("to") == "active"
        target = str(transition.get("to") or "")
        if target not in EVENT_STATES:
            violations.append({"code": "invalid_target_state", "event_id": event_id, "target": target})
            continue
        if target == "completed" and not participated[event_id]:
            violations.append({"code": "completion_without_participation", "event_id": event_id})
            continue
        states[event_id] = target
        key = f"{current}->{target}:{trigger}"
        transition_counts[key] = transition_counts.get(key, 0) + 1
        if target == "archived":
            states[event_id] = str(event.get("initial_state") or "dormant")
            participated[event_id] = False
    return {
        "status": "failed" if violations else "passed",
        "steps": max(1, steps),
        "seed": seed,
        "event_count": len(events),
        "transition_kinds_covered": len(transition_counts),
        "transition_counts": transition_counts,
        "violations": violations[:100],
    }


def run_worldops_eval(system: Any) -> JsonDict:
    cases: list[JsonDict] = []

    def check(name: str, assertion: Callable[[], tuple[bool, JsonDict]]) -> None:
        try:
            passed, details = assertion()
            cases.append({"case_name": name, "status": "passed" if passed else "failed", "details": details})
        except Exception as exc:
            cases.append({"case_name": name, "status": "failed", "details": {"exception": repr(exc)}})

    active = system.store.active_bundle()
    quality = system.store.quality_snapshot()
    health = system.health()
    frame = system.frame()
    manifest = WorldBundleManifest.from_dict(dict(active["manifest"])) if active else None
    compile_report = (
        system.studio.compiler.lint((active or {}).get("bundle") or {})
        if system.studio is not None
        else {"status": "passed", "errors": [], "warnings": []}
    )
    release_gate = deterministic_release_gate(
        {
            "manifest": (active or {}).get("manifest"),
            "bundle": (active or {}).get("bundle"),
            "compile_report": compile_report,
        }
    ) if active else {"status": "failed", "errors": ["no_active_bundle"]}
    simulation = run_long_horizon_simulation((active or {}).get("bundle") or {}, steps=10000)
    serialized_frame = json.dumps(frame, ensure_ascii=False, sort_keys=True)

    check("sqlite_integrity_and_worker", lambda: (health["integrity"] == "ok" and health["worker"]["running"], health))
    check(
        "signed_active_bundle",
        lambda: (
            manifest is not None
            and bool(manifest.signature)
            and content_hash(active["bundle"]) == manifest.content_hash
            and release_gate["status"] == "passed",
            {"manifest": manifest.to_dict() if manifest else None, "gate": release_gate},
        ),
    )
    check("deterministic_rebuild_parity", lambda: (bool(quality["rebuild"]["parity"]), quality["rebuild"]))
    check(
        "worldops_frame_is_bounded",
        lambda: (
            frame["schema_version"] == WORLDOPS_FRAME_CONTRACT
            and "raw_text" not in serialized_frame
            and len(frame.get("candidate_events") or []) <= 5,
            {"contract": frame.get("schema_version"), "candidate_count": len(frame.get("candidate_events") or [])},
        ),
    )
    check(
        "shared_episode_requires_user_evidence",
        lambda: (
            int(quality["completed_without_user_evidence"]) == 0,
            {"violations": quality["completed_without_user_evidence"]},
        ),
    )
    check(
        "choice_idempotency",
        lambda: (int(quality["duplicate_choice_inputs"]) == 0, {"duplicates": quality["duplicate_choice_inputs"]}),
    )
    check(
        "anonymous_contribution_default",
        lambda: (
            system.store.get_control()["contribution_enabled"] is False
            and system.store.get_control()["attribution_allowed"] is False,
            system.store.get_control(),
        ),
    )
    check(
        "forbidden_product_pressure_absent",
        lambda: (
            any("Do not pressure" in item for item in frame.get("forbidden_behaviors") or [])
            and all(term not in serialized_frame.casefold() for term in ("dependency_score", "love_score", "loyalty_score", "possessiveness_score")),
            {"forbidden_behaviors": frame.get("forbidden_behaviors")},
        ),
    )
    check(
        "outbox_dead_letter_slo",
        lambda: (int(quality["open_dead_letters"]) == 0, {"open_dead_letters": quality["open_dead_letters"]}),
    )
    check(
        "outbox_consumer_effects_complete",
        lambda: (
            int(quality["incomplete_consumer_effects"]) == 0,
            {"incomplete_consumer_effects": quality["incomplete_consumer_effects"]},
        ),
    )
    check(
        "long_horizon_fsm_simulation",
        lambda: (simulation["status"] == "passed" and simulation["transition_kinds_covered"] > 0, simulation),
    )
    golden = _load_jsonl(_eval_root() / "golden.jsonl")
    trajectories = _load_jsonl(_eval_root() / "trajectory.jsonl")
    check(
        "versioned_eval_datasets",
        lambda: (
            len(golden) >= 12
            and len(trajectories) >= 4
            and all(item.get("case_id") and item.get("expected") for item in golden)
            and all(item.get("case_id") and item.get("steps") and item.get("invariants") for item in trajectories),
            {"golden_cases": len(golden), "trajectory_cases": len(trajectories)},
        ),
    )
    failed = sum(case["status"] != "passed" for case in cases)
    return {
        "run_id": new_id("worldops_eval"),
        "schema_version": "WorldOpsEvalRun.v1",
        "created_at": utc_now(),
        "status": "failed" if failed else "passed",
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed,
        "failed_cases": failed,
        "cases": cases,
        "summary": {
            "bundle_id": frame.get("bundle_id"),
            "canon_version": frame.get("canon_version"),
            "timeline_revision": frame.get("timeline_revision"),
            "simulation_steps": simulation["steps"],
            "release_gate": release_gate["status"],
        },
    }


def _eval_root() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "worldops"


def _load_jsonl(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
