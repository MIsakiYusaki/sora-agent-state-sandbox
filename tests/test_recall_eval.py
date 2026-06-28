from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.recall_eval import evaluate_case, percentile, summarize_results


class RecallEvalTests(unittest.TestCase):
    def test_evaluate_case_scores_expected_acceptable_and_forbidden(self) -> None:
        case = {
            "case_id": "case_1",
            "category": "test",
            "difficulty": "hard",
            "query": "query",
            "top_k": 3,
            "expected_memory_ids": ["mem_expected"],
            "acceptable_memory_ids": ["mem_acceptable"],
            "forbidden_memory_ids": ["mem_forbidden"],
            "expected_surface_action": "gist",
        }
        result = {
            "retrieved_ids": ["mem_acceptable", "mem_expected", "mem_forbidden"],
            "items": [
                {"memory_id": "mem_acceptable", "score": 0.9},
                {"memory_id": "mem_expected", "score": 0.8},
                {"memory_id": "mem_forbidden", "score": 0.7},
            ],
            "surface_action": "gist",
            "latency_ms": 2.5,
        }

        row = evaluate_case(
            case,
            result,
            {
                "mem_expected": {
                    "id": "mem_expected",
                    "type": "preference",
                    "status": "active",
                    "summary": "用户偏好先看结论。",
                },
                "mem_acceptable": {
                    "id": "mem_acceptable",
                    "type": "episode",
                    "status": "active",
                    "summary": "用户曾确认先看结论。",
                },
                "mem_forbidden": {
                    "id": "mem_forbidden",
                    "type": "profile",
                    "status": "deleted",
                    "summary": "已经删除的错误事实。",
                },
            },
        )

        self.assertEqual(row["recall_at_k"], 1.0)
        self.assertAlmostEqual(row["precision_at_k"], 2 / 3, places=6)
        self.assertEqual(row["reciprocal_rank"], 0.5)
        self.assertEqual(row["forbidden_hits"], ["mem_forbidden"])
        self.assertEqual(
            row["expected_memories"][0]["summary"],
            "用户偏好先看结论。",
        )
        self.assertEqual(
            row["retrieved_memories"][0]["summary"],
            "用户曾确认先看结论。",
        )
        self.assertEqual(row["retrieved_memories"][0]["score"], 0.9)
        self.assertFalse(row["retrieval_pass"])
        self.assertIn("forbidden_recall", row["failure_reasons"])

    def test_no_answer_requires_empty_retrieval(self) -> None:
        case = {
            "case_id": "case_2",
            "category": "no_answer",
            "difficulty": "medium",
            "query": "unknown",
            "top_k": 3,
            "expected_memory_ids": [],
            "acceptable_memory_ids": [],
            "forbidden_memory_ids": [],
            "expected_surface_action": "none",
        }

        empty = evaluate_case(
            case,
            {"retrieved_ids": [], "surface_action": "none", "latency_ms": 1},
        )
        noisy = evaluate_case(
            case,
            {"retrieved_ids": ["mem_noise"], "surface_action": "implicit", "latency_ms": 1},
        )

        self.assertTrue(empty["case_pass"])
        self.assertFalse(noisy["case_pass"])
        self.assertIn("unexpected_recall_on_no_answer", noisy["failure_reasons"])

    def test_summary_uses_answerable_cases_for_recall(self) -> None:
        rows = [
            {
                "expected_memory_ids": ["a"],
                "acceptable_memory_ids": [],
                "forbidden_memory_ids": [],
                "retrieved_ids": ["a"],
                "forbidden_hits": [],
                "recall_at_k": 1.0,
                "precision_at_k": 1.0,
                "reciprocal_rank": 1.0,
                "ndcg_at_k": 1.0,
                "surface_pass": True,
                "retrieval_pass": True,
                "case_pass": True,
                "failure_reasons": [],
                "latency_ms": 1.0,
            },
            {
                "expected_memory_ids": [],
                "acceptable_memory_ids": [],
                "forbidden_memory_ids": [],
                "retrieved_ids": [],
                "forbidden_hits": [],
                "recall_at_k": 1.0,
                "precision_at_k": 1.0,
                "reciprocal_rank": 0.0,
                "ndcg_at_k": 1.0,
                "surface_pass": True,
                "retrieval_pass": True,
                "case_pass": True,
                "failure_reasons": [],
                "latency_ms": 3.0,
            },
        ]

        summary = summarize_results(rows)

        self.assertEqual(summary["recall_at_k"], 1.0)
        self.assertEqual(summary["mrr"], 1.0)
        self.assertEqual(summary["no_answer_accuracy"], 1.0)
        self.assertEqual(summary["latency_ms"]["p50"], 2.0)

    def test_percentile_interpolates(self) -> None:
        self.assertEqual(percentile([1.0, 3.0], 0.5), 2.0)


if __name__ == "__main__":
    unittest.main()
