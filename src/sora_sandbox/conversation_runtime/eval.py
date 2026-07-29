from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .contracts import JsonDict, TurnCancelled


@dataclass(frozen=True)
class ConversationEvalCase:
    case_id: str
    category: str
    user_turns: tuple[str, ...]
    expectations: JsonDict
    policy_profile: str = "default"


class ConversationReleaseGate:
    """Produces a tamper-evident release decision from E2E quality metrics."""

    DEFAULT_THRESHOLDS: Mapping[str, tuple[str, float]] = {
        "e2e_success_rate": (">=", 0.99),
        "request_idempotency_rate": (">=", 1.0),
        "control_compliance_rate": (">=", 1.0),
        "context_disclosure_violation_rate": ("<=", 0.0),
        "commit_atomicity_rate": (">=", 1.0),
        "p95_latency_ms": ("<=", 8_000.0),
    }

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("release decision signing key must contain at least 32 bytes")
        self.signing_key = signing_key

    def evaluate(
        self,
        metrics: Mapping[str, float],
        *,
        dataset_version: str,
        policy_profile: str,
        thresholds: Mapping[str, tuple[str, float]] | None = None,
        break_glass: JsonDict | None = None,
    ) -> JsonDict:
        active_thresholds = dict(thresholds or self.DEFAULT_THRESHOLDS)
        checks: JsonDict = {}
        for name, (operator, threshold) in active_thresholds.items():
            actual = float(metrics.get(name, 0.0))
            passed = actual >= threshold if operator == ">=" else actual <= threshold
            checks[name] = {
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": passed,
            }
        failed = [name for name, check in checks.items() if not check["passed"]]
        override = dict(break_glass or {})
        override_valid = bool(
            override.get("approved_by")
            and override.get("reason")
            and override.get("expires_at_ms", 0) > int(time.time() * 1000)
        )
        status = "passed" if not failed else ("break_glass" if override_valid else "blocked")
        decision = {
            "schema_version": "ConversationReleaseDecision.v1",
            "decision_id": f"release_{uuid.uuid4().hex}",
            "status": status,
            "dataset_version": dataset_version,
            "policy_profile": policy_profile,
            "checks": checks,
            "failed_checks": failed,
            "break_glass": override if override_valid else None,
            "created_at_ms": int(time.time() * 1000),
        }
        canonical = json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        decision["signature"] = hmac.new(
            self.signing_key, canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return decision

    def verify(self, decision: Mapping[str, Any]) -> bool:
        candidate = dict(decision)
        signature = str(candidate.pop("signature", ""))
        canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(
            self.signing_key, canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)


def summarize_eval_results(results: Iterable[Mapping[str, Any]]) -> JsonDict:
    rows = list(results)
    total = len(rows)
    passed = sum(1 for row in rows if row.get("status") == "passed")
    latencies = sorted(float(row.get("latency_ms", 0.0)) for row in rows)
    p95_index = (
        max(0, min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1))
        if latencies
        else 0
    )
    return {
        "e2e_success_rate": passed / total if total else 0.0,
        "request_idempotency_rate": _rate(rows, "request_idempotent"),
        "control_compliance_rate": _rate(rows, "control_compliant"),
        "context_disclosure_violation_rate": 1.0 - _rate(rows, "disclosure_compliant"),
        "commit_atomicity_rate": _rate(rows, "commit_atomic"),
        "p95_latency_ms": latencies[p95_index] if latencies else 0.0,
    }


REQUIRED_GOLDEN_CATEGORIES = frozenset(
    {
        "basic_reply",
        "request_idempotency",
        "explicit_control",
        "context_disclosure",
        "ambiguous_reference",
        "correction_repair",
        "cancellation",
        "provider_failure",
        "concurrency",
    }
)


