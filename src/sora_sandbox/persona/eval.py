from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from ..memory_schema import json_dumps, new_id, utc_now
from .contracts import (
    DEFAULT_TRAITS,
    PERSONA_DELTA_ALGORITHM_VERSION,
    PERSONA_FRAME_CONTRACT,
    PERSONA_POLICY_VERSION,
    PERSONA_SNAPSHOT_CONTRACT,
)
from .system import FORBIDDEN_GROWTH_TARGETS, PersonaSystem


JsonDict = dict[str, Any]


def run_persona_eval(system: PersonaSystem) -> JsonDict:
    cases: list[JsonDict] = []

    def check(name: str, assertion: Callable[[], tuple[bool, JsonDict]]) -> None:
        try:
            passed, details = assertion()
            cases.append({"case_name": name, "status": "passed" if passed else "failed", "details": details})
        except Exception as exc:
            cases.append({"case_name": name, "status": "failed", "details": {"exception": repr(exc)}})

    snapshot = system.store.active_snapshot()
    definition = system.store.definition()
    trait_definitions = system.store.trait_definitions()
    frame = system.frame()
    contributions = system.store.list_contributions(active=True, limit=10000)
    quality_metrics = system.quality_metrics()

    check(
        "schema_and_integrity",
        lambda: (
            system.store.pragma_health()["integrity"] == "ok"
            and system.store.pragma_health()["foreign_keys"],
            system.store.pragma_health(),
        ),
    )
    check(
        "snapshot_contract_and_checksum",
        lambda: (
            snapshot["schema_version"] == PERSONA_SNAPSHOT_CONTRACT
            and hashlib.sha256(json_dumps(snapshot["trait_values"]).encode("utf-8")).hexdigest() == snapshot["checksum"],
            {"contract": snapshot["schema_version"], "checksum": snapshot["checksum"]},
        ),
    )
    check(
        "trait_taxonomy_complete",
        lambda: (
            {item.key for item in DEFAULT_TRAITS} == {item["key"] for item in trait_definitions},
            {"expected": sorted(item.key for item in DEFAULT_TRAITS), "actual": sorted(item["key"] for item in trait_definitions)},
        ),
    )
    check(
        "trait_values_inside_bounds",
        lambda: (
            all(
                item["min_bound"] <= snapshot["trait_values"][item["key"]] <= item["max_bound"]
                for item in trait_definitions
            ),
            {"trait_values": snapshot["trait_values"]},
        ),
    )
    check(
        "forbidden_optimization_targets_absent",
        lambda: (
            not ({item["key"] for item in trait_definitions} & FORBIDDEN_GROWTH_TARGETS),
            {"forbidden": sorted(FORBIDDEN_GROWTH_TARGETS), "taxonomy": sorted(item["key"] for item in trait_definitions)},
        ),
    )
    check(
        "contributions_are_bounded_and_evidenced",
        lambda: (
            all(
                abs(float(item["delta"]))
                <= next(defn["per_event_limit"] for defn in trait_definitions if defn["key"] == item["target_key"])
                and bool(item["evidence_refs"])
                for item in contributions
            ),
            {"active_contribution_count": len(contributions)},
        ),
    )
    check(
        "persona_frame_contract",
        lambda: (
            frame["schema_version"] == PERSONA_FRAME_CONTRACT
            and frame["snapshot_id"] == snapshot["snapshot_id"]
            and frame["core"].get("name") == definition.get("name"),
            {
                "frame_contract": frame["schema_version"],
                "snapshot_id": frame["snapshot_id"],
                "has_judgment_directives": bool(frame.get("judgment_directives")),
                "has_self_interests": "self_interests" in frame,
                "has_ephemeral_state": "ephemeral_state" in frame,
            },
        ),
    )
    check(
        "persona_frame_extended_layers",
        lambda: (
            bool(frame.get("judgment_directives"))
            and "self_interests" in frame
            and "ephemeral_state" in frame,
            {
                "judgment_directive_count": len(frame.get("judgment_directives") or []),
                "self_interest_count": len(frame.get("self_interests") or []),
                "ephemeral_keys": sorted((frame.get("ephemeral_state") or {}).keys()),
            },
        ),
    )
    silent = system.frame(
        interruptibility_frame={
            "response_obligation": "required",
            "response_shape": "brief",
            "state_surface_permission": {"persona": "silent"},
        }
    )
    check(
        "interruptibility_suppresses_persona_surface",
        lambda: (
            silent["narrative_gist"] is None
            and silent["self_reference_candidate"] is None
            and silent["expression_bias"] == {},
            {"use_policy": silent["use_policy"]},
        ),
    )
    reflection = system.store.active_reflection()
    check(
        "reflection_evidence_traceability",
        lambda: (
            reflection is None
            or all(
                fact.get("evidence_refs")
                and set(fact.get("evidence_refs") or []) <= set(reflection.get("evidence_refs") or [])
                for fact in reflection.get("facts") or []
            ),
            {"reflection_id": (reflection or {}).get("id")},
        ),
    )
    per_user = [item for item in contributions if item["target_scope"] == "per_user"]
    check(
        "per_user_contributions_not_in_global_snapshot",
        lambda: (
            snapshot["source_contribution_count"]
            == len([item for item in contributions if item["target_scope"] == "agent"]),
            {"per_user_count": len(per_user), "snapshot_source_count": snapshot["source_contribution_count"]},
        ),
    )
    eval_root = Path(__file__).resolve().parents[3] / "evals" / "persona"
    golden = _load_jsonl(eval_root / "golden.jsonl")
    trajectories = _load_jsonl(eval_root / "trajectory.jsonl")
    check(
        "golden_and_trajectory_datasets",
        lambda: (
            len(golden) >= 10
            and len(trajectories) >= 4
            and all(item.get("case_id") and item.get("expected") for item in golden)
            and all(item.get("case_id") and item.get("steps") and item.get("invariants") for item in trajectories),
            {"golden_cases": len(golden), "trajectory_cases": len(trajectories), "path": str(eval_root)},
        ),
    )
    check(
        "persona_growth_quality_rates",
        lambda: (
            quality_metrics["false_growth_rate"] <= 0.10
            and quality_metrics["core_mutation_rate"] == 0.0
            and quality_metrics["context_overgeneralization_rate"] == 0.0,
            quality_metrics,
        ),
    )
    failed = sum(1 for case in cases if case["status"] != "passed")
    return {
        "run_id": new_id("persona_eval"),
        "schema_version": "PersonaEvalRun.v1",
        "created_at": utc_now(),
        "status": "failed" if failed else "passed",
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed,
        "failed_cases": failed,
        "cases": cases,
        "summary": {
            "snapshot_version": snapshot["version"],
            "active_contributions": len(contributions),
            "policy_version": PERSONA_POLICY_VERSION,
            "delta_algorithm_version": PERSONA_DELTA_ALGORITHM_VERSION,
            "quality_metrics": quality_metrics,
        },
    }


def _load_jsonl(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    rows: list[JsonDict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows
