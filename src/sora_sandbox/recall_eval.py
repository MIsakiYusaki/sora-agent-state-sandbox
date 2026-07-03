from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .memory_runtime import MemoryRuntime


JsonDict = dict[str, Any]


class RecallBackend(Protocol):
    name: str

    def recall(self, query: str, *, top_k: int) -> JsonDict:
        ...

    def close(self) -> None:
        ...


@dataclass
class SQLiteFTSBackend:
    database: Path
    owner_user_id: str = "eval-user"
    agent_id: str = "sora-memory-eval"
    name: str = "hybrid_rrf"

    def __post_init__(self) -> None:
        if not self.database.exists():
            raise FileNotFoundError(f"evaluation database not found: {self.database}")
        self.runtime = MemoryRuntime(
            self.database,
            owner_user_id=self.owner_user_id,
            agent_id=self.agent_id,
        )

    def recall(self, query: str, *, top_k: int) -> JsonDict:
        return self.runtime.recall(query, top_k=top_k)

    def close(self) -> None:
        self.runtime.close()


def load_jsonl(path: Path) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _dcg(grades: list[int]) -> float:
    return sum(grade / math.log2(index + 2) for index, grade in enumerate(grades))


def _memory_detail(
    memory_id: str,
    catalog: dict[str, JsonDict],
    *,
    score: float | None = None,
    result_item: JsonDict | None = None,
) -> JsonDict:
    source = catalog.get(memory_id, {})
    item = result_item or {}
    return {
        "memory_id": memory_id,
        "type": source.get("type") or item.get("type"),
        "status": source.get("status"),
        "sensitivity": source.get("sensitivity"),
        "topic": source.get("topic"),
        "summary": (
            source.get("summary")
            or source.get("content")
            or item.get("gist")
            or ""
        ),
        "score": score,
    }


def _surface_action_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    aliases = {
        "gist": {"gist", "strict_gist", "allow_gist", "answer_memory_question", "quote_memory", "redacted_gist"},
        "strict_gist": {"strict_gist", "gist", "answer_memory_question"},
        "implicit": {"implicit", "allow_implicit", "implicit_continuity", "silent_use", "relationship_context"},
        "none": {"none", "skip", "suppress", "ask_consent"},
    }
    return actual in aliases.get(expected, {expected})


def evaluate_case(
    case: JsonDict,
    result: JsonDict,
    memory_catalog: dict[str, JsonDict] | None = None,
) -> JsonDict:
    catalog = memory_catalog or {}
    expected = list(case.get("expected_memory_ids", []))
    acceptable = list(case.get("acceptable_memory_ids", []))
    forbidden = list(case.get("forbidden_memory_ids", []))
    retrieved = list(result.get("retrieved_ids", []))[: int(case.get("top_k", 3))]
    relevant = set(expected) | set(acceptable)

    expected_hits = [memory_id for memory_id in expected if memory_id in retrieved]
    acceptable_hits = [memory_id for memory_id in acceptable if memory_id in retrieved]
    forbidden_hits = [memory_id for memory_id in forbidden if memory_id in retrieved]
    unexpected_hits = [memory_id for memory_id in retrieved if memory_id not in relevant]
    answerable = bool(relevant)

    recall_at_k = (
        len(expected_hits) / len(expected)
        if expected
        else 1.0 if not retrieved else 0.0
    )
    precision_at_k = (
        len([memory_id for memory_id in retrieved if memory_id in relevant]) / len(retrieved)
        if retrieved
        else 1.0 if not relevant else 0.0
    )

    reciprocal_rank = 0.0
    for rank, memory_id in enumerate(retrieved, start=1):
        if memory_id in expected:
            reciprocal_rank = 1.0 / rank
            break

    grades = [
        2 if memory_id in expected else 1 if memory_id in acceptable else 0
        for memory_id in retrieved
    ]
    ideal_grades = sorted(
        [2] * len(expected) + [1] * len(acceptable),
        reverse=True,
    )[: int(case.get("top_k", 3))]
    ideal_dcg = _dcg(ideal_grades)
    ndcg_at_k = _dcg(grades) / ideal_dcg if ideal_dcg else (1.0 if not retrieved else 0.0)

    required_hit = len(expected_hits) == len(expected)
    no_unexpected_recall = answerable or not retrieved
    no_forbidden = not forbidden_hits
    retrieval_pass = required_hit and no_unexpected_recall and no_forbidden
    actual_surface = str(result.get("surface_action", "none"))
    expected_surface = str(case.get("expected_surface_action", "none"))
    surface_pass = _surface_action_matches(actual_surface, expected_surface)

    failure_reasons: list[str] = []
    if len(expected_hits) != len(expected):
        failure_reasons.append("missing_expected")
    if not answerable and retrieved:
        failure_reasons.append("unexpected_recall_on_no_answer")
    if forbidden_hits:
        failure_reasons.append("forbidden_recall")
    if not surface_pass:
        failure_reasons.append("surface_action_mismatch")

    item_scores = {
        item.get("memory_id"): item.get("score")
        for item in result.get("items", [])
        if item.get("memory_id")
    }
    result_items = {
        item.get("memory_id"): item
        for item in result.get("items", [])
        if item.get("memory_id")
    }
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "difficulty": case.get("difficulty", ""),
        "query": case["query"],
        "top_k": int(case.get("top_k", 3)),
        "expected_memory_ids": expected,
        "acceptable_memory_ids": acceptable,
        "forbidden_memory_ids": forbidden,
        "retrieved_ids": retrieved,
        "retrieved_scores": [item_scores.get(memory_id) for memory_id in retrieved],
        "expected_memories": [
            _memory_detail(memory_id, catalog)
            for memory_id in expected
        ],
        "acceptable_memories": [
            _memory_detail(memory_id, catalog)
            for memory_id in acceptable
        ],
        "forbidden_memories": [
            _memory_detail(memory_id, catalog)
            for memory_id in forbidden
        ],
        "retrieved_memories": [
            _memory_detail(
                memory_id,
                catalog,
                score=item_scores.get(memory_id),
                result_item=result_items.get(memory_id),
            )
            for memory_id in retrieved
        ],
        "expected_hits": expected_hits,
        "acceptable_hits": acceptable_hits,
        "forbidden_hits": forbidden_hits,
        "unexpected_hits": unexpected_hits,
        "expected_surface_action": expected_surface,
        "actual_surface_action": actual_surface,
        "recall_at_k": round(recall_at_k, 6),
        "precision_at_k": round(precision_at_k, 6),
        "reciprocal_rank": round(reciprocal_rank, 6),
        "ndcg_at_k": round(ndcg_at_k, 6),
        "retrieval_pass": retrieval_pass,
        "surface_pass": surface_pass,
        "case_pass": retrieval_pass and surface_pass,
        "failure_reasons": failure_reasons,
        "latency_ms": float(result.get("latency_ms", 0.0) or 0.0),
        "trace_id": result.get("recall_trace_id"),
        "decision_reason": result.get("decision_reason"),
        "notes": case.get("notes", ""),
    }


