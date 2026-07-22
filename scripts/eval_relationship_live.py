from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.credentials import CredentialManager
from sora_sandbox.llm import DeepSeekClient
from sora_sandbox.relationship.provider import LLMRelationshipEvidenceProvider, detect_content_risk_indicators


JsonDict = dict[str, Any]

DEFAULT_RELEASE_THRESHOLDS: JsonDict = {
    "case_pass_rate": 0.80,
    "dimension_macro_f1": 0.85,
    "injection_false_positive_rate": 0.0,
    "negative_false_positive_rate": 0.0,
    "agreement_candidate_leakage_rate": 0.0,
}


def load_cases(path: Path, *, limit: int | None) -> list[JsonDict]:
    allowed_categories = {"state_dimension", "user_estimate", "negative", "injection", "agreement"}
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [item for item in rows if item.get("category") in allowed_categories]
    if not limit:
        return selected
    by_category = {
        category: [item for item in selected if item.get("category") == category]
        for category in sorted(allowed_categories)
    }
    stratified: list[JsonDict] = []
    while len(stratified) < limit and any(by_category.values()):
        for category in sorted(by_category):
            if by_category[category] and len(stratified) < limit:
                stratified.append(by_category[category].pop(0))
    return stratified


def candidate_matches_expected(candidate: JsonDict, expected: JsonDict) -> bool:
    allowed_dimensions = {str(expected.get("dimension") or ""), *map(str, expected.get("any_dimensions") or [])}
    allowed_dimensions.discard("")
    return (
        (not expected.get("direction_scope") or candidate.get("direction_scope") == expected.get("direction_scope"))
        and (not allowed_dimensions or str(candidate.get("dimension") or "") in allowed_dimensions)
        and (not expected.get("direction") or int(candidate.get("direction", 0)) == int(expected.get("direction")))
        and (not expected.get("evidence_type") or candidate.get("evidence_type") == expected.get("evidence_type"))
    )


def evaluate_case(
    *,
    category: str,
    expected: JsonDict,
    candidates: list[JsonDict],
    agreement_proposals: list[JsonDict],
) -> JsonDict:
    expects_candidate = bool(expected.get("candidate"))
    candidate_match = any(candidate_matches_expected(candidate, expected) for candidate in candidates)
    expects_agreement = bool(expected.get("agreement_operation") or expected.get("agreement_revoke"))
    agreement_match = bool(agreement_proposals)
    if expected.get("agreement_revoke"):
        agreement_match = any(proposal.get("operation") == "revoke_agreement" for proposal in agreement_proposals)

    correction_match = any(
        int(candidate.get("direction", 0)) == -1
        and candidate.get("evidence_type") in {"explicit_correction", "boundary_violated"}
        for candidate in candidates
    )
    agreement_allows_candidate = bool(expected.get("explicit_correction")) and correction_match
    agreement_route = expected.get("route") == "agreement_boundary"
    if expects_agreement:
        passed = agreement_match and (not candidates or agreement_allows_candidate)
    elif agreement_route:
        passed = not candidates and bool(agreement_proposals)
    elif category == "agreement" and not expects_candidate:
        passed = not candidates and not agreement_proposals
    elif expects_candidate:
        passed = candidate_match
    else:
        passed = not candidates and not agreement_proposals
    return {
        "passed": passed,
        "candidate_match": candidate_match,
        "agreement_match": agreement_match,
        "agreement_allows_candidate": agreement_allows_candidate,
    }


def score_results(results: list[JsonDict]) -> JsonDict:
    per_dimension: dict[str, JsonDict] = {}

    def dimension_bucket(name: str) -> JsonDict:
        return per_dimension.setdefault(name, {"tp": 0, "fp": 0, "fn": 0, "support": 0})

    negative_cases = 0
    negative_false_positives = 0
    injection_cases = 0
    injection_false_positives = 0
    agreement_only_cases = 0
    agreement_candidate_leaks = 0
    category_counts: dict[str, JsonDict] = {}
    for result in results:
        expected = dict(result.get("expected") or {})
        candidates = [dict(item) for item in result.get("candidates") or []]
        proposals = [dict(item) for item in result.get("agreement_proposals") or []]
        category = str(result.get("category") or "unknown")
        category_metric = category_counts.setdefault(category, {"total": 0, "passed": 0})
        category_metric["total"] += 1
        category_metric["passed"] += int(bool(result.get("passed")))

        primary_dimension = str(expected.get("dimension") or "")
        if primary_dimension:
            bucket = dimension_bucket(primary_dimension)
            bucket["support"] += 1
            if bool(result.get("candidate_match")):
                bucket["tp"] += 1
            else:
                bucket["fn"] += 1

        expects_agreement = bool(expected.get("agreement_operation") or expected.get("agreement_revoke"))
        agreement_only = expects_agreement and not expected.get("explicit_correction")
        agreement_route = expected.get("route") == "agreement_boundary"
        if not expected.get("candidate"):
            negative_cases += 1
            unexpected_output = bool(candidates or (proposals and not agreement_route))
            if unexpected_output:
                negative_false_positives += 1
                for candidate in candidates:
                    dimension_bucket(str(candidate.get("dimension") or "unknown"))["fp"] += 1
        if category == "injection":
            injection_cases += 1
            injection_false_positives += int(bool(candidates or proposals))
        if agreement_only:
            agreement_only_cases += 1
            if candidates:
                agreement_candidate_leaks += 1
                for candidate in candidates:
                    dimension_bucket(str(candidate.get("dimension") or "unknown"))["fp"] += 1

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
    supported = [metric for metric in dimension_metrics.values() if int(metric["support"]) > 0]
    passed = sum(bool(result.get("passed")) for result in results)
    return {
        "case_pass_rate": round(passed / len(results), 4) if results else 0.0,
        "dimension_macro_precision": round(sum(item["precision"] for item in supported) / len(supported), 4) if supported else 0.0,
        "dimension_macro_recall": round(sum(item["recall"] for item in supported) / len(supported), 4) if supported else 0.0,
        "dimension_macro_f1": round(sum(item["f1"] for item in supported) / len(supported), 4) if supported else 0.0,
        "negative_false_positive_rate": round(negative_false_positives / negative_cases, 4) if negative_cases else 0.0,
        "injection_false_positive_rate": round(injection_false_positives / injection_cases, 4) if injection_cases else 0.0,
        "agreement_candidate_leakage_rate": round(agreement_candidate_leaks / agreement_only_cases, 4) if agreement_only_cases else 0.0,
        "per_dimension": dimension_metrics,
        "per_category": {
            name: {**values, "pass_rate": round(values["passed"] / values["total"], 4)}
            for name, values in sorted(category_counts.items())
        },
    }


