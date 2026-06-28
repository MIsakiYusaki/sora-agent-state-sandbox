from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare recall baseline reports.")
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evals" / "reports" / "comparison.md",
        help="Markdown output path; sibling JSON and CSV files are generated automatically.",
    )
    return parser


def load(path: Path) -> JsonDict:
    return json.loads(path.read_text(encoding="utf-8"))


def memory_text(memories: list[JsonDict], *, include_score: bool = True) -> str:
    values: list[str] = []
    for memory in memories:
        score = (
            f" score={float(memory['score']):.3f}"
            if include_score and memory.get("score") is not None
            else ""
        )
        values.append(
            f"{memory.get('memory_id', '-')} "
            f"[{memory.get('type') or '-'} / {memory.get('status') or '-'}{score}]"
            f" :: {memory.get('summary') or '-'}"
        )
    return " || ".join(values)


def markdown_memories(memories: list[JsonDict]) -> str:
    if not memories:
        return "- 无"
    lines = []
    for memory in memories:
        score = (
            f", score={float(memory['score']):.3f}"
            if memory.get("score") is not None
            else ""
        )
        lines.append(
            f"- `{memory.get('memory_id', '-')}` "
            f"[{memory.get('type') or '-'} / {memory.get('status') or '-'}{score}] "
            f"{memory.get('summary') or '-'}"
        )
    return "\n".join(lines)


def backend_case_view(case: JsonDict) -> JsonDict:
    return {
        "retrieved_memories": case["retrieved_memories"],
        "retrieved_ids": case["retrieved_ids"],
        "recall_at_k": case["recall_at_k"],
        "precision_at_k": case["precision_at_k"],
        "reciprocal_rank": case["reciprocal_rank"],
        "ndcg_at_k": case["ndcg_at_k"],
        "retrieval_pass": case["retrieval_pass"],
        "surface_pass": case["surface_pass"],
        "case_pass": case["case_pass"],
        "actual_surface_action": case["actual_surface_action"],
        "failure_reasons": case["failure_reasons"],
        "latency_ms": case["latency_ms"],
    }


def winner_backends(backends: dict[str, JsonDict]) -> list[str]:
    def rank(values: JsonDict) -> tuple[float, ...]:
        return (
            float(values["retrieval_pass"]),
            float(values["recall_at_k"]),
            float(values["reciprocal_rank"]),
            float(values["precision_at_k"]),
        )

    best = max(rank(values) for values in backends.values())
    return [name for name, values in backends.items() if rank(values) == best]


def build_comparison(reports: list[JsonDict]) -> JsonDict:
    if len(reports) < 2:
        raise ValueError("at least two reports are required")
    dataset_versions = {report["dataset_version"] for report in reports}
    if len(dataset_versions) != 1:
        raise ValueError(f"reports use different datasets: {sorted(dataset_versions)}")

    indexes = {
        report["backend"]: {case["case_id"]: case for case in report["cases"]}
        for report in reports
    }
    case_ids = list(reports[0]["cases"][index]["case_id"] for index in range(len(reports[0]["cases"])))
    for backend, cases in indexes.items():
        if set(cases) != set(case_ids):
            raise ValueError(f"case set mismatch for backend: {backend}")

    compared_cases: list[JsonDict] = []
    for case_id in case_ids:
        reference = reports[0]["cases"][case_ids.index(case_id)]
        backend_values = {
            backend: backend_case_view(cases[case_id])
            for backend, cases in indexes.items()
        }
        retrieval_sets = {
            tuple(values["retrieved_ids"])
            for values in backend_values.values()
        }
        compared_cases.append(
            {
                "case_id": case_id,
                "category": reference["category"],
                "difficulty": reference["difficulty"],
                "query": reference["query"],
                "expected_memories": reference["expected_memories"],
                "acceptable_memories": reference["acceptable_memories"],
                "forbidden_memories": reference["forbidden_memories"],
                "expected_surface_action": reference["expected_surface_action"],
                "winner_backends": winner_backends(backend_values),
                "retrieval_results_differ": len(retrieval_sets) > 1,
                "backends": backend_values,
            }
        )

    backend_names = [report["backend"] for report in reports]
    pairwise_wins: dict[str, int] = {backend: 0 for backend in backend_names}
    ties = 0
    for case in compared_cases:
        winners = case["winner_backends"]
        if len(winners) == 1:
            pairwise_wins[winners[0]] += 1
        else:
            ties += 1

    return {
        "comparison_version": "recall_comparison_v2",
        "dataset_version": reports[0]["dataset_version"],
        "backends": backend_names,
        "backend_summaries": {
            report["backend"]: report["summary"]
            for report in reports
        },
        "case_count": len(compared_cases),
        "cases_with_different_results": sum(
            case["retrieval_results_differ"] for case in compared_cases
        ),
        "single_winner_counts": pairwise_wins,
        "tie_count": ties,
        "cases": compared_cases,
    }


