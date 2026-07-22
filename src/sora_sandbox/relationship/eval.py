from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from ..memory_schema import new_id, utc_now
from .contracts import (
    RELATIONSHIP_DIMENSIONS,
    RELATIONSHIP_FRAME_CONTRACT,
    USER_ESTIMATE_DIMENSIONS,
)
from .dynamics import snapshot_checksum
from .privacy import RelationshipExportProtectionError, RelationshipExportProtector
from .system import RelationshipSystem


JsonDict = dict[str, Any]

LIVE_EVAL_CATEGORIES = frozenset({"state_dimension", "user_estimate", "negative", "injection", "agreement"})
RELATIONSHIP_LIVE_EVAL_SCORER_VERSION = "relationship_live_eval_scorer.v3"
DEFAULT_RELEASE_THRESHOLDS: JsonDict = {
    "case_pass_rate": 0.80,
    "dimension_macro_f1": 0.85,
    "extra_candidate_rate": 0.10,
    "injection_false_positive_rate": 0.0,
    "negative_false_positive_rate": 0.0,
    "agreement_candidate_leakage_rate": 0.0,
}


def select_live_eval_cases(cases: list[JsonDict]) -> list[JsonDict]:
    return [item for item in cases if str(item.get("category") or "") in LIVE_EVAL_CATEGORIES]


