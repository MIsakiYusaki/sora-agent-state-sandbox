from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .memory_gate_schema import (
    GateDecision,
    GateDecisionAction,
    MEMORY_GATE_POLICY_VERSION,
)
from .memory_schema import JsonDict


AUTO_COMMIT_EXCLUDED_TYPES = {"relationship_signal", "shared_episode", "worldops_episode", "persona_delta"}
HIGH_RISK_SENSITIVITY = {"high", "forbidden"}
REVIEW_SENSITIVITY = {"medium", "sensitive"}


def compute_candidate_hash(candidate: JsonDict) -> str:
    operation = str(candidate.get("operation") or "ADD").upper()
    memory_type = str(candidate.get("type") or candidate.get("memory_type") or "")
    summary = _normalize_summary(str(candidate.get("summary") or ""))
    target = str(candidate.get("target_memory_id") or candidate.get("supersedes_id") or "")
    digest = hashlib.sha256()
    digest.update(f"{operation}\0{memory_type}\0{summary}\0{target}".encode("utf-8"))
    return digest.hexdigest()


def evaluate_candidate_gate(
    candidate: JsonDict,
    *,
    evidence: list[JsonDict],
    now: str | None = None,
    deferred_ttl_days: int = 30,
) -> GateDecision:
    operation = str(candidate.get("operation") or "ADD").upper()
    memory_type = str(candidate.get("type") or candidate.get("memory_type") or "")
    sensitivity = str(candidate.get("sensitivity") or "low")
    confidence = float(candidate.get("confidence") or 0.0)
    conflict_score = float(candidate.get("conflict_score") or 0.0)
    stability = float(candidate.get("stability") or 0.5)
    target_memory_id = candidate.get("target_memory_id") or candidate.get("supersedes_id")
    evidence_ids = [item["id"] for item in evidence if item.get("id")]
    candidate_hash = str(candidate.get("candidate_hash") or compute_candidate_hash(candidate))
    scores = {
        "confidence": confidence,
        "importance": float(candidate.get("importance") or 0.5),
        "stability": stability,
        "specificity": float(candidate.get("specificity") or 0.5),
        "novelty": float(candidate.get("novelty") or 1.0),
        "conflict_score": conflict_score,
    }
    risk = {
        "sensitivity": sensitivity,
        "relationship_risk": _relationship_risk(memory_type, candidate),
        "privacy_risk": "high" if sensitivity in HIGH_RISK_SENSITIVITY else ("medium" if sensitivity in REVIEW_SENSITIVITY else "low"),
        "conflict_risk": "high" if conflict_score >= 0.75 else ("medium" if conflict_score >= 0.30 else "low"),
    }
    trace = {
        "candidate_hash": candidate_hash,
        "evidence_ids": evidence_ids,
        "extraction_job_id": candidate.get("extraction_job_id"),
        "schema_version": candidate.get("schema_version"),
    }

    if sensitivity in HIGH_RISK_SENSITIVITY:
        return GateDecision(
            candidate_id=str(candidate["candidate_id"]),
            decision=GateDecisionAction.QUARANTINE,
            operation=operation,
            target_memory_id=_optional_str(target_memory_id),
            gate_score=0.0,
            reasons=["high_risk_sensitivity_quarantine"],
            risk=risk,
            scores=scores,
            trace=trace,
            quarantine=_quarantine_payload(candidate, risk_type="privacy", severity="high"),
        )

    if operation not in {"NOOP"} and not evidence_ids:
        return GateDecision(
            candidate_id=str(candidate["candidate_id"]),
            decision=GateDecisionAction.REJECT,
            operation=operation,
            target_memory_id=_optional_str(target_memory_id),
            gate_score=0.0,
            reasons=["missing_evidence"],
            risk=risk,
            scores=scores,
            trace=trace,
        )

    if confidence < 0.70:
        return GateDecision(
            candidate_id=str(candidate["candidate_id"]),
            decision=GateDecisionAction.REJECT,
            operation=operation,
            target_memory_id=_optional_str(target_memory_id),
            gate_score=confidence,
            reasons=["confidence_below_minimum"],
            risk=risk,
            scores=scores,
            trace=trace,
        )

    if stability < 0.30 and not candidate.get("explicit_user_request"):
        return GateDecision(
            candidate_id=str(candidate["candidate_id"]),
            decision=GateDecisionAction.DEFER,
            operation=operation,
            target_memory_id=_optional_str(target_memory_id),
            gate_score=confidence,
            reasons=["stability_too_low_wait_for_more_evidence"],
            risk=risk,
            scores=scores,
            trace=trace,
            deferred_until=_deferred_until(now, deferred_ttl_days),
        )

    if operation in {"UPDATE", "MERGE", "DELETE"} and not target_memory_id:
        return _review(candidate, operation, target_memory_id, risk, scores, trace, "target_memory_id_required")

    if memory_type in AUTO_COMMIT_EXCLUDED_TYPES:
        return _review(candidate, operation, target_memory_id, risk, scores, trace, "memory_type_requires_review")

    if sensitivity in REVIEW_SENSITIVITY:
        return _review(candidate, operation, target_memory_id, risk, scores, trace, "sensitivity_requires_review")

    if conflict_score >= 0.30 and operation not in {"UPDATE", "DELETE"}:
        return _review(candidate, operation, target_memory_id, risk, scores, trace, "conflict_requires_review")

    auto_commit_allowed = (
        sensitivity == "low"
        and confidence >= 0.90
        and bool(evidence_ids)
        and operation in {"ADD", "MERGE"}
        and memory_type not in AUTO_COMMIT_EXCLUDED_TYPES
        and conflict_score < 0.30
        and (
            candidate.get("explicit_user_request")
            or candidate.get("schema_version") == "extractor_result.v1"
        )
    )
    if auto_commit_allowed:
        return GateDecision(
            candidate_id=str(candidate["candidate_id"]),
            decision=GateDecisionAction.AUTO_COMMIT,
            operation=operation,
            target_memory_id=_optional_str(target_memory_id),
            gate_score=min(1.0, confidence),
            reasons=["confidence_above_auto_commit_threshold", "sensitivity_low", "evidence_present"],
            risk=risk,
            scores=scores,
            trace=trace,
        )
    return _review(candidate, operation, target_memory_id, risk, scores, trace, "default_review_required")