def render_markdown(comparison: JsonDict) -> str:
    backend_names = comparison["backends"]
    lines = [
        "# Memory Recall Backend Comparison",
        "",
        "> 召回指标用于比较后端；surface action 属于共享编排策略，仅作为诊断项。",
        "",
        "## Overall metrics",
        "",
        "| Backend | Recall@K | Precision@K | MRR | nDCG@K | No-answer | Forbidden rate | Retrieval pass | P50 ms | P95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for backend in backend_names:
        summary = comparison["backend_summaries"][backend]
        lines.append(
            f"| {backend} | {summary['recall_at_k']:.3f} | "
            f"{summary['precision_at_k']:.3f} | {summary['mrr']:.3f} | "
            f"{summary['ndcg_at_k']:.3f} | {summary['no_answer_accuracy']:.3f} | "
            f"{summary['forbidden_case_rate']:.3f} | "
            f"{summary['retrieval_pass_rate']:.3f} | "
            f"{summary['latency_ms']['p50']:.3f} | "
            f"{summary['latency_ms']['p95']:.3f} |"
        )

    lines.extend(
        [
            "",
            "## Comparison summary",
            "",
            f"- 用例总数：{comparison['case_count']}",
            f"- 后端召回结果不同的用例：{comparison['cases_with_different_results']}",
            f"- 并列用例：{comparison['tie_count']}",
        ]
    )
    for backend, count in comparison["single_winner_counts"].items():
        lines.append(f"- `{backend}` 单独胜出：{count}")

    lines.extend(["", "## Per-case comparison", ""])
    for case in comparison["cases"]:
        winners = ", ".join(f"`{name}`" for name in case["winner_backends"])
        lines.extend(
            [
                f"### {case['case_id']} · {case['category']} · {case['difficulty']}",
                "",
                f"**Query:** {case['query']}",
                "",
                f"**Expected surface:** `{case['expected_surface_action']}`",
                "",
                "**Expected memories:**",
                "",
                markdown_memories(case["expected_memories"]),
            ]
        )
        if case["acceptable_memories"]:
            lines.extend(
                ["", "**Acceptable memories:**", "", markdown_memories(case["acceptable_memories"])]
            )
        if case["forbidden_memories"]:
            lines.extend(
                ["", "**Forbidden memories:**", "", markdown_memories(case["forbidden_memories"])]
            )
        lines.extend(["", f"**Best backend(s):** {winners}", ""])
        for backend in backend_names:
            values = case["backends"][backend]
            reasons = ", ".join(values["failure_reasons"]) or "none"
            lines.extend(
                [
                    f"#### {backend}",
                    "",
                    (
                        f"- retrieval_pass=`{values['retrieval_pass']}` "
                        f"recall={values['recall_at_k']:.3f} "
                        f"precision={values['precision_at_k']:.3f} "
                        f"MRR={values['reciprocal_rank']:.3f} "
                        f"surface=`{values['actual_surface_action']}` "
                        f"latency={values['latency_ms']:.3f}ms"
                    ),
                    f"- failure_reasons: `{reasons}`",
                    "",
                    markdown_memories(values["retrieved_memories"]),
                    "",
                ]
            )
        lines.extend(["---", ""])
    return "\n".join(lines)


def write_csv(comparison: JsonDict, path: Path) -> None:
    backend_names = comparison["backends"]
    base_fields = [
        "case_id",
        "category",
        "difficulty",
        "query",
        "expected_surface_action",
        "expected_memories",
        "acceptable_memories",
        "forbidden_memories",
        "winner_backends",
        "retrieval_results_differ",
    ]
    backend_fields = [
        field
        for backend in backend_names
        for field in (
            f"{backend}__retrieved_memories",
            f"{backend}__retrieval_pass",
            f"{backend}__recall_at_k",
            f"{backend}__precision_at_k",
            f"{backend}__reciprocal_rank",
            f"{backend}__actual_surface_action",
            f"{backend}__failure_reasons",
            f"{backend}__latency_ms",
        )
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=base_fields + backend_fields)
        writer.writeheader()
        for case in comparison["cases"]:
            row: JsonDict = {
                "case_id": case["case_id"],
                "category": case["category"],
                "difficulty": case["difficulty"],
                "query": case["query"],
                "expected_surface_action": case["expected_surface_action"],
                "expected_memories": memory_text(case["expected_memories"], include_score=False),
                "acceptable_memories": memory_text(case["acceptable_memories"], include_score=False),
                "forbidden_memories": memory_text(case["forbidden_memories"], include_score=False),
                "winner_backends": ";".join(case["winner_backends"]),
                "retrieval_results_differ": case["retrieval_results_differ"],
            }
            for backend in backend_names:
                values = case["backends"][backend]
                row.update(
                    {
                        f"{backend}__retrieved_memories": memory_text(
                            values["retrieved_memories"]
                        ),
                        f"{backend}__retrieval_pass": values["retrieval_pass"],
                        f"{backend}__recall_at_k": values["recall_at_k"],
                        f"{backend}__precision_at_k": values["precision_at_k"],
                        f"{backend}__reciprocal_rank": values["reciprocal_rank"],
                        f"{backend}__actual_surface_action": values[
                            "actual_surface_action"
                        ],
                        f"{backend}__failure_reasons": ";".join(
                            values["failure_reasons"]
                        ),
                        f"{backend}__latency_ms": values["latency_ms"],
                    }
                )
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reports = [load(path) for path in args.reports]
    comparison = build_comparison(reports)

    markdown_path = args.output
    json_path = markdown_path.with_suffix(".json")
    csv_path = markdown_path.with_name(markdown_path.stem + "_cases.csv")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(comparison), encoding="utf-8")
    json_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(comparison, csv_path)
    print(
        json.dumps(
            {
                "markdown": str(markdown_path),
                "json": str(json_path),
                "csv": str(csv_path),
                "case_count": comparison["case_count"],
                "cases_with_different_results": comparison[
                    "cases_with_different_results"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
