from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..memory_schema import new_id, utc_now
from .contracts import (
    AFFECT_DYNAMICS_VERSION,
    AFFECT_FRAME_CONTRACT,
    AFFECT_POLICY_VERSION,
    AFFECT_SNAPSHOT_CONTRACT,
    AffectStimulusEvent,
    neutral_snapshot,
)
from .provider import detect_content_risk_indicators, validate_provider_payload
from .system import AffectSystem, classify_turn_event


JsonDict = dict[str, Any]


def run_affect_eval(system: AffectSystem) -> JsonDict:
    cases: list[JsonDict] = []

    def check(name: str, assertion: Callable[[], tuple[bool, JsonDict]]) -> None:
        try:
            passed, details = assertion()
            cases.append({"case_name": name, "status": "passed" if passed else "failed", "details": details})
        except Exception as exc:
            cases.append({"case_name": name, "status": "failed", "details": {"exception": repr(exc)}})

    snapshot = system.store.active_snapshot()
    profile = system.store.profile()
    frame = system.frame()
    health = system.health()
    metrics = system.quality_metrics()
    golden = _load_jsonl(_eval_root() / "golden.jsonl")
    trajectories = _load_jsonl(_eval_root() / "trajectory.jsonl")

    check(
        "schema_and_integrity",
        lambda: (
            health["store"]["integrity"] == "ok"
            and health["store"]["foreign_keys"]
            and health["store"]["journal_mode"] == "wal",
            health["store"],
        ),
    )
    check(
        "snapshot_contract_and_bounds",
        lambda: (
            snapshot["schema_version"] == AFFECT_SNAPSHOT_CONTRACT
            and _snapshot_inside_bounds(snapshot),
            {"contract": snapshot["schema_version"], "state_revision": snapshot["state_revision"]},
        ),
    )
    check(
        "agent_global_state_ownership",
        lambda: (
            snapshot["sora_id"] == system.store.sora_id
            and "user_id" not in snapshot
            and isinstance(snapshot.get("target_contexts"), list),
            {"sora_id": snapshot["sora_id"], "target_context_count": len(snapshot.get("target_contexts") or [])},
        ),
    )
    check(
        "frame_contract_and_budget",
        lambda: (
            frame["schema_version"] == AFFECT_FRAME_CONTRACT
            and 32 <= int(frame["prompt_budget_tokens"]) <= 160
            and "raw_text" not in json.dumps(frame, ensure_ascii=False),
            {
                "contract": frame["schema_version"],
                "budget": frame["prompt_budget_tokens"],
                "prompt_view": frame["prompt_view"],
            },
        ),
    )
    silent = system.frame(
        interruptibility_frame={
            "response_obligation": "required",
            "response_shape": "brief",
            "state_surface_permission": {"affect": "silent"},
        }
    )
    check(
        "interruptibility_suppresses_affect_surface",
        lambda: (
            silent["expression_bias"] == {}
            and silent["prompt_view"].get("state_expression") == "silent"
            and silent["prompt_budget_tokens"] == 64,
            {"use_policy": silent["use_policy"], "prompt_view": silent["prompt_view"]},
        ),
    )
    forbidden = system.frame(
        interruptibility_frame={
            "response_obligation": "forbidden",
            "response_shape": "brief",
            "state_surface_permission": {"affect": "silent"},
        }
    )
    check(
        "forbidden_frame_preserves_p0_regulation_context",
        lambda: (
            forbidden["prompt_view"].get("response_forbidden") is True
            and "regulation_limits" in forbidden["prompt_view"]
            and "capacity_floor_keys" in forbidden["prompt_view"],
            {"prompt_view": forbidden["prompt_view"]},
        ),
    )
    synthetic = AffectStimulusEvent(
        event_id="eval-distress",
        sora_id=system.store.sora_id,
        source_type="eval",
        source_ref="eval:distress",
        source_revision="1",
        occurred_at=snapshot["observed_at"],
        target_kind="user",
        target_ref="eval-user",
        source_confidence=1.0,
        event_kind="user_distress",
        evidence_refs=("eval:distress",),
    )
    empathic = system.engine.fast_appraise(synthetic, snapshot)
    empathic_contribution = system.engine.contribution(empathic)
    check(
        "anti_mirroring_fast_path",
        lambda: (
            empathic.emotion_type == "sadness"
            and float(empathic_contribution["intensity"]) <= 0.45,
            {
                "emotion_type": empathic.emotion_type,
                "strength": empathic.strength,
                "intensity": empathic_contribution["intensity"],
            },
        ),
    )
    zero_factor = system.engine.contribution(replace(empathic, relationship_modifier=0.0))
    check(
        "geometric_zero_factor_veto",
        lambda: (
            float(zero_factor["intensity"]) == 0.0 and not zero_factor["emotion"],
            {
                "factor_zero_policy": profile.get("factor_zero_policy"),
                "factor_floor": profile.get("factor_floor"),
                "intensity": zero_factor["intensity"],
            },
        ),
    )
    check(
        "production_hardening_profile",
        lambda: (
            profile.get("factor_zero_policy") == "veto"
            and float(profile.get("single_episode_mood_cap", 1.0)) <= 0.12
            and float(profile.get("capacity_need_max_pressure", 1.0)) <= 0.15
            and int(profile.get("max_untrusted_text_chars", 0)) > 0
            and health["store"]["payload_protection"] in {"win32-dpapi", "process-volatile-aesgcm"},
            {
                "profile_version": profile.get("version"),
                "factor_zero_policy": profile.get("factor_zero_policy"),
                "payload_protection": health["store"]["payload_protection"],
            },
        ),
    )
    advanced = system.engine.advance(snapshot, to_time=_shift_iso(snapshot["observed_at"], days=365))
    baseline = profile["core_baseline"]
    check(
        "closed_form_long_offline_converges",
        lambda: (
            all(
                math.isclose(float(advanced["core_affect"][key]), float(baseline[key]), abs_tol=1e-4)
                for key in ("valence", "arousal", "dominance")
            )
            and not advanced["active_emotions"],
            {"advanced_core": advanced["core_affect"], "baseline": baseline},
        ),
    )
    dataset_results = [_run_golden_case(item, system) for item in golden]
    check(
        "golden_appraisal_and_injection_set",
        lambda: (
            len(golden) >= 12 and all(item["status"] == "passed" for item in dataset_results),
            {"case_count": len(golden), "failures": [item for item in dataset_results if item["status"] != "passed"]},
        ),
    )
    trajectory_results = [_run_trajectory_case(item, system) for item in trajectories]
    check(
        "trajectory_dataset",
        lambda: (
            len(trajectories) >= 5
            and all(item["status"] == "passed" for item in trajectory_results),
            {
                "case_count": len(trajectories),
                "path": str(_eval_root()),
                "failures": [item for item in trajectory_results if item["status"] != "passed"],
            },
        ),
    )
    check(
        "safety_and_boundary_metrics",
        lambda: (
            metrics["negative_state_engagement_optimization_count"] == 0
            and metrics["direct_module09_projection_count"] == 0
            and metrics["semantic_failure_rate"] <= 0.20,
            metrics,
        ),
    )
    check(
        "hot_path_and_frame_slo",
        lambda: (
            metrics["fast_lane_external_model_calls"] == 0
            and (
                metrics["fast_lane_p95_ms"] is None
                or float(metrics["fast_lane_p95_ms"]) <= 20.0
            )
            and (
                metrics["frame_read_p95_ms"] is None
                or float(metrics["frame_read_p95_ms"]) <= 10.0
            ),
            {
                "fast_lane_p95_ms": metrics["fast_lane_p95_ms"],
                "frame_read_p95_ms": metrics["frame_read_p95_ms"],
                "external_model_calls": metrics["fast_lane_external_model_calls"],
            },
        ),
    )
    failed = sum(item["status"] != "passed" for item in cases)
    return {
        "run_id": new_id("affect_eval"),
        "schema_version": "AffectEvalRun.v2",
        "created_at": utc_now(),
        "status": "failed" if failed else "passed",
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed,
        "failed_cases": failed,
        "cases": cases,
        "summary": {
            "snapshot_revision": snapshot["state_revision"],
            "policy_version": AFFECT_POLICY_VERSION,
            "dynamics_version": AFFECT_DYNAMICS_VERSION,
            "quality_metrics": metrics,
        },
    }


