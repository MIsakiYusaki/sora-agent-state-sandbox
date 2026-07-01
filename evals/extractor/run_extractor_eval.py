from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.extractor_eval import ExtractorEvalConfig, run_extractor_eval
from sora_sandbox.memory_extractor_provider import OpenAICompatibleExtractorProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Project Sora extractor eval cases.")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "extractor" / "cases.jsonl")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "evals" / "reports" / "extractor")
    parser.add_argument("--real", action="store_true", help="Use the configured real OpenAI-compatible LLM.")
    args = parser.parse_args()
    provider = OpenAICompatibleExtractorProvider.from_env() if args.real else None
    report = run_extractor_eval(
        ExtractorEvalConfig(cases_path=args.cases, report_dir=args.report_dir, provider=provider)
    )
    print(f"operation_accuracy={report['operation_accuracy']:.3f}")
    print(f"memory_type_accuracy={report['memory_type_accuracy']:.3f}")
    print(f"target_memory_accuracy={report['target_memory_accuracy']:.3f}")
    print(f"hallucination_rate={report['hallucination_rate']:.3f}")
    if report["failed_cases"]:
        print("failed_cases=" + ",".join(report["failed_cases"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
