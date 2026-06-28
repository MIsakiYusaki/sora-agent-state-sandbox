from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.recall_eval import (
    SQLiteFTSBackend,
    load_jsonl,
    run_evaluation,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Project Sora memory recall evaluation.")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / ".sandbox" / "evals" / "memory_eval.db",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "evals" / "generated" / "recall_cases.jsonl",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=ROOT / "evals" / "generated" / "memory_snapshot.jsonl",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "evals" / "generated" / "manifest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Defaults to evals/reports/<backend>.",
    )
    parser.add_argument("--category", action="append", help="Only run selected category.")
    parser.add_argument("--limit", type=int, help="Only run the first N selected cases.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = load_jsonl(args.cases)
    if args.category:
        selected = set(args.category)
        cases = [case for case in cases if case.get("category") in selected]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no evaluation cases selected")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    memory_catalog = {
        row["id"]: row
        for row in load_jsonl(args.snapshot)
    }
    backend = SQLiteFTSBackend(args.database)
    output_dir = args.output_dir or ROOT / "evals" / "reports" / backend.name
    try:
        report = run_evaluation(
            cases,
            backend,
            dataset_version=manifest.get("dataset_version", "unknown"),
            memory_catalog=memory_catalog,
        )
    finally:
        backend.close()

    paths = write_report(report, output_dir)
    summary = report["summary"]
    print(
        json.dumps(
            {
                "backend": report["backend"],
                "dataset_version": report["dataset_version"],
                "case_count": summary["case_count"],
                "recall_at_k": summary["recall_at_k"],
                "precision_at_k": summary["precision_at_k"],
                "mrr": summary["mrr"],
                "ndcg_at_k": summary["ndcg_at_k"],
                "no_answer_accuracy": summary["no_answer_accuracy"],
                "forbidden_case_rate": summary["forbidden_case_rate"],
                "surface_action_accuracy": summary["surface_action_accuracy"],
                "full_case_pass_rate": summary["full_case_pass_rate"],
                "latency_ms": summary["latency_ms"],
                "reports": {name: str(path) for name, path in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