def _review(
    candidate: JsonDict,
    operation: str,
    target_memory_id: object,
    risk: JsonDict,
    scores: JsonDict,
    trace: JsonDict,
    reason: str,
) -> GateDecision:
    queue = "default"
    if str(candidate.get("type")) in AUTO_COMMIT_EXCLUDED_TYPES:
        queue = "relationship"
    elif risk.get("privacy_risk") in {"medium", "high"}:
        queue = "privacy"
    elif risk.get("conflict_risk") in {"medium", "high"}:
        queue = "conflict"
    return GateDecision(
        candidate_id=str(candidate["candidate_id"]),
        decision=GateDecisionAction.NEEDS_REVIEW,
        operation=operation,
        target_memory_id=_optional_str(target_memory_id),
        gate_score=float(scores.get("confidence") or 0.0),
        reasons=[reason],
        risk=risk,
        scores=scores,
        trace=trace,
        review={
            "queue": queue,
            "suggested_action": "approve" if queue == "default" else "review",
            "reviewer_visible_evidence": list(candidate.get("evidence_ids") or []),
        },
    )


def _relationship_risk(memory_type: str, candidate: JsonDict) -> str:
    if memory_type not in AUTO_COMMIT_EXCLUDED_TYPES:
        return "low"
    summary = str(candidate.get("summary") or "").lower()
    intense_terms = (
        "love",
        "dependency",
        "loyalty",
        "possessiveness",
        "only you",
        "can't live without",
        "依赖",
        "占有",
        "忠诚",
        "亲密",
        "离不开",
        "没有你不行",
        "唯一",
        "全部给你",
        "不许",
        "只能",
        "必须",
        "给钱",
        "转账",
    )
    return "high" if any(term in summary for term in intense_terms) else "medium"


def _quarantine_payload(candidate: JsonDict, *, risk_type: str, severity: str) -> JsonDict:
    return {
        "source_type": "candidate",
        "source_id": candidate["candidate_id"],
        "risk_type": risk_type,
        "severity": severity,
        "reason": "gate_quarantine",
        "redacted_summary": _redact(str(candidate.get("summary") or "")),
        "retention_policy": "delete_on_expiry",
        "status": "active",
        "policy_version": MEMORY_GATE_POLICY_VERSION,
    }


def _normalize_summary(summary: str) -> str:
    return re.sub(r"\s+", " ", summary.strip().lower())


def _redact(text: str, limit: int = 120) -> str:
    compact = _normalize_summary(text)
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _deferred_until(now: str | None, days: int) -> str:
    if now:
        base = datetime.fromisoformat(now)
    else:
        base = datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