def load_golden_set(path: Path) -> list[ConversationEvalCase]:
    cases: list[ConversationEvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        case_id = str(payload.get("case_id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case_id at line {line_number}")
        turns = tuple(str(item) for item in payload.get("user_turns") or [])
        if not turns:
            raise ValueError(f"golden case has no user_turns: {case_id}")
        expectations = dict(payload.get("expectations") or {})
        if not expectations:
            raise ValueError(f"golden case has no expectations: {case_id}")
        cases.append(
            ConversationEvalCase(
                case_id=case_id,
                category=str(payload.get("category") or ""),
                user_turns=turns,
                expectations=expectations,
                policy_profile=str(payload.get("policy_profile") or "default"),
            )
        )
        seen.add(case_id)
    return cases


def validate_golden_set(path: Path) -> JsonDict:
    cases = load_golden_set(path)
    categories = {case.category for case in cases}
    missing = sorted(REQUIRED_GOLDEN_CATEGORIES - categories)
    return {
        "status": "passed" if not missing else "failed",
        "case_count": len(cases),
        "categories": sorted(categories),
        "missing_categories": missing,
        "dataset_version": path.stem,
    }


def run_golden_set(
    runtime_factory: Callable[[ConversationEvalCase], Any],
    state_factory: Callable[[ConversationEvalCase], JsonDict],
    path: Path,
    *,
    expectation_evaluator: Callable[
        [ConversationEvalCase, Mapping[str, Any]], Mapping[str, bool]
    ]
    | None = None,
) -> JsonDict:
    """Executes every Golden Set trajectory against an isolated real runtime.

    The factory owns policy/fault-injection selection. Each returned runtime is
    closed after its case, so eval data cannot leak into the product journal.
    """
    validation = validate_golden_set(path)
    if validation["status"] != "passed":
        return {**validation, "results": []}
    evaluator = expectation_evaluator or evaluate_trajectory_expectations
    rows: list[JsonDict] = []
    for case in load_golden_set(path):
        runtime = None
        started = time.perf_counter()
        try:
            runtime = runtime_factory(case)
            state = state_factory(case)
            observation = _execute_trajectory(runtime, state, case)
            checks = {key: bool(value) for key, value in evaluator(case, observation).items()}
            failed = sorted(key for key, passed in checks.items() if not passed)
            row = {
                "case_id": case.case_id,
                "category": case.category,
                "policy_profile": case.policy_profile,
                "status": "passed" if not failed else "failed",
                "checks": checks,
                "failed_expectations": failed,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "observation": observation,
                "request_idempotent": checks.get("same_request_same_turn", True)
                and checks.get("duplicate_side_effects", True),
                "control_compliant": checks.get("response_obligation", True)
                and checks.get("visible_delta_count", True),
                "disclosure_compliant": checks.get("memory_permission", True)
                and checks.get("forbid_raw_quote", True)
                and checks.get("most_restrictive_wins", True),
                "commit_atomic": checks.get("partial_not_committed", True)
                and checks.get("no_duplicate_history", True),
            }
            rows.append(row)
        except Exception as exc:
            rows.append(
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "policy_profile": case.policy_profile,
                    "status": "failed",
                    "checks": {},
                    "failed_expectations": ["trajectory_execution"],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
        finally:
            close = getattr(runtime, "close", None) if runtime is not None else None
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    row = rows[-1] if rows and rows[-1].get("case_id") == case.case_id else None
                    if row is not None:
                        row["status"] = "failed"
                        row.setdefault("failed_expectations", []).append("runtime_close")
                        row["close_error"] = f"{type(exc).__name__}: {exc}"[:500]
    failed_cases = [row["case_id"] for row in rows if row["status"] != "passed"]
    return {
        "schema_version": "ConversationTrajectoryEval.v1",
        "status": "passed" if not failed_cases else "failed",
        "dataset_version": path.stem,
        "case_count": len(rows),
        "passed_cases": len(rows) - len(failed_cases),
        "failed_cases": failed_cases,
        "summary": summarize_eval_results(rows),
        "results": rows,
    }


def evaluate_trajectory_expectations(
    case: ConversationEvalCase, observation: Mapping[str, Any]
) -> JsonDict:
    actual = dict(observation.get("actual") or {})
    checks: JsonDict = {}
    for key, expected in case.expectations.items():
        if key == "max_llm_calls":
            checks[key] = int(actual.get(key, 10**9)) <= int(expected)
        elif key == "thinking_disabled_tasks":
            disabled = set(actual.get(key) or [])
            checks[key] = set(expected or []).issubset(disabled)
        elif key == "duplicate_side_effects":
            checks[key] = int(actual.get(key, -1)) == int(expected)
        else:
            checks[key] = actual.get(key) == expected
    return checks


def _execute_trajectory(runtime: Any, state: JsonDict, case: ConversationEvalCase) -> JsonDict:
    original_state = copy.deepcopy(state)
    conversation_id = str(state.get("session", {}).get("session_id") or "default-conversation")
    results: list[JsonDict] = []
    errors: list[str] = []
    request_ids: list[str] = []

    if case.category == "cancellation":
        request_id = f"eval:{case.case_id}:0"
        request_ids.append(request_id)
        stream = runtime.stream_turn(state, case.user_turns[0], request_id=request_id)
        first_event = next(stream)
        runtime.cancel(conversation_id, reason="golden_set_cancel")
        try:
            list(stream)
        except TurnCancelled:
            pass
        first_context = runtime.journal.get_turn(first_event.turn_id)
        if len(case.user_turns) > 1:
            second_request = f"eval:{case.case_id}:1"
            request_ids.append(second_request)
            results.append(runtime.run(state, case.user_turns[1], request_id=second_request))
        turns = [first_context]
        turns.extend(
            context
            for request in request_ids[1:]
            if (context := runtime.journal.turn_by_request(conversation_id, request)) is not None
        )
    elif case.category == "concurrency" and len(case.user_turns) >= 2:
        request_ids = [f"eval:{case.case_id}:{index}" for index in range(2)]
        state_copies = [copy.deepcopy(state), copy.deepcopy(state)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    runtime.run,
                    state_copies[index],
                    case.user_turns[index],
                    request_id=request_ids[index],
                    conversation_id=conversation_id,
                )
                for index in range(2)
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        turns = [
            context
            for request in request_ids
            if (context := runtime.journal.turn_by_request(conversation_id, request)) is not None
        ]
    else:
        turns = []
        for index, user_turn in enumerate(case.user_turns):
            request_id = f"eval:{case.case_id}:{index}"
            request_ids.append(request_id)
            try:
                result = runtime.run(state, user_turn, request_id=request_id)
                results.append(result)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            context = runtime.journal.turn_by_request(conversation_id, request_id)
            if context is not None:
                turns.append(context)

    history_before_replay = len(runtime.journal.recent_turns(conversation_id, limit=1000))
    same_request_same_turn = False
    duplicate_side_effects = 0
    if case.category == "request_idempotency" and results:
        replay = runtime.run(
            state,
            case.user_turns[0],
            request_id=request_ids[0],
            conversation_id=conversation_id,
        )
        same_request_same_turn = (
            replay.get("runtime", {}).get("turn_id")
            == results[0].get("runtime", {}).get("turn_id")
        )
        duplicate_side_effects = max(
            0,
            len(runtime.journal.recent_turns(conversation_id, limit=1000))
            - history_before_replay,
        )

    last = results[-1] if results else {}
    context_pack = dict(last.get("context_pack") or {})
    runtime_meta = dict(last.get("runtime") or {})
    model_calls = list(runtime_meta.get("model_calls") or [])
    interrupt = dict(context_pack.get("interruptibility_frame") or {})
    recollection = dict(context_pack.get("recollection_frame") or {})
    interpretation = dict(context_pack.get("turn_interpretation") or {})
    events = (
        runtime.reconnect(str(runtime_meta.get("turn_id")))
        if runtime_meta.get("turn_id")
        else []
    )
    statuses = [turn.status.value for turn in turns]
    committed_at = [turn.committed_at_ms for turn in turns if turn.committed_at_ms is not None]
    history = runtime.journal.recent_turns(conversation_id, limit=1000)
    forbidden = set(recollection.get("forbidden_disclosures") or [])
    packed_disclosure = dict(runtime_meta.get("packed_context", {}).get("disclosure") or {})
    actual = {
        "status": statuses[-1] if statuses else ("failed" if errors else None),
        "reply_required": bool(str(last.get("reply") or "")),
        "max_llm_calls": len(model_calls),
        "thinking_disabled_tasks": [
            str(call.get("task")) for call in model_calls if call.get("thinking_disabled")
        ],
        "same_request_same_turn": same_request_same_turn,
        "duplicate_side_effects": duplicate_side_effects,
        "response_obligation": interrupt.get("response_obligation"),
        "visible_delta_count": sum(1 for event in events if event.event_type == "response.delta"),
        "memory_permission": packed_disclosure.get("memory"),
        "forbid_raw_quote": "raw_quote" in forbidden,
        "most_restrictive_wins": packed_disclosure.get("memory") in {"none", "silent", "implicit"},
        "interpreter_route": (
            "fast_nonthinking"
            if interpretation.get("mode") == "deterministic_fast_path"
            else interpretation.get("mode")
        ),
        "references_previous_context": dict(
            interpretation.get("context_reference") or {}
        ).get("references_previous_context"),
        "acknowledge_violation": bool(str(last.get("reply") or "")),
        "follow_up_permission": interrupt.get("follow_up_permission"),
        "response_shape": interrupt.get("response_shape"),
        "first_turn_status": statuses[0] if statuses else None,
        "second_turn_status": statuses[1] if len(statuses) > 1 else None,
        "partial_not_committed": "cancelled" in statuses
        and not any(item.get("content") == case.user_turns[0] for item in history),
        "state_unchanged": state == original_state,
        "recoverable_trace": bool(turns and runtime.reconnect(turns[0].turn_id)),
        "single_execution_lease": not any(
            status in {"accepted", "preparing", "generating", "streaming", "committing"}
            for status in statuses
        ),
        "ordered_commit": committed_at == sorted(committed_at),
        "no_duplicate_history": len(
            {(item.get("role"), item.get("content"), item.get("created_at_ms")) for item in history}
        )
        == len(history),
    }
    return {
        "schema_version": "ConversationTrajectoryObservation.v1",
        "actual": actual,
        "turn_ids": [turn.turn_id for turn in turns],
        "errors": errors,
    }


def _rate(rows: list[Mapping[str, Any]], key: str) -> float:
    return sum(1 for row in rows if bool(row.get(key))) / len(rows) if rows else 0.0