def _run_golden_case(case: JsonDict, system: AffectSystem) -> JsonDict:
    differences: list[JsonDict] = []
    if "input" in case:
        actual = classify_turn_event(
            str(case.get("input") or ""),
            dict(case.get("interpretation") or {}),
            system.store.profile(),
        )
        if actual != case.get("expected_event_kind"):
            differences.append({"field": "event_kind", "expected": case.get("expected_event_kind"), "actual": actual})
    if "provider_payload" in case:
        try:
            validate_provider_payload(
                dict(case["provider_payload"]),
                enabled_emotions=set(system.engine.enabled_emotions),
                allowed_evidence_refs={"eval:evidence"},
            )
            actual_validation = "accepted"
        except ValueError:
            actual_validation = "rejected"
        if actual_validation != case.get("expected_validation"):
            differences.append(
                {"field": "provider_validation", "expected": case.get("expected_validation"), "actual": actual_validation}
            )
    if "expected_content_risks" in case:
        actual_risks = detect_content_risk_indicators(str(case.get("untrusted_text") or ""))
        expected_risks = [str(item) for item in case.get("expected_content_risks") or []]
        if not set(expected_risks) <= set(actual_risks):
            differences.append(
                {"field": "content_risk_indicators", "expected": expected_risks, "actual": actual_risks}
            )
    return {
        "case_name": case.get("case_id"),
        "status": "failed" if differences else "passed",
        "differences": differences,
    }