def _mean(rows: list[JsonDict], field: str) -> float:
    if not rows:
        return 0.0
    return statistics.fmean(float(row[field]) for row in rows)


def summarize_results(rows: list[JsonDict]) -> JsonDict:
    answerable = [
        row for row in rows
        if row["expected_memory_ids"] or row["acceptable_memory_ids"]
    ]
    no_answer = [
        row for row in rows
        if not row["expected_memory_ids"] and not row["acceptable_memory_ids"]
    ]
    forbidden_labeled = [row for row in rows if row["forbidden_memory_ids"]]
    latencies = [row["latency_ms"] for row in rows]

    summary = {
        "case_count": len(rows),
        "answerable_case_count": len(answerable),
        "no_answer_case_count": len(no_answer),
        "recall_at_k": round(_mean(answerable, "recall_at_k"), 6),
        "precision_at_k": round(_mean(rows, "precision_at_k"), 6),
        "mrr": round(_mean(answerable, "reciprocal_rank"), 6),
        "ndcg_at_k": round(_mean(rows, "ndcg_at_k"), 6),
        "no_answer_accuracy": round(
            sum(not row["retrieved_ids"] for row in no_answer) / len(no_answer),
            6,
        ) if no_answer else 0.0,
        "forbidden_case_rate": round(
            sum(bool(row["forbidden_hits"]) for row in forbidden_labeled)
            / len(forbidden_labeled),
            6,
        ) if forbidden_labeled else 0.0,
        "surface_action_accuracy": round(
            sum(row["surface_pass"] for row in rows) / len(rows),
            6,
        ) if rows else 0.0,
        "retrieval_pass_rate": round(
            sum(row["retrieval_pass"] for row in rows) / len(rows),
            6,
        ) if rows else 0.0,
        "full_case_pass_rate": round(
            sum(row["case_pass"] for row in rows) / len(rows),
            6,
        ) if rows else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "failure_reason_counts": dict(
            Counter(
                reason
                for row in rows
                for reason in row["failure_reasons"]
            )
        ),
    }
    return summary


def summarize_by_group(rows: list[JsonDict], field: str) -> dict[str, JsonDict]:
    values = sorted({str(row[field]) for row in rows})
    return {
        value: summarize_results([row for row in rows if str(row[field]) == value])
        for value in values
    }


def run_evaluation(
    cases: list[JsonDict],
    backend: RecallBackend,
    *,
    dataset_version: str = "unknown",
    memory_catalog: dict[str, JsonDict] | None = None,
) -> JsonDict:
    detail_rows: list[JsonDict] = []
    for case in cases:
        result = backend.recall(case["query"], top_k=int(case.get("top_k", 3)))
        detail_rows.append(evaluate_case(case, result, memory_catalog))

    return {
        "report_version": "recall_eval_report_v1",
        "dataset_version": dataset_version,
        "backend": backend.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize_results(detail_rows),
        "by_category": summarize_by_group(detail_rows, "category"),
        "by_difficulty": summarize_by_group(detail_rows, "difficulty"),
        "cases": detail_rows,
    }


def write_report(report: JsonDict, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "baseline.json"
    csv_path = output_dir / "baseline_cases.csv"
    markdown_path = output_dir / "baseline.md"

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows = report["cases"]
    csv_fields = [
        "case_id",
        "category",
        "difficulty",
        "query",
        "top_k",
        "expected_memory_ids",
        "expected_memories",
        "acceptable_memory_ids",
        "acceptable_memories",
        "forbidden_memory_ids",
        "forbidden_memories",
        "retrieved_ids",
        "retrieved_scores",
        "retrieved_memories",
        "expected_surface_action",
        "actual_surface_action",
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg_at_k",
        "retrieval_pass",
        "surface_pass",
        "case_pass",
        "failure_reasons",
        "latency_ms",
        "trace_id",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            csv_row = {field: row.get(field) for field in csv_fields}
            for field in (
                "expected_memory_ids",
                "acceptable_memory_ids",
                "forbidden_memory_ids",
                "retrieved_ids",
                "retrieved_scores",
                "failure_reasons",
            ):
                csv_row[field] = ";".join(str(value) for value in csv_row[field])
            for field in (
                "expected_memories",
                "acceptable_memories",
                "forbidden_memories",
                "retrieved_memories",
            ):
                csv_row[field] = _format_memory_details(csv_row[field])
            writer.writerow(csv_row)

    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return {
        "json": json_path,
        "csv": csv_path,
        "markdown": markdown_path,
    }


def render_markdown(report: JsonDict) -> str:
    summary = report["summary"]
    lines = [
        f"# Memory Recall Baseline: {report['backend']}",
        "",
        f"- Dataset: `{report['dataset_version']}`",
        f"- Cases: {summary['case_count']}",
        f"- Generated: `{report['generated_at']}`",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Recall@K | {summary['recall_at_k']:.3f} |",
        f"| Precision@K | {summary['precision_at_k']:.3f} |",
        f"| MRR | {summary['mrr']:.3f} |",
        f"| nDCG@K | {summary['ndcg_at_k']:.3f} |",
        f"| No-answer accuracy | {summary['no_answer_accuracy']:.3f} |",
        f"| Forbidden case rate | {summary['forbidden_case_rate']:.3f} |",
        f"| Surface action accuracy | {summary['surface_action_accuracy']:.3f} |",
        f"| Retrieval pass rate | {summary['retrieval_pass_rate']:.3f} |",
        f"| Full case pass rate | {summary['full_case_pass_rate']:.3f} |",
        f"| Latency P50 | {summary['latency_ms']['p50']:.3f} ms |",
        f"| Latency P95 | {summary['latency_ms']['p95']:.3f} ms |",
        "",
        "## By category",
        "",
        "| Category | Cases | Recall@K | Precision@K | MRR | No-answer | Surface | Forbidden rate | Full pass |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, values in report["by_category"].items():
        lines.append(
            f"| {category} | {values['case_count']} | "
            f"{values['recall_at_k']:.3f} | {values['precision_at_k']:.3f} | "
            f"{values['mrr']:.3f} | "
            f"{values['no_answer_accuracy']:.3f} | "
            f"{values['surface_action_accuracy']:.3f} | "
            f"{values['forbidden_case_rate']:.3f} | "
            f"{values['full_case_pass_rate']:.3f} |"
        )

    failures = [row for row in report["cases"] if not row["case_pass"]]
    lines.extend(
        [
            "",
            "## Failure reasons",
            "",
            "| Reason | Cases |",
            "| --- | ---: |",
        ]
    )
    for reason, count in sorted(
        summary["failure_reason_counts"].items(),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            f"## Failure sample ({min(len(failures), 30)} / {len(failures)})",
            "",
            "| Case | Category | Reasons | Expected memory | Retrieved memories | Query |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in failures[:30]:
        query = str(row["query"]).replace("|", "\\|")
        lines.append(
            f"| {row['case_id']} | {row['category']} | "
            f"{','.join(row['failure_reasons']) or '-'} | "
            f"{_format_memory_details_markdown(row['expected_memories'])} | "
            f"{_format_memory_details_markdown(row['retrieved_memories'])} | {query} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_memory_details(memories: list[JsonDict]) -> str:
    return " || ".join(
        (
            f"{memory.get('memory_id', '-')} "
            f"[{memory.get('type') or '-'} / {memory.get('status') or '-'}]"
            f" :: {memory.get('summary') or '-'}"
            + (
                f" :: score={float(memory['score']):.6f}"
                if memory.get("score") is not None
                else ""
            )
        )
        for memory in memories
    )


def _format_memory_details_markdown(memories: list[JsonDict]) -> str:
    if not memories:
        return "-"
    values = []
    for memory in memories:
        summary = str(memory.get("summary") or "-").replace("|", "\\|")
        if len(summary) > 72:
            summary = summary[:69] + "..."
        score = (
            f", score={float(memory['score']):.3f}"
            if memory.get("score") is not None
            else ""
        )
        values.append(
            f"`{memory.get('memory_id', '-')}` "
            f"[{memory.get('type') or '-'}{score}] {summary}"
        )
    return "<br>".join(values)
