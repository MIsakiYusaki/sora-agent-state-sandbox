from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..llm import LLMClient
from .contracts import PERSONA_GROWTH_CANDIDATE_CONTRACT, JsonDict


class PersonaCandidateProvider(Protocol):
    provider_name: str

    def evaluate(
        self,
        source_payload: Mapping[str, Any],
        *,
        agent_id: str,
        user_id: str | None,
        trait_definitions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        """Return a bounded growth proposal and an auditable model trace."""

    def summarize_reflection(
        self,
        snapshot: Mapping[str, Any],
        *,
        contributions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        """Return evidence-linked reflection text or None on failure."""


@dataclass
class LLMPersonaCandidateProvider:
    llm: LLMClient
    provider_name: str = "deepseek_persona_candidate_provider"

    def evaluate(
        self,
        source_payload: Mapping[str, Any],
        *,
        agent_id: str,
        user_id: str | None,
        trait_definitions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        memory = dict(source_payload.get("memory") or {})
        event_type = str(source_payload.get("event_type") or "memory_upsert")
        system = (
            "You are the bounded proposal provider for a production Persona system. "
            "You may only classify evidence and propose one bounded trait direction. "
            "Do not calculate deltas, mutate identity, infer relationship intensity, or answer the user. "
            "Return strict JSON only. If the evidence does not support durable Persona growth, return "
            '{"decision":"no_candidate","reason":"..."}.'
        )
        user = json.dumps(
            {
                "task": "evaluate_persona_growth_evidence",
                "contract": PERSONA_GROWTH_CANDIDATE_CONTRACT,
                "allowed_traits": [item["key"] for item in trait_definitions],
                "trait_definitions": trait_definitions,
                "source": {
                    "event_id": source_payload.get("event_id"),
                    "event_type": event_type,
                    "source_ref": (source_payload.get("source") or {}).get("source_ref"),
                    "memory_id": memory.get("id"),
                    "memory_type": memory.get("type"),
                    "summary": memory.get("summary"),
                    "tags": memory.get("tags") or [],
                    "confidence": memory.get("confidence"),
                    "sensitivity": memory.get("sensitivity"),
                    "evidence_ids": memory.get("evidence_ids") or [],
                    "evidence_episode_ids": memory.get("evidence_episode_ids") or [],
                    "lifecycle": source_payload.get("lifecycle") or {},
                },
                "identity_scope": {
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "agent_scope_rule": "Use agent only for evidence about Sora's durable cross-context disposition. User-specific style preference must be per_user.",
                },
                "output_schema": {
                    "decision": "candidate|no_candidate",
                    "target_scope": "agent|per_user",
                    "target_key": "one allowed trait",
                    "direction": "increase|decrease",
                    "reason": "short evidence-grounded explanation",
                    "confidence": "0..1",
                    "evidence_strength": "0..1",
                    "consistency": "0..1",
                    "generalizability": "0..1",
                    "source_reliability": "0..1",
                    "conflict_score": "0..1",
                    "risk_penalty": "0..1",
                    "explicit_feedback": "boolean",
                    "evidence_refs": ["existing evidence ids only"],
                },
                "hard_constraints": [
                    "never propose love, dependency, loyalty, possessiveness or attachment as traits",
                    "never invent evidence refs",
                    "single-user preference is not global identity evidence",
                    "uncertain or contradictory evidence should return no_candidate or high conflict_score",
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        payload, trace = self._call_json(system=system, user=user, task="persona_candidate", max_tokens=900)
        if not payload or payload.get("decision") != "candidate":
            return None, trace | {"decision": "no_candidate", "provider_reason": (payload or {}).get("reason")}
        evidence_ids = {str(item) for item in memory.get("evidence_ids") or []}
        proposed_refs = [str(item) for item in payload.get("evidence_refs") or []]
        payload["evidence_refs"] = [item for item in proposed_refs if item in evidence_ids]
        if not payload["evidence_refs"]:
            payload["evidence_refs"] = sorted(evidence_ids)
        payload.update(
            {
                "schema_version": PERSONA_GROWTH_CANDIDATE_CONTRACT,
                "agent_id": agent_id,
                "user_id": user_id,
                "source_kind": "memory_projection",
                "source_ref": str(memory.get("id") or source_payload.get("event_id") or ""),
                "sensitivity": str(memory.get("sensitivity") or "low"),
                "metadata": {
                    "projection_event_id": source_payload.get("event_id"),
                    "memory_type": memory.get("type"),
                    "model_provider": self.provider_name,
                    "episode_refs": list(memory.get("evidence_episode_ids") or []),
                    "source_revision": memory.get("updated_at") or memory.get("created_at"),
                },
            }
        )
        return payload, trace | {"decision": "candidate"}

    def summarize_reflection(
        self,
        snapshot: Mapping[str, Any],
        *,
        contributions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        evidence_refs = sorted(
            {
                str(ref)
                for item in contributions
                for ref in item.get("evidence_refs", [])
                if ref
            }
        )
        system = (
            "You write a short evidence-grounded growth journal for Sora. "
            "Every factual sentence must list evidence_refs from the supplied allowlist. "
            "Do not reveal scores, hidden reasoning, or claim feelings not present in evidence. Return strict JSON only."
        )
        user = json.dumps(
            {
                "task": "summarize_persona_growth",
                "snapshot": {
                    "version": snapshot.get("version"),
                    "trait_bands": snapshot.get("trait_bands") or {},
                },
                "contributions": contributions[-20:],
                "allowed_evidence_refs": evidence_refs,
                "output_schema": {
                    "summary": "one or two sentences",
                    "facts": [{"text": "factual sentence", "evidence_refs": ["allowed ref"]}],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        payload, trace = self._call_json(system=system, user=user, task="persona_reflection", max_tokens=500)
        if not payload or not isinstance(payload.get("facts"), list):
            return None, trace | {"validation": "missing_facts"}
        allowed = set(evidence_refs)
        for fact in payload["facts"]:
            refs = {str(item) for item in (fact or {}).get("evidence_refs") or []}
            if not refs or not refs <= allowed:
                return None, trace | {"validation": "invalid_evidence_refs"}
        return payload, trace | {"validation": "passed"}

    def _call_json(
        self,
        *,
        system: str,
        user: str,
        task: str,
        max_tokens: int,
    ) -> tuple[JsonDict | None, JsonDict]:
        started = time.perf_counter()
        result = self.llm.chat(
            system=system,
            user=user,
            workload="persona_candidate",
            temperature=0.0,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
        )
        content = str(result.get("content") or "")
        repaired = False
        try:
            payload = extract_json_object(content)
        except ValueError as first_error:
            repaired = True
            repair = self.llm.chat(
                system=(
                    "Repair the supplied malformed model output into one strict JSON object. "
                    "Preserve meaning, add no facts, and return JSON only."
                ),
                user=json.dumps({"task": "repair_json", "malformed": content[:6000]}, ensure_ascii=False),
                workload="persona_candidate",
                temperature=0.0,
                max_tokens=max_tokens,
                thinking={"type": "disabled"},
            )
            try:
                payload = extract_json_object(str(repair.get("content") or ""))
            except ValueError as repair_error:
                return None, {
                    "provider": self.provider_name,
                    "task": task,
                    "status": "invalid_json",
                    "first_error": str(first_error),
                    "repair_error": str(repair_error),
                    "repair_attempted": True,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "model": result.get("model"),
                    "usage": _merge_usage(result.get("usage"), repair.get("usage")),
                }
            result = {
                **result,
                "usage": _merge_usage(result.get("usage"), repair.get("usage")),
            }
        return payload, {
            "provider": self.provider_name,
            "task": task,
            "status": "ok",
            "repair_attempted": repaired,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "model": result.get("model"),
            "usage": dict(result.get("usage") or {}),
        }


def extract_json_object(text: str) -> JsonDict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found") from None
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("provider output is not a JSON object")
    return value


def _merge_usage(first: Any, second: Any) -> JsonDict:
    left = dict(first or {})
    right = dict(second or {})
    keys = set(left) | set(right)
    result: JsonDict = {}
    for key in keys:
        a = left.get(key, 0)
        b = right.get(key, 0)
        result[key] = (a + b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else b or a
    return result
