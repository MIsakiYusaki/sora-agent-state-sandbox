from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .memory_gate_engine import evaluate_candidate_gate
from .memory_gate_schema import GateDecision, GateDecisionAction, gate_decision_to_dict
from .memory_schema import JsonDict, json_dumps, new_id as _new_id, utc_now as _utc_now
from .memory_store import gate_decision_from_row, quarantine_from_row, review_item_from_row


def _json(value: Any) -> str:
    return json_dumps(value)


class MemoryGatePipeline:
    """Candidate admission, review queue, quarantine, and deferred TTL handling."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        get_candidate: Callable[[str], JsonDict],
        candidate_evidence: Callable[[JsonDict], list[JsonDict]],
        commit_candidates: Callable[[list[str], str], JsonDict],
        reject_candidate: Callable[[str, str], JsonDict],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._get_candidate = get_candidate
        self._candidate_evidence = candidate_evidence
        self._commit_candidates = commit_candidates
        self._reject_candidate = reject_candidate

    def run_candidate_gate(self, candidate_id: str, *, actor: str = "memory_gate") -> JsonDict:
        with self._lock:
            candidate = self._get_candidate(candidate_id)
            if candidate["status"] not in {"pending", "deferred"}:
                return {
                    "status": "skipped",
                    "candidate": candidate,
                    "reason": f"candidate_status_{candidate['status']}",
                }
            evidence = self._candidate_evidence(candidate)
            decision = evaluate_candidate_gate(candidate, evidence=evidence)
            decision_id = self._record_gate_decision(decision)
            self._conn.execute(
                "UPDATE memory_candidate SET gate_decision_id = ?, candidate_hash = ? WHERE id = ?",
                (decision_id, decision.trace["candidate_hash"], candidate_id),
            )
            if decision.decision == GateDecisionAction.AUTO_COMMIT:
                committed = self._commit_candidates([candidate_id], actor)["committed"]
                return {
                    "status": "auto_committed",
                    "decision": self.get_gate_decision(decision_id),
                    "candidate": self._get_candidate(candidate_id),
                    "committed": committed,
                }
            if decision.decision == GateDecisionAction.NEEDS_REVIEW:
                review = self._create_review_item(candidate_id, decision_id, decision)
                self._conn.execute(
                    "UPDATE memory_candidate SET status = 'needs_review' WHERE id = ?",
                    (candidate_id,),
                )
                self._conn.commit()
                return {
                    "status": "needs_review",
                    "decision": self.get_gate_decision(decision_id),
                    "candidate": self._get_candidate(candidate_id),
                    "review": review,
                }
            if decision.decision == GateDecisionAction.REJECT:
                now = _utc_now()
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'rejected', resolved_at = ?, reason = ?
                    WHERE id = ?
                    """,
                    (now, ";".join(decision.reasons), candidate_id),
                )
                self._conn.commit()
                return {
                    "status": "rejected",
                    "decision": self.get_gate_decision(decision_id),
                    "candidate": self._get_candidate(candidate_id),
                }
            if decision.decision == GateDecisionAction.QUARANTINE:
                quarantine = self._create_quarantine(candidate_id, decision)
                now = _utc_now()
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'quarantined', quarantine_id = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (quarantine["quarantine_id"], now, candidate_id),
                )
                self._conn.commit()
                return {
                    "status": "quarantined",
                    "decision": self.get_gate_decision(decision_id),
                    "candidate": self._get_candidate(candidate_id),
                    "quarantine": quarantine,
                }
            if decision.decision == GateDecisionAction.DEFER:
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'deferred', deferred_until = ?
                    WHERE id = ?
                    """,
                    (decision.deferred_until, candidate_id),
                )
                self._conn.commit()
                return {
                    "status": "deferred",
                    "decision": self.get_gate_decision(decision_id),
                    "candidate": self._get_candidate(candidate_id),
                }
        raise AssertionError("unreachable gate decision")

    def approve_review(self, review_id: str, *, actor: str = "manual_review") -> JsonDict:
        with self._lock:
            review = self.get_review_item(review_id)
            if review["status"] != "open":
                raise ValueError(f"review item is not open: {review_id}")
            committed = self._commit_candidates([review["candidate_id"]], actor)["committed"]
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE memory_review_item
                SET status = 'resolved', resolved_at = ?, resolved_by = ?,
                    resolution = 'approved'
                WHERE id = ?
                """,
                (now, actor, review_id),
            )
            self._conn.commit()
        return {
            "status": "approved",
            "review": self.get_review_item(review_id),
            "committed": committed,
        }

    def reject_review(
        self,
        review_id: str,
        *,
        reason: str = "manual_review_reject",
        actor: str = "manual_review",
    ) -> JsonDict:
        with self._lock:
            review = self.get_review_item(review_id)
            if review["status"] != "open":
                raise ValueError(f"review item is not open: {review_id}")
            candidate = self._reject_candidate(review["candidate_id"], reason)
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE memory_review_item
                SET status = 'resolved', resolved_at = ?, resolved_by = ?,
                    resolution = ?
                WHERE id = ?
                """,
                (now, actor, f"rejected:{reason}", review_id),
            )
            self._conn.commit()
        return {
            "status": "rejected",
            "review": self.get_review_item(review_id),
            "candidate": candidate,
        }

    def defer_review(
        self,
        review_id: str,
        *,
        days: int = 30,
        until: str | None = None,
        reason: str = "manual_review_defer",
        actor: str = "manual_review",
    ) -> JsonDict:
        deferred_until = until or (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        with self._lock:
            review = self.get_review_item(review_id)
            if review["status"] != "open":
                raise ValueError(f"review item is not open: {review_id}")
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE memory_candidate
                SET status = 'deferred', deferred_until = ?, resolved_at = NULL,
                    reason = ?
                WHERE id = ?
                """,
                (deferred_until, reason, review["candidate_id"]),
            )
            self._conn.execute(
                """
                UPDATE memory_review_item
                SET status = 'resolved', resolved_at = ?, resolved_by = ?,
                    resolution = ?
                WHERE id = ?
                """,
                (now, actor, f"deferred_until:{deferred_until}", review_id),
            )
            self._conn.commit()
        return {
            "status": "deferred",
            "review": self.get_review_item(review_id),
            "candidate": self._get_candidate(review["candidate_id"]),
        }

    def run_batch_gate(
        self,
        candidate_ids: list[str],
        *,
        actor: str = "memory_gate_batch",
    ) -> JsonDict:
        results = [
            self.run_candidate_gate(candidate_id, actor=actor)
            for candidate_id in candidate_ids
        ]
        return {
            "status": "ok",
            "requested_count": len(candidate_ids),
            "results": results,
            "status_counts": _count_by_status(results),
        }

    def expire_deferred_candidates(
        self,
        *,
        now: str | None = None,
        reason: str = "deferred_ttl_expired",
    ) -> JsonDict:
        cutoff = now or _utc_now()
        expired: list[JsonDict] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_candidate
                WHERE status = 'deferred'
                  AND deferred_until IS NOT NULL
                  AND deferred_until <= ?
                ORDER BY deferred_until ASC
                """,
                (cutoff,),
            ).fetchall()
            resolved_at = _utc_now()
            for row in rows:
                candidate_id = row["id"]
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'rejected', resolved_at = ?, reason = ?
                    WHERE id = ?
                    """,
                    (resolved_at, reason, candidate_id),
                )
                expired.append(self._get_candidate(candidate_id))
            self._conn.commit()
        return {
            "status": "ok",
            "expired_count": len(expired),
            "expired": expired,
        }

    def process_expired_deferrals(
        self,
        *,
        now: str | None = None,
        reason: str = "deferred_ttl_expired",
    ) -> JsonDict:
        return self.expire_deferred_candidates(now=now, reason=reason)

    def get_gate_decision(self, decision_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_gate_decision WHERE id = ?",
                (decision_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"gate decision not found: {decision_id}")
        return gate_decision_from_row(row)

    def list_gate_decisions(
        self,
        *,
        candidate_id: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        sql = "SELECT * FROM memory_gate_decision"
        params: list[Any] = []
        if candidate_id:
            sql += " WHERE candidate_id = ?"
            params.append(candidate_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [gate_decision_from_row(row) for row in rows]

    def get_review_item(self, review_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_review_item WHERE id = ?",
                (review_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"review item not found: {review_id}")
        return review_item_from_row(row)

    def list_review_items(self, *, status: str | None = "open", limit: int = 50) -> list[JsonDict]:
        sql = "SELECT * FROM memory_review_item"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [review_item_from_row(row) for row in rows]

    def get_quarantine(self, quarantine_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_quarantine WHERE id = ?",
                (quarantine_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"quarantine not found: {quarantine_id}")
        return quarantine_from_row(row)

    def list_quarantines(self, *, status: str | None = "active", limit: int = 50) -> list[JsonDict]:
        sql = "SELECT * FROM memory_quarantine"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [quarantine_from_row(row) for row in rows]

    def _record_gate_decision(self, decision: GateDecision) -> str:
        decision_id = _new_id("gdec")
        self._conn.execute(
            """
            INSERT INTO memory_gate_decision (
                id, candidate_id, decision, operation, target_memory_id,
                policy_version, gate_score, reasons_json, risk_json, scores_json,
                trace_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                decision.candidate_id,
                decision.decision.value,
                decision.operation,
                decision.target_memory_id,
                decision.policy_version,
                decision.gate_score,
                _json(decision.reasons),
                _json(decision.risk),
                _json(decision.scores),
                _json(gate_decision_to_dict(decision)["trace"]),
                _utc_now(),
            ),
        )
        return decision_id

    def _create_review_item(
        self,
        candidate_id: str,
        decision_id: str,
        decision: GateDecision,
    ) -> JsonDict:
        review = decision.review or {}
        review_id = _new_id("revq")
        self._conn.execute(
            """
            INSERT INTO memory_review_item (
                id, candidate_id, gate_decision_id, queue, status, suggested_action,
                reviewer_visible_evidence_json, reason, created_at
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                review_id,
                candidate_id,
                decision_id,
                review.get("queue", "default"),
                review.get("suggested_action", "review"),
                _json(review.get("reviewer_visible_evidence", [])),
                ";".join(decision.reasons),
                _utc_now(),
            ),
        )
        return self.get_review_item(review_id)

    def _create_quarantine(self, candidate_id: str, decision: GateDecision) -> JsonDict:
        payload = dict(decision.quarantine or {})
        quarantine_id = _new_id("qrn")
        self._conn.execute(
            """
            INSERT INTO memory_quarantine (
                id, source_type, source_id, risk_type, severity, reason,
                redacted_summary, encrypted_payload_ref, retention_policy,
                expires_at, status, policy_version, audit_trace_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quarantine_id,
                payload.get("source_type", "candidate"),
                payload.get("source_id", candidate_id),
                payload.get("risk_type", "policy"),
                payload.get("severity", "high"),
                payload.get("reason", "gate_quarantine"),
                payload.get("redacted_summary", ""),
                payload.get("encrypted_payload_ref"),
                payload.get("retention_policy", "delete_on_expiry"),
                payload.get("expires_at"),
                payload.get("status", "active"),
                payload.get("policy_version", decision.policy_version),
                payload.get("audit_trace_id"),
                _utc_now(),
            ),
        )
        return self.get_quarantine(quarantine_id)


def _count_by_status(results: list[JsonDict]) -> JsonDict:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