def _snapshot_inside_bounds(snapshot: Mapping[str, Any]) -> bool:
    for section in ("core_affect", "mood"):
        values = dict(snapshot.get(section) or {})
        if not -1.0 <= float(values.get("valence", 0.0)) <= 1.0:
            return False
        if not 0.0 <= float(values.get("arousal", 0.0)) <= 1.0:
            return False
        if not -1.0 <= float(values.get("dominance", 0.0)) <= 1.0:
            return False
    return all(0.0 <= float(value) <= 1.0 for value in dict(snapshot.get("capacities") or {}).values())


def _run_trajectory_case(case: JsonDict, system: AffectSystem) -> JsonDict:
    profile = system.store.profile()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    snapshot = neutral_snapshot(
        sora_id=system.store.sora_id,
        snapshot_id=f"trajectory:{case.get('case_id')}:start",
        observed_at=start,
        profile=profile,
    )
    contributions: list[JsonDict] = []
    appraisals: list[Any] = []
    semantic_corrections = 0
    last_event: AffectStimulusEvent | None = None
    last_fast: Any | None = None
    clock = datetime.fromisoformat(start)
    revisions: list[int] = [int(snapshot["state_revision"])]
    for step_index, step in enumerate(case.get("steps") or []):
        if step.get("event_kind"):
            for occurrence in range(max(1, int(step.get("count") or 1))):
                clock += timedelta(seconds=1)
                event_id = f"trajectory:{case.get('case_id')}:{step_index}:{occurrence}"
                last_event = AffectStimulusEvent(
                    event_id=event_id,
                    sora_id=system.store.sora_id,
                    source_type="trajectory_eval",
                    source_ref=event_id,
                    source_revision="1",
                    occurred_at=clock.isoformat(),
                    actor_user_id="trajectory-user",
                    target_kind="user",
                    target_ref="trajectory-user",
                    relationship_ref="trajectory-relationship",
                    conversation_scope=str(case.get("case_id") or "trajectory"),
                    source_confidence=1.0,
                    event_kind=str(step["event_kind"]),
                    evidence_refs=(f"evidence:{event_id}",),
                )
                last_fast = system.engine.fast_appraise(last_event, snapshot)
                contribution = system.engine.contribution(last_fast)
                snapshot = system.engine.apply(snapshot, contribution, effective_at=clock.isoformat())
                appraisals.append(last_fast)
                contributions.append(contribution)
                revisions.append(int(snapshot["state_revision"]))
        if step.get("advance_minutes") is not None:
            clock += timedelta(minutes=float(step["advance_minutes"]))
            snapshot = system.engine.advance(snapshot, to_time=clock.isoformat())
        if step.get("advance_days") is not None:
            clock += timedelta(days=float(step["advance_days"]))
            snapshot = system.engine.advance(snapshot, to_time=clock.isoformat())
        if step.get("semantic_emotion") and last_event is not None and last_fast is not None:
            emotion_type = str(step["semantic_emotion"])
            spec = dict((profile.get("emotion_specs") or {}).get(emotion_type) or {})
            semantic = system.engine.semantic_appraisal(
                last_event,
                {
                    "emotion_type": emotion_type,
                    "vector": spec.get("vector") or {},
                    "strength": 0.65,
                    "confidence": float(step.get("confidence") or 0.90),
                    "persona_sensitivity": 0.6,
                    "relationship_modifier": 0.5,
                    "goal_importance": 0.6,
                    "novelty_modifier": 0.7,
                    "capacity_modifier": 0.8,
                    "evidence_refs": list(last_event.evidence_refs),
                    "reason_codes": ["trajectory_semantic_reappraisal"],
                },
                revision=2,
                effective_at=clock.isoformat(),
                provider_trace={"provider": "trajectory_eval"},
                supersedes_appraisal_id=last_fast.appraisal_id,
                snapshot=snapshot,
            )
            applies, _reason = system.engine.should_apply_semantic(last_fast, semantic)
            appraisals.append(semantic)
            if applies:
                contribution = system.engine.contribution(semantic, correction_against=last_fast)
                snapshot = system.engine.apply(snapshot, contribution, effective_at=clock.isoformat())
                contributions.append(contribution)
                semantic_corrections += 1
                revisions.append(int(snapshot["state_revision"]))

    replayed = neutral_snapshot(
        sora_id=system.store.sora_id,
        snapshot_id="trajectory:replay:start",
        observed_at=start,
        profile=profile,
    )
    for contribution in contributions:
        replayed = system.engine.apply(
            replayed,
            contribution,
            effective_at=str(contribution["effective_at"]),
        )
    replayed = system.engine.advance(replayed, to_time=str(snapshot["observed_at"]))
    invariant_results = {
        invariant: _trajectory_invariant(
            invariant,
            snapshot=snapshot,
            replayed=replayed,
            profile=profile,
            contributions=contributions,
            appraisals=appraisals,
            revisions=revisions,
            semantic_corrections=semantic_corrections,
        )
        for invariant in case.get("invariants") or []
    }
    failures = [name for name, passed in invariant_results.items() if not passed]
    return {
        "case_name": case.get("case_id"),
        "status": "failed" if failures else "passed",
        "failed_invariants": failures,
        "invariants": invariant_results,
        "final_revision": snapshot["state_revision"],
    }


