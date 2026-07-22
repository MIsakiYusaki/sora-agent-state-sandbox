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
from sora_sandbox.memory_schema import utc_now
from sora_sandbox.relationship.eval import (
    LIVE_EVAL_CATEGORIES,
    RELATIONSHIP_LIVE_EVAL_SCORER_VERSION,
    evaluate_live_case,
    evaluate_live_release_gate,
    live_eval_dataset_sha256,
    relationship_extractor_sha256,
    score_live_results,
    select_live_eval_cases,
)
from sora_sandbox.relationship.provider import LLMRelationshipEvidenceProvider, detect_content_risk_indicators


JsonDict = dict[str, Any]

def load_cases(path: Path, *, limit: int | None) -> list[JsonDict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = select_live_eval_cases(rows)
    if not limit:
        return selected
    by_category = {
        category: [item for item in selected if item.get("category") == category]
        for category in sorted(LIVE_EVAL_CATEGORIES)
    }
    stratified: list[JsonDict] = []
    while len(stratified) < limit and any(by_category.values()):
        for category in sorted(by_category):
            if by_category[category] and len(stratified) < limit:
                stratified.append(by_category[category].pop(0))
    return stratified


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
        evaluation = evaluate_live_case(
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
    metrics = score_live_results(results)
    release_gate = evaluate_live_release_gate(metrics)
    release_eligible = args.limit is None and args.category is None and not args.case_id
    payload = {
        "schema_version": "RelationshipLiveEval.v3",
        "scorer_version": RELATIONSHIP_LIVE_EVAL_SCORER_VERSION,
        "generated_at": utc_now(),
        "dataset_sha256": live_eval_dataset_sha256(load_cases(Path("evals/relationship/golden.jsonl"), limit=None)),
        "extractor_sha256": relationship_extractor_sha256(),
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
