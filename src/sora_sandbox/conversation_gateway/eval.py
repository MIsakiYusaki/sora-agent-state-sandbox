from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


JsonDict = dict[str, object]


REQUIRED_CATEGORIES = frozenset(
    {
        "authentication",
        "authorization_isolation",
        "protocol_validation",
        "idempotency",
        "event_resume",
        "terminal_repair",
        "cancellation",
        "history_consistency",
        "concurrency",
        "backpressure",
        "shutdown_recovery",
    }
)


@dataclass(frozen=True)
class GatewayEvalCase:
    case_id: str
    category: str
    operation: str
    expectations: dict[str, object]


class GatewayReleaseGate:
    DEFAULT_THRESHOLDS: Mapping[str, tuple[str, float]] = {
        "e2e_success_rate": (">=", 0.99),
        "authorization_isolation_rate": (">=", 1.0),
        "idempotency_rate": (">=", 1.0),
        "event_resume_integrity_rate": (">=", 1.0),
        "terminal_delivery_rate": (">=", 1.0),
        "history_consistency_rate": (">=", 1.0),
        "p95_submit_ack_ms": ("<=", 500.0),
    }

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("release signing key must contain at least 32 bytes")
        self.signing_key = signing_key

    def evaluate(
        self,
        metrics: Mapping[str, float],
        *,
        dataset_version: str,
        policy_profile: str,
        thresholds: Mapping[str, tuple[str, float]] | None = None,
    ) -> JsonDict:
        checks: JsonDict = {}
        for name, (operator, threshold) in dict(
            thresholds or self.DEFAULT_THRESHOLDS
        ).items():
            actual = float(metrics.get(name, 0.0))
            passed = actual >= threshold if operator == ">=" else actual <= threshold
            checks[name] = {
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": passed,
            }
        failed = sorted(name for name, item in checks.items() if not item["passed"])
        result: JsonDict = {
            "schema_version": "ConversationGatewayReleaseDecision.v1",
            "decision_id": f"gateway_release_{uuid.uuid4().hex}",
            "status": "passed" if not failed else "blocked",
            "dataset_version": dataset_version,
            "policy_profile": policy_profile,
            "checks": checks,
            "failed_checks": failed,
            "created_at_ms": int(time.time() * 1000),
        }
        result["signature"] = self._sign(result)
        return result

    def verify(self, decision: Mapping[str, object]) -> bool:
        candidate = dict(decision)
        signature = str(candidate.pop("signature", ""))
        return hmac.compare_digest(signature, self._sign(candidate))

    def _sign(self, payload: Mapping[str, object]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hmac.new(
            self.signing_key, canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()


def load_gateway_golden_set(path: Path) -> list[GatewayEvalCase]:
    result: list[GatewayEvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        case_id = str(payload.get("case_id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case_id at line {line_number}")
        expectations = dict(payload.get("expectations") or {})
        if not expectations:
            raise ValueError(f"gateway eval case has no expectations: {case_id}")
        result.append(
            GatewayEvalCase(
                case_id=case_id,
                category=str(payload.get("category") or ""),
                operation=str(payload.get("operation") or ""),
                expectations=expectations,
            )
        )
        seen.add(case_id)
    return result


def validate_gateway_golden_set(path: Path) -> JsonDict:
    cases = load_gateway_golden_set(path)
    categories = {case.category for case in cases}
    missing = sorted(REQUIRED_CATEGORIES - categories)
    return {
        "status": "passed" if not missing else "failed",
        "total_cases": len(cases),
        "passed_cases": len(cases) if not missing else 0,
        "failed_cases": len(missing),
        "summary": {
            "dataset_version": path.stem,
            "categories": sorted(categories),
            "missing_categories": missing,
        },
    }


def run_gateway_cases(
    path: Path,
    executor: Callable[[GatewayEvalCase], Mapping[str, object]],
) -> JsonDict:
    validation = validate_gateway_golden_set(path)
    if validation["status"] != "passed":
        return validation
    rows: list[JsonDict] = []
    for case in load_gateway_golden_set(path):
        started = time.perf_counter()
        try:
            actual = dict(executor(case))
            checks = {
                key: actual.get(key) == expected
                for key, expected in case.expectations.items()
            }
            failed = sorted(key for key, passed in checks.items() if not passed)
            rows.append(
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "status": "passed" if not failed else "failed",
                    "checks": checks,
                    "failed_expectations": failed,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "status": "failed",
                    "checks": {},
                    "failed_expectations": ["execution"],
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
    failed_cases = [row["case_id"] for row in rows if row["status"] != "passed"]
    return {
        "schema_version": "ConversationGatewayEval.v1",
        "status": "passed" if not failed_cases else "failed",
        "total_cases": len(rows),
        "passed_cases": len(rows) - len(failed_cases),
        "failed_cases": len(failed_cases),
        "summary": summarize_gateway_results(rows),
        "results": rows,
    }


def summarize_gateway_results(rows: Iterable[Mapping[str, object]]) -> JsonDict:
    values = list(rows)
    total = len(values)
    latencies = sorted(float(row.get("latency_ms", 0.0)) for row in values)
    p95_index = (
        max(0, min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1))
        if latencies
        else 0
    )

    def category_rate(category: str) -> float:
        selected = [row for row in values if row.get("category") == category]
        if not selected:
            return 0.0
        return sum(row.get("status") == "passed" for row in selected) / len(selected)

    return {
        "e2e_success_rate": (
            sum(row.get("status") == "passed" for row in values) / total
            if total
            else 0.0
        ),
        "authorization_isolation_rate": category_rate("authorization_isolation"),
        "idempotency_rate": category_rate("idempotency"),
        "event_resume_integrity_rate": category_rate("event_resume"),
        "terminal_delivery_rate": category_rate("terminal_repair"),
        "history_consistency_rate": category_rate("history_consistency"),
        "p95_submit_ack_ms": latencies[p95_index] if latencies else 0.0,
    }


def run_gateway_self_check(service) -> JsonDict:
    readiness = service.readiness()
    dataset = validate_gateway_golden_set(
        Path(__file__).resolve().parent / "eval_data" / "golden_v1.jsonl"
    )
    checks = {
        "gateway_ready": readiness.get("status") == "ready",
        "registry_wal": readiness.get("registry", {}).get("journal_mode") == "wal",
        "protocol_pinned": readiness.get("protocol_version") == "1.0",
        "bounded_frames": 1024 <= service.policy.max_frame_bytes <= 1024 * 1024,
        "bounded_connections": service.policy.max_connections_per_profile <= 16,
        "golden_set_complete": dataset.get("status") == "passed",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "status": "passed" if not failed else "failed",
        "total_cases": len(checks),
        "passed_cases": len(checks) - len(failed),
        "failed_cases": len(failed),
        "summary": {"checks": checks, "failed": failed, "golden_set": dataset},
    }
