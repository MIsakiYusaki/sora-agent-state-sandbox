"""Bounded, content-free Memory job observations; never stores provider payloads."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from .turn_observability import TurnEvidence, observe_call


def error_code(value: Any) -> str | None:
    if not value:
        return None
    allowed = {"worker_lease_expired", "extractor_error:ExtractorValidationError",
               "extractor_error:ExtractionSourceUnavailable", "extractor_error:RuntimeError",
               "extractor_error:ValueError", "extractor_error:KeyError",
               "extractor_error:TypeError", "extractor_error:OperationalError",
               "extractor_error:IntegrityError", "extractor_error:TimeoutError",
               "extractor_error:JSONDecodeError"}
    return value if isinstance(value, str) and value in allowed else "unclassified_error"


def at_ms(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() * 1000 if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def memory_job_observer(runtime):
    with runtime._lock:
        observer = getattr(runtime, "_memory_job_observer", None)
        if observer is None:
            observer = MemoryJobObserver(runtime)
            runtime._memory_job_observer = observer
        return observer


class MemoryJobObserver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.active: dict[tuple[str, int], TurnEvidence] = {}
        self.write_failures = 0

    def run(self, job, function, **kwargs):
        key = (job["job_id"], job["attempts"])
        evidence = TurnEvidence(lane="memory_worker", limit=128)
        with self.runtime._lock:
            self.active[key] = evidence
        self._persist(key, evidence, "running", None, final=False)
        outcome, error = "interrupted", None
        try:
            with evidence.activate():
                result = observe_call("memory.extractor.attempt", function, job, **kwargs)
            outcome = result.get("status", "unknown")
            error = error_code(result.get("error"))
            return result
        finally:
            self._persist(key, evidence, outcome, error, final=True)
            with self.runtime._lock:
                self.active.pop(key, None)

    def _persist(self, key, evidence, outcome, error, *, final):
        # Diagnostics must not roll back or turn a completed canonical write
        # into a retry. Never join a caller's transaction or persist raw errors.
        with self.runtime._lock:
            conn = self.runtime._conn
            if conn.in_transaction:
                self.write_failures += 1
                return
            try:
                conn.execute("""INSERT INTO memory_extraction_attempt
                    (job_id, attempt, started_at_ms, ended_at_ms, outcome, error_code, execution_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, attempt) DO UPDATE SET ended_at_ms=excluded.ended_at_ms,
                    outcome=excluded.outcome, error_code=excluded.error_code, execution_json=excluded.execution_json
                """, (*key, evidence.anchor_at_ms, time.time_ns() / 1_000_000 if final else None,
                      outcome, error, json.dumps(evidence.snapshot(final=final))))
                conn.execute("DELETE FROM memory_extraction_attempt WHERE started_at_ms < ?",
                             (time.time_ns() / 1_000_000 - 7 * 86400_000,))
                conn.commit()
            except Exception:
                conn.rollback()
                self.write_failures += 1

    def snapshot(self, conversation_id: str, turn_ids: list[str]) -> dict[str, Any]:
        # Identity is bound by the Memory runtime; caller must also authenticate
        # conversation access at the Gateway. Limit input and SQL result size.
        ids = list(dict.fromkeys(turn_ids))[:20]
        if not ids:
            return {"status": "available", "jobs": [], "truncated": False}
        with self.runtime._lock:
            records = self.runtime._conn.execute(f"""
                SELECT j.id AS job_id, j.status, j.attempts, j.created_at, j.available_at,
                       j.claimed_at, j.completed_at, j.error, r.turn_id, r.correlation_id,
                       r.status AS receipt_status,
                       EXISTS(SELECT 1 FROM memory_extraction_checkpoint c WHERE c.job_id=j.id) AS checkpoint
                FROM extraction_job j JOIN memory_turn_receipt r ON r.episode_id=j.episode_id
                WHERE r.owner_user_id=? AND r.agent_id=? AND r.conversation_id=?
                  AND r.turn_id IN ({','.join('?' for _ in ids)})
                ORDER BY j.created_at DESC LIMIT 101
            """, (self.runtime.owner_user_id, self.runtime.agent_id, conversation_id, *ids)).fetchall()
            jobs = []
            for record in records[:100]:
                job = dict(record)
                available = at_ms(job["available_at"])
                waiting = None
                if job["status"] == "pending":
                    waiting = ("ingestion_pending" if job["receipt_status"] != "completed"
                               else "retry_backoff" if available and available > time.time_ns() / 1_000_000
                               else "queued")
                attempts = []
                for attempt in self.runtime._conn.execute("""
                    SELECT * FROM memory_extraction_attempt WHERE job_id=? AND started_at_ms>=?
                    ORDER BY attempt DESC LIMIT 10
                """, (job["job_id"], time.time_ns() / 1_000_000 - 7 * 86400_000)).fetchall():
                    active = self.active.get((job["job_id"], attempt["attempt"]))
                    execution = active.snapshot() if active else json.loads(attempt["execution_json"])
                    if not active and execution.get("snapshot_state") == "live":
                        execution["snapshot_state"] = "final"
                        for span in execution.get("spans", []):
                            if span["status"] == "running":
                                span["status"] = "end_unobserved"
                    attempts.append({"attempt": attempt["attempt"],
                                     "outcome": attempt["outcome"] if active or attempt["ended_at_ms"] else "end_unobserved",
                                     "error_code": error_code(attempt["error_code"]), "execution": execution})
                jobs.append({"job_id": job["job_id"], "turn_id": job["turn_id"],
                             "correlation_id": job["correlation_id"], "status": job["status"],
                             "attempt_count": job["attempts"], "wait_reason": waiting,
                             "created_at_ms": at_ms(job["created_at"]), "available_at_ms": available,
                             "claimed_at_ms": at_ms(job["claimed_at"]), "completed_at_ms": at_ms(job["completed_at"]),
                             "error_code": error_code(job["error"]), "checkpoint_available": bool(job["checkpoint"]),
                             "attempts": attempts, "attempts_truncated": len(attempts) < job["attempts"]})
            return {"status": "available", "jobs": jobs, "truncated": len(records) > 100,
                    "observation_write_failures": self.write_failures}