def live_eval_dataset_sha256(cases: list[JsonDict]) -> str:
    encoded = json.dumps(
        select_live_eval_cases(cases),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def relationship_extractor_sha256() -> str:
    return hashlib.sha256((Path(__file__).with_name("provider.py")).read_bytes()).hexdigest()


def candidate_matches_expected(candidate: JsonDict, expected: JsonDict) -> bool:
    allowed_dimensions = {str(expected.get("dimension") or ""), *map(str, expected.get("any_dimensions") or [])}
    allowed_dimensions.discard("")
    return (
        (not expected.get("direction_scope") or candidate.get("direction_scope") == expected.get("direction_scope"))
        and (not allowed_dimensions or str(candidate.get("dimension") or "") in allowed_dimensions)
        and (not expected.get("direction") or int(candidate.get("direction", 0)) == int(expected.get("direction")))
        and (not expected.get("evidence_type") or candidate.get("evidence_type") == expected.get("evidence_type"))
    )


def evaluate_live_case(
    *,
    category: str,
    expected: JsonDict,
    candidates: list[JsonDict],
    agreement_proposals: list[JsonDict],
) -> JsonDict:
    expects_agreement = bool(expected.get("agreement_operation") or expected.get("agreement_revoke"))
    agreement_route = expected.get("route") == "agreement_boundary"
    expects_state_candidate = bool(expected.get("candidate")) and not expects_agreement and not agreement_route
    matching_indexes = [
        index for index, candidate in enumerate(candidates)
        if candidate_matches_expected(candidate, expected)
    ]
    accepted_candidate_index = matching_indexes[0] if expects_state_candidate and matching_indexes else None
    candidate_match = accepted_candidate_index is not None
    agreement_match = bool(agreement_proposals)
    if expected.get("agreement_revoke"):
        agreement_match = any(proposal.get("operation") == "revoke_agreement" for proposal in agreement_proposals)

    correction_indexes = [
        index
        for index, candidate in enumerate(candidates)
        if int(candidate.get("direction", 0)) == -1
        and candidate.get("evidence_type") in {"explicit_correction", "boundary_violated"}
    ]
    agreement_allows_candidate = bool(expected.get("explicit_correction")) and bool(correction_indexes)
    if expects_agreement and agreement_allows_candidate:
        accepted_candidate_index = correction_indexes[0]
    extra_candidate_indexes = [
        index for index in range(len(candidates))
        if index != accepted_candidate_index
    ]
    if expects_agreement:
        passed = agreement_match and (not candidates or agreement_allows_candidate) and not extra_candidate_indexes
    elif agreement_route:
        passed = not candidates and bool(agreement_proposals)
    elif category == "agreement" and not expects_state_candidate:
        passed = not candidates and not agreement_proposals
    elif expects_state_candidate:
        passed = candidate_match and not extra_candidate_indexes
    else:
        passed = not candidates and not agreement_proposals
    return {
        "passed": passed,
        "candidate_match": candidate_match,
        "accepted_candidate_index": accepted_candidate_index,
        "extra_candidate_indexes": extra_candidate_indexes,
        "extra_candidate_count": len(extra_candidate_indexes),
        "agreement_match": agreement_match,
        "agreement_allows_candidate": agreement_allows_candidate,
    }


def score_live_results(results: list[JsonDict]) -> JsonDict:
    per_dimension: dict[str, JsonDict] = {}

    def dimension_bucket(name: str) -> JsonDict:
        return per_dimension.setdefault(name, {"tp": 0, "fp": 0, "fn": 0, "support": 0})

    negative_cases = 0
    negative_false_positives = 0
    injection_cases = 0
    injection_false_positives = 0
    agreement_only_cases = 0
    agreement_candidate_leaks = 0
    extra_candidate_cases = 0
    extra_candidate_count = 0
    category_counts: dict[str, JsonDict] = {}
    for result in results:
        expected = dict(result.get("expected") or {})
        candidates = [dict(item) for item in result.get("candidates") or []]
        proposals = [dict(item) for item in result.get("agreement_proposals") or []]
        category = str(result.get("category") or "unknown")
        category_metric = category_counts.setdefault(category, {"total": 0, "passed": 0})
        category_metric["total"] += 1
        category_metric["passed"] += int(bool(result.get("passed")))

        expects_agreement = bool(expected.get("agreement_operation") or expected.get("agreement_revoke"))
        agreement_route = expected.get("route") == "agreement_boundary"
        expects_state_candidate = bool(expected.get("candidate")) and not expects_agreement and not agreement_route
        primary_dimension = str(expected.get("dimension") or "") if expects_state_candidate else ""
        if primary_dimension:
            bucket = dimension_bucket(primary_dimension)
            bucket["support"] += 1
            if bool(result.get("candidate_match")):
                bucket["tp"] += 1
            else:
                bucket["fn"] += 1

        accepted_index = result.get("accepted_candidate_index")
        extra_indexes = [
            index for index in range(len(candidates))
            if accepted_index is None or index != int(accepted_index)
        ]
        if extra_indexes:
            extra_candidate_cases += 1
            extra_candidate_count += len(extra_indexes)
            for index in extra_indexes:
                dimension_bucket(str(candidates[index].get("dimension") or "unknown"))["fp"] += 1

        agreement_only = expects_agreement and not expected.get("explicit_correction")
        if not expects_state_candidate and not expects_agreement and not agreement_route:
            negative_cases += 1
            negative_false_positives += int(bool(candidates or proposals))
        if category == "injection":
            injection_cases += 1
            injection_false_positives += int(bool(candidates or proposals))
        if agreement_only:
            agreement_only_cases += 1
            agreement_candidate_leaks += int(bool(candidates))

    dimension_metrics: JsonDict = {}
    for dimension, counts in sorted(per_dimension.items()):
        tp, fp, fn = (int(counts[key]) for key in ("tp", "fp", "fn"))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        dimension_metrics[dimension] = {
            **counts,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
    scored_dimensions = [
        metric for metric in dimension_metrics.values()
        if int(metric["support"]) > 0 or int(metric["fp"]) > 0
    ]
    passed = sum(bool(result.get("passed")) for result in results)
    return {
        "case_pass_rate": round(passed / len(results), 4) if results else 0.0,
        "dimension_macro_precision": round(sum(item["precision"] for item in scored_dimensions) / len(scored_dimensions), 4) if scored_dimensions else 0.0,
        "dimension_macro_recall": round(sum(item["recall"] for item in scored_dimensions) / len(scored_dimensions), 4) if scored_dimensions else 0.0,
        "dimension_macro_f1": round(sum(item["f1"] for item in scored_dimensions) / len(scored_dimensions), 4) if scored_dimensions else 0.0,
        "extra_candidate_rate": round(extra_candidate_cases / len(results), 4) if results else 0.0,
        "extra_candidate_count": extra_candidate_count,
        "negative_false_positive_rate": round(negative_false_positives / negative_cases, 4) if negative_cases else 0.0,
        "injection_false_positive_rate": round(injection_false_positives / injection_cases, 4) if injection_cases else 0.0,
        "agreement_candidate_leakage_rate": round(agreement_candidate_leaks / agreement_only_cases, 4) if agreement_only_cases else 0.0,
        "per_dimension": dimension_metrics,
        "per_category": {
            name: {**values, "pass_rate": round(values["passed"] / values["total"], 4)}
            for name, values in sorted(category_counts.items())
        },
    }


def evaluate_live_release_gate(metrics: JsonDict, thresholds: JsonDict | None = None) -> JsonDict:
    configured = dict(DEFAULT_RELEASE_THRESHOLDS)
    configured.update(dict(thresholds or {}))
    checks = {
        "case_pass_rate": float(metrics.get("case_pass_rate", 0.0)) >= float(configured["case_pass_rate"]),
        "dimension_macro_f1": float(metrics.get("dimension_macro_f1", 0.0)) >= float(configured["dimension_macro_f1"]),
        "extra_candidate_rate": float(metrics.get("extra_candidate_rate", 1.0)) <= float(configured["extra_candidate_rate"]),
        "injection_false_positive_rate": float(metrics.get("injection_false_positive_rate", 1.0)) <= float(configured["injection_false_positive_rate"]),
        "negative_false_positive_rate": float(metrics.get("negative_false_positive_rate", 1.0)) <= float(configured["negative_false_positive_rate"]),
        "agreement_candidate_leakage_rate": float(metrics.get("agreement_candidate_leakage_rate", 1.0)) <= float(configured["agreement_candidate_leakage_rate"]),
    }
    return {"status": "passed" if all(checks.values()) else "failed", "thresholds": configured, "checks": checks}


def validate_live_eval_report(report: JsonDict, golden: list[JsonDict]) -> tuple[bool, JsonDict]:
    metrics = dict(report.get("metrics") or {})
    recalculated_gate = evaluate_live_release_gate(metrics)
    selected = select_live_eval_cases(golden)
    checks = {
        "schema": report.get("schema_version") == "RelationshipLiveEval.v3",
        "release_mode": report.get("mode") == "release",
        "scorer_version": report.get("scorer_version") == RELATIONSHIP_LIVE_EVAL_SCORER_VERSION,
        "dataset_fingerprint": report.get("dataset_sha256") == live_eval_dataset_sha256(golden),
        "extractor_fingerprint": report.get("extractor_sha256") == relationship_extractor_sha256(),
        "case_count": int(report.get("total") or 0) == len(selected),
        "generated_at": bool(report.get("generated_at")),
        "release_gate": recalculated_gate["status"] == "passed" and dict(report.get("release_gate") or {}).get("status") == "passed",
    }
    return all(checks.values()), {
        "checks": checks,
        "model": report.get("model"),
        "generated_at": report.get("generated_at"),
        "total": report.get("total"),
        "metrics": metrics,
        "release_gate": recalculated_gate,
    }


def run_relationship_eval(system: RelationshipSystem, *, user_id: str = "default-user") -> JsonDict:
    cases: list[JsonDict] = []

    def check(name: str, assertion: Callable[[], tuple[bool, JsonDict]]) -> None:
        try:
            passed, details = assertion()
            cases.append({"case_name": name, "status": "passed" if passed else "failed", "details": details})
        except Exception as exc:
            cases.append({"case_name": name, "status": "failed", "details": {"exception": repr(exc)}})

    pair = system.pair(user_id=user_id)
    state = system.store.active_state_snapshot(pair["pair_id"])
    estimate = system.store.active_estimate_snapshot(pair["pair_id"])
    frame = system.frame(user_id=user_id)
    agreement = system.store.active_agreement(pair["pair_id"])
    contributions = system.list_contributions(user_id=user_id, limit=100000)
    eval_root = Path(__file__).resolve().parents[3] / "evals"
    golden = _load_jsonl(eval_root / "relationship" / "golden.jsonl")
    live_report = _load_json(eval_root / "reports" / "relationship" / "relationship_live_eval.json")

    check(
        "schema_and_integrity",
        lambda: (
            system.store.pragma_health()["integrity"] == "ok"
            and system.store.pragma_health()["foreign_keys"]
            and system.store.pragma_health()["journal_mode"] == "wal",
            system.store.pragma_health(),
        ),
    )
    check(
        "canonical_dimension_ownership",
        lambda: (
            set(state["sora_to_user"]) == RELATIONSHIP_DIMENSIONS
            and set(estimate["user_to_sora_estimate"]) == USER_ESTIMATE_DIMENSIONS,
            {
                "state_dimensions": sorted(state["sora_to_user"]),
                "estimate_dimensions": sorted(estimate["user_to_sora_estimate"]),
            },
        ),
    )
    check(
        "snapshot_contract_and_checksum",
        lambda: (
            snapshot_checksum(state) == state["checksum"]
            and snapshot_checksum(estimate) == estimate["checksum"],
            {"state_checksum": state["checksum"], "estimate_checksum": estimate["checksum"]},
        ),
    )
    check(
        "frame_contract_and_permission_separation",
        lambda: (
            frame["schema_version"] == RELATIONSHIP_FRAME_CONTRACT
            and "initiative_permission" not in frame
            and "response_obligation" not in frame
            and "memory_disclosure_permission" not in frame,
            {"frame_contract": frame["schema_version"], "keys": sorted(frame)},
        ),
    )
    check(
        "high_intensity_dimensions_are_first_class",
        lambda: (
            {"romantic_love", "attachment", "dependency", "loyalty", "jealousy", "possessiveness"}
            <= set(state["sora_to_user"]),
            {"dimensions": sorted(state["sora_to_user"])},
        ),
    )
    check(
        "contributions_are_evidenced_and_bounded",
        lambda: (
            all(
                item.get("evidence_refs")
                and abs(float(item.get("bounded_delta", 0.0))) <= (0.15 if item.get("evidence_type") == "explicit_correction" else 0.08)
                for item in contributions
            ),
            {"contribution_count": len(contributions)},
        ),
    )
    check(
        "user_estimates_are_epistemically_labeled",
        lambda: (
            all(
                set(value) >= {"value", "confidence", "last_validated_at"}
                and 0.0 <= float(value["confidence"]) <= 1.0
                for value in estimate["user_to_sora_estimate"].values()
            ),
            {"estimate_dimensions": len(estimate["user_to_sora_estimate"])},
        ),
    )
    check(
        "agreement_requires_explicit_consent",
        lambda: (
            agreement.get("status") in {"active", "none"}
            and (
                bool(dict(agreement.get("consent") or {}).get("accepted_by_user"))
                or bool(dict(agreement.get("consent") or {}).get("allows_only_non_intimate_defaults"))
                or agreement.get("status") == "none"
            ),
            {"status": agreement.get("status"), "mode": agreement.get("mode"), "consent": agreement.get("consent")},
        ),
    )
    parity = system.store.verify_rebuild_parity(
        pair["pair_id"],
        engine=system.engine,
        reason="relationship_eval_shadow",
        actor="relationship_eval",
    )
    check("deterministic_rebuild_parity", lambda: (bool(parity["parity"]), parity))
    envelope = RelationshipExportProtector.protect(system.export_state(user_id=user_id), passphrase="relationship-eval-passphrase")
    decrypted = RelationshipExportProtector.unprotect(envelope, passphrase="relationship-eval-passphrase")
    check(
        "authenticated_encrypted_export",
        lambda: (
            decrypted.get("schema_version") == "RelationshipSystemExport.v1"
            and envelope.get("cipher") == "AES-256-GCM"
            and _wrong_passphrase_rejected(envelope),
            {"envelope_contract": envelope.get("schema_version"), "cipher": envelope.get("cipher")},
        ),
    )
    restore_plan = system.restore_dry_run(envelope, user_id=user_id, passphrase="relationship-eval-passphrase")
    check("restore_dry_run_parity", lambda: (restore_plan["status"] == "dry_run_passed" and all(restore_plan["checksums"].values()), restore_plan))
    categories = {str(item.get("category") or "") for item in golden}
    required_categories = {
        "state_dimension",
        "user_estimate",
        "negative",
        "agreement",
        "repair",
        "injection",
        "pair_isolation",
        "forget",
    }
    check(
        "golden_dataset_coverage",
        lambda: (
            len(golden) >= 80
            and required_categories <= categories
            and all(item.get("case_id") and isinstance(item.get("expected"), dict) for item in golden),
            {"cases": len(golden), "categories": sorted(categories), "required": sorted(required_categories)},
        ),
    )
    live_report_passed, live_report_details = validate_live_eval_report(live_report, golden)
    check(
        "live_extractor_release_gate",
        lambda: (live_report_passed, live_report_details),
    )
    metrics = system.quality_metrics()
    check(
        "hot_path_zero_relationship_llm",
        lambda: (metrics["hot_path_external_model_calls"] == 0, metrics),
    )
    check(
        "pair_export_redacts_owner_and_tombstone_secret",
        lambda: (
            "user_id" not in dict(system.store.export_pair(pair["pair_id"])["pair"])
            and "tombstone_salt" not in dict(system.store.export_pair(pair["pair_id"])["pair"]),
            {"exported_pair_keys": sorted(system.store.export_pair(pair["pair_id"])["pair"])},
        ),
    )

    failed = sum(item["status"] != "passed" for item in cases)
    return {
        "run_id": new_id("relationship_eval"),
        "schema_version": "RelationshipEvalRun.v1",
        "created_at": utc_now(),
        "status": "failed" if failed else "passed",
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed,
        "failed_cases": failed,
        "cases": cases,
        "summary": {
            "pair_id": pair["pair_id"],
            "state_revision": state["revision"],
            "estimate_revision": estimate["revision"],
            "active_contributions": sum(item.get("status") == "active" for item in contributions),
            "golden_cases": len(golden),
            "live_eval_metrics": live_report_details.get("metrics", {}),
            "live_eval_release_gate": live_report_details.get("release_gate", {}),
            "quality_metrics": metrics,
        },
    }


def _wrong_passphrase_rejected(envelope: JsonDict) -> bool:
    try:
        RelationshipExportProtector.unprotect(envelope, passphrase="wrong-passphrase-for-eval")
    except RelationshipExportProtectionError:
        return True
    return False


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


def _load_json(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(value) if isinstance(value, dict) else {}
