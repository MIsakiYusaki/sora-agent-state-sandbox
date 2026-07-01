from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_extractor_provider import GoldenSetExtractorProvider, MemoryExtractorProvider
from .memory_runtime import MemoryRuntime
from .memory_schema import JsonDict


@dataclass(frozen=True)
class ExtractorEvalConfig:
    cases_path: Path
    report_dir: Path
    provider: MemoryExtractorProvider | None = None
    auto_commit: bool = False


def run_extractor_eval(config: ExtractorEvalConfig) -> JsonDict:
    cases = _load_cases(config.cases_path)
    case_results: list[JsonDict] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime = MemoryRuntime(Path(temp_dir) / "extractor_eval.db")
        try:
            provider = config.provider or GoldenSetExtractorProvider()
            for case in cases:
                case_results.append(_run_case(runtime, provider, case, auto_commit=config.auto_commit))
        finally:
            runtime.close()

    report = _summarize(case_results)
    report["cases"] = case_results
    config.report_dir.mkdir(parents=True, exist_ok=True)
    (config.report_dir / "extractor_eval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (config.report_dir / "extractor_eval.md").write_text(_markdown_report(report), encoding="utf-8")
    return report


def _run_case(
    runtime: MemoryRuntime,
    provider: MemoryExtractorProvider,
    case: JsonDict,
    *,
    auto_commit: bool,
) -> JsonDict:
    for memory in case.get("setup_memories", []):
        runtime.add_manual(
            str(memory["summary"]),
            memory_type=str(memory.get("type") or "project_principle"),
            tags=[str(tag) for tag in memory.get("tags", [])],
        )
    turn = runtime.ingest_turn(str(case["user"]), str(case.get("assistant") or "Understood."))
    result = runtime.run_extraction_once(provider=provider, auto_commit=auto_commit)
    candidates = result.get("materialized", {}).get("candidates", [])
    noop_candidates = result.get("materialized", {}).get("noop_candidates", [])
    observed_ops = [item.get("operation") for item in candidates] + [
        item.get("operation") for item in noop_candidates
    ]
    observed_types = [item.get("type") or item.get("memory_type") for item in candidates] + [
        item.get("memory_type") for item in noop_candidates
    ]
    expected_ops = [str(item) for item in case.get("expected_operations", [])]
    acceptable_ops = [str(item) for item in case.get("acceptable_operations", expected_ops)]
    expected_types = [str(item) for item in case.get("expected_memory_types", [])]
    hallucinated = bool(
        case.get("negative")
        and any(str(item.get("operation")) not in {"NOOP", "REVIEW"} for item in candidates)
    )
    target_required = bool(case.get("target_required"))
    target_ok = (not target_required) or all(item.get("target_memory_id") for item in candidates)
    return {
        "case_id": case["case_id"],
        "category": case.get("category", "uncategorized"),
        "episode_id": turn["episode"]["episode_id"],
        "status": result.get("status"),
        "observed_operations": observed_ops,
        "expected_operations": expected_ops,
        "acceptable_operations": acceptable_ops,
        "operation_ok": _matches_expected(observed_ops, expected_ops, acceptable_ops),
        "observed_memory_types": observed_types,
        "expected_memory_types": expected_types,
        "type_ok": True if case.get("negative") and not hallucinated else _contains_all(observed_types, expected_types),
        "hallucinated": hallucinated,
        "target_ok": target_ok,
        "candidate_count": len(candidates),
        "noop_count": len(noop_candidates),
        "error": result.get("error"),
    }


def _summarize(case_results: list[JsonDict]) -> JsonDict:
    total = len(case_results)
    operation_ok = sum(1 for item in case_results if item["operation_ok"])
    type_ok = sum(1 for item in case_results if item["type_ok"])
    target_cases = [item for item in case_results if item["target_ok"] is not None]
    target_ok = sum(1 for item in target_cases if item["target_ok"])
    negative_cases = [item for item in case_results if item["category"] == "negative_implicit"]
    hallucinations = sum(1 for item in negative_cases if item["hallucinated"])
    return {
        "total_cases": total,
        "operation_accuracy": _ratio(operation_ok, total),
        "memory_type_accuracy": _ratio(type_ok, total),
        "target_memory_accuracy": _ratio(target_ok, len(target_cases)),
        "hallucination_rate": _ratio(hallucinations, len(negative_cases)),
        "failed_cases": [
            item["case_id"]
            for item in case_results
            if not item["operation_ok"] or not item["type_ok"] or item["hallucinated"] or not item["target_ok"]
        ],
    }


def _markdown_report(report: JsonDict) -> str:
    lines = [
        "# Extractor Eval Report",
        "",
        f"- total_cases: {report['total_cases']}",
        f"- operation_accuracy: {report['operation_accuracy']:.3f}",
        f"- memory_type_accuracy: {report['memory_type_accuracy']:.3f}",
        f"- target_memory_accuracy: {report['target_memory_accuracy']:.3f}",
        f"- hallucination_rate: {report['hallucination_rate']:.3f}",
        "",
        "| case | category | ops | expected | ok |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        lines.append(
            "| {case_id} | {category} | {ops} | {expected} | {ok} |".format(
                case_id=case["case_id"],
                category=case["category"],
                ops=",".join(str(item) for item in case["observed_operations"]),
                expected=",".join(str(item) for item in case["expected_operations"]),
                ok="yes" if case["operation_ok"] and case["type_ok"] and not case["hallucinated"] else "no",
            )
        )
    return "\n".join(lines) + "\n"


def _load_cases(path: Path) -> list[JsonDict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contains_all(observed: list[Any], expected: list[str]) -> bool:
    if not expected:
        return True
    observed_set = {str(item) for item in observed}
    return all(item in observed_set for item in expected)


def _matches_expected(observed: list[Any], expected: list[str], acceptable: list[str]) -> bool:
    if not observed and ("NOOP" in expected or "NOOP" in acceptable):
        return True
    if acceptable and not any(str(item) in set(acceptable) for item in observed):
        return False
    return _contains_all(observed, expected) or bool(acceptable)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return numerator / denominator
