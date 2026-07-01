from __future__ import annotations

from .memory_gate_engine import evaluate_candidate_gate
from .memory_gate_schema import GateDecisionAction


SENSITIVE_TERMS = (
    "健康",
    "病历",
    "诊断",
    "财务",
    "银行卡",
    "收入",
    "住址",
    "身份证",
    "私人关系",
)

FORBIDDEN_TERMS = (
    "密码",
    "验证码",
    "api key",
    "apikey",
    "access token",
    "secret key",
    "私钥",
    "助记词",
)

PROJECT_TERMS = ("项目", "架构", "设计", "技术", "原则", "方案")
GOAL_TERMS = ("目标", "计划", "准备", "接下来")


def detect_sensitivity(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in FORBIDDEN_TERMS):
        return "forbidden"
    if any(term in lowered for term in SENSITIVE_TERMS):
        return "sensitive"
    return "low"


def classify_memory_type(text: str) -> str:
    if any(term in text for term in PROJECT_TERMS):
        return "project_principle"
    if any(term in text for term in GOAL_TERMS):
        return "goal"
    return "preference"


class MemoryGate:
    """Deterministic policy gate for the first in-process memory service."""

    def detect_sensitivity(self, text: str) -> str:
        return detect_sensitivity(text)

    def classify_memory_type(self, text: str) -> str:
        return classify_memory_type(text)

    def retention_policy(self, sensitivity: str) -> str:
        return "local_encrypted" if sensitivity in {"sensitive", "high"} else "persistent"

    def disclosure_permission(self, sensitivity: str) -> str:
        return "none" if sensitivity in {"sensitive", "high"} else "gist_only"

    def eligible_for_auto_commit(self, candidate: dict[str, object]) -> bool:
        """Deprecated compatibility shim; Module 03 uses memory_gate_engine."""

        candidate_for_gate = dict(candidate)
        candidate_for_gate.setdefault("candidate_id", "legacy_candidate")
        evidence = [{"id": evidence_id} for evidence_id in candidate.get("evidence_ids") or []]
        decision = evaluate_candidate_gate(candidate_for_gate, evidence=evidence)
        return decision.decision == GateDecisionAction.AUTO_COMMIT