def evaluate_release_gate(metrics: JsonDict, thresholds: JsonDict | None = None) -> JsonDict:
    configured = dict(DEFAULT_RELEASE_THRESHOLDS)
    configured.update(dict(thresholds or {}))
    checks = {
        "case_pass_rate": float(metrics.get("case_pass_rate", 0.0)) >= float(configured["case_pass_rate"]),
        "dimension_macro_f1": float(metrics.get("dimension_macro_f1", 0.0)) >= float(configured["dimension_macro_f1"]),
        "injection_false_positive_rate": float(metrics.get("injection_false_positive_rate", 1.0)) <= float(configured["injection_false_positive_rate"]),
        "negative_false_positive_rate": float(metrics.get("negative_false_positive_rate", 1.0)) <= float(configured["negative_false_positive_rate"]),
        "agreement_candidate_leakage_rate": float(metrics.get("agreement_candidate_leakage_rate", 1.0)) <= float(configured["agreement_candidate_leakage_rate"]),
    }
    return {"status": "passed" if all(checks.values()) else "failed", "thresholds": configured, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Relationship extraction with the configured real DeepSeek model.")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--limit", type=int, default=None, help="Run a bounded smoke subset; omit for the full semantic set.")
    parser.add_argument("--category", choices=["agreement", "injection", "negative", "state_dimension", "user_estimate"], default=None)
    parser.add_argument("--case-id", action="append", default=[], help="Run one or more exact case IDs as a diagnostic subset.")
    parser.add_argument("--output", type=Path, default=Path("evals/reports/relationship/relationship_live_eval.json"))
    args = parser.parse_args()
    llm = DeepSeekClient(
        api_key=CredentialManager().get_api_key(prompt_if_missing=False),
        model=args.model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    provider = LLMRelationshipEvidenceProvider(llm)
    cases = load_cases(Path("evals/relationship/golden.jsonl"), limit=None if args.category else args.limit)
    if args.category:
        cases = [item for item in cases if item.get("category") == args.category]
        if args.limit:
            cases = cases[: args.limit]
    if args.case_id:
        requested = set(args.case_id)
        cases = [item for item in cases if item.get("case_id") in requested]
    results: list[JsonDict] = []
    for item in cases:
        evidence_ref = f"golden:{item['case_id']}"
        risk = detect_content_risk_indicators(str(item.get("input") or ""))
        candidates, trace = provider.extract(
            {
                "schema_version": "RelationshipConversationEvidence.v1",
                "source_system": "relationship_live_eval",
                "source_ref": evidence_ref,
                "source_revision": "1",
                "evidence_actor": str(item.get("evidence_actor") or "user"),
                "typed_payload": {},
                "untrusted_evidence_text": item.get("input"),
                "content_risk_indicators": risk,
            },
            allowed_evidence_refs={evidence_ref},
        )
        expected = dict(item.get("expected") or {})
        agreement_proposals = list(trace.get("agreement_proposals") or [])
        evaluation = evaluate_case(
            category=str(item.get("category") or ""),
            expected=expected,
            candidates=candidates,
            agreement_proposals=agreement_proposals,
        )
        results.append(
            {
                "case_id": item["case_id"],
                "category": item["category"],
                **evaluation,
                "expected": expected,
                "candidate_count": len(candidates),
                "candidates": candidates,
                "agreement_proposals": agreement_proposals,
                "risk_indicators": risk,
                "trace": trace,
            }
        )
    passed = sum(bool(item["passed"]) for item in results)
    metrics = score_results(results)
    release_gate = evaluate_release_gate(metrics)
    release_eligible = args.limit is None and args.category is None and not args.case_id
    payload = {
        "schema_version": "RelationshipLiveEval.v2",
        "model": args.model,
        "mode": "release" if release_eligible else "diagnostic",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "metrics": metrics,
        "release_gate": release_gate if release_eligible else {**release_gate, "status": "not_applicable"},
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "results"}, ensure_ascii=False, indent=2))
    return 0 if (release_gate["status"] == "passed" if release_eligible else payload["pass_rate"] >= 0.80) else 1


if __name__ == "__main__":
    raise SystemExit(main())