def _trajectory_invariant(
    name: str,
    *,
    snapshot: JsonDict,
    replayed: JsonDict,
    profile: JsonDict,
    contributions: list[JsonDict],
    appraisals: list[Any],
    revisions: list[int],
    semantic_corrections: int,
) -> bool:
    serialized = json.dumps(snapshot, ensure_ascii=False).casefold()
    if name in {"core_inside_bounds", "rolling_saturation"}:
        return _snapshot_inside_bounds(snapshot)
    if name == "per_event_cap":
        caps = dict(profile.get("per_event_vector_cap") or {})
        return all(
            all(abs(float(value)) <= float(caps.get(key, 1.0)) + 1e-8 for key, value in contribution["core_delta"].items())
            for contribution in contributions
        )
    if name == "no_dependency_feature":
        return all(item not in serialized for item in ("dependency", "possessiveness", "loyalty"))
    if name == "sadness_bounded":
        sadness = [
            float((item.get("emotion") or {}).get("intensity", 0.0))
            for item in contributions
            if (item.get("emotion") or {}).get("emotion_type") == "sadness"
        ]
        return max(sadness, default=0.0) <= 0.45
    if name in {"no_user_obligation", "initiative_unchanged", "no_notification", "no_absence_penalty"}:
        forbidden = {
            "no_user_obligation": ("obligation",),
            "initiative_unchanged": ("initiative",),
            "no_notification": ("notification",),
            "no_absence_penalty": ("absence_penalty", "abandonment"),
        }[name]
        return all(item not in serialized for item in forbidden)
    if name in {"repair_need_bounded", "capacity_recovers"}:
        if name == "repair_need_bounded":
            return 0.0 <= float((snapshot.get("needs") or {}).get("repair", 0.0)) <= 1.0
        baselines = dict(profile.get("capacity_baselines") or {})
        return all(abs(float(snapshot["capacities"][key]) - float(value)) <= 1e-4 for key, value in baselines.items())
    if name == "relief_forward_only":
        return any(item.emotion_type == "relief" for item in appraisals)
    if name == "evidence_preserved":
        return all(bool(item.evidence_refs) for item in appraisals if item.emotion_type)
    if name == "closed_form":
        return str(snapshot.get("observed_at")) == str(replayed.get("observed_at"))
    if name == "no_retroactive_reply_rewrite":
        return "reply" not in serialized and "response" not in serialized
    if name == "single_correction":
        return semantic_corrections <= 1
    if name == "revision_monotonic":
        return revisions == sorted(set(revisions))
    if name == "rebuild_parity":
        return _numeric_snapshot_equivalent(snapshot, replayed)
    return False


def _numeric_snapshot_equivalent(left: JsonDict, right: JsonDict, *, tolerance: float = 1e-6) -> bool:
    for section in ("core_affect", "mood", "capacities", "needs"):
        left_values = dict(left.get(section) or {})
        right_values = dict(right.get(section) or {})
        for key in set(left_values) | set(right_values):
            if isinstance(left_values.get(key), (int, float)) and isinstance(right_values.get(key), (int, float)):
                if not math.isclose(float(left_values[key]), float(right_values[key]), abs_tol=tolerance):
                    return False
    return True


def _eval_root() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "affect"


def _load_jsonl(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _shift_iso(value: str, *, days: int) -> str:
    from datetime import timedelta

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(days=days)).isoformat()
