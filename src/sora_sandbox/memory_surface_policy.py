from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Protocol

from .memory_schema import JsonDict, RecallPlan, json_dumps


SURFACE_POLICY_VERSION = "surface_policy_v1"
DEFAULT_SURFACE_PROFILE = "companion_default"

VISIBLE_ACTIONS = {
    "implicit_continuity",
    "answer_memory_question",
    "quote_memory",
    "redacted_gist",
    "relationship_context",
    "control_surface",
}


class SurfacePolicyProvider(Protocol):
    """Extension points for optional low-temperature LLM surface helpers."""

    name: str

    def compress(self, request: JsonDict) -> JsonDict:
        """Return a compressed gist package with optional usage/cost metadata."""

    def relationship_explain(self, request: JsonDict) -> JsonDict:
        """Return a companion-safe explanation for relationship-heavy memories."""


class DeterministicSurfacePolicyProvider:
    name = "deterministic_surface_policy"

    def compress(self, request: JsonDict) -> JsonDict:
        text = str(request.get("text") or "")
        budget = max(int(request.get("token_budget") or 0), 0)
        if not budget:
            return {"text": text, "degraded": False, "usage": {}}
        return {
            "text": _truncate_for_budget(text, budget),
            "degraded": False,
            "usage": {"provider": self.name, "mode": "deterministic"},
        }

    def relationship_explain(self, request: JsonDict) -> JsonDict:
        return {
            "text": str(request.get("summary") or request.get("text") or ""),
            "degraded": False,
            "usage": {"provider": self.name, "mode": "deterministic"},
        }


@dataclass
class OpenAICompatibleSurfacePolicyProvider:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: float = 45.0
    name: str = "openai_compatible_surface_policy"
    prompt_version: str = "surface_policy_prompt.v1"
    json_repair_pass_enabled: bool = True

    @classmethod
    def from_env(cls) -> "OpenAICompatibleSurfacePolicyProvider":
        api_key = (
            os.getenv("SORA_SURFACE_API_KEY", "").strip()
            or os.getenv("SORA_LLM_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
            or os.getenv("DEEPSEEK_API_KEY", "").strip()
        )
        if not api_key:
            raise RuntimeError("surface LLM provider requires SORA_SURFACE_API_KEY, SORA_LLM_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY")
        return cls(
            api_key=api_key,
            base_url=(
                os.getenv("SORA_SURFACE_BASE_URL", "").strip()
                or os.getenv("SORA_LLM_BASE_URL", "").strip()
                or os.getenv("OPENAI_BASE_URL", "").strip()
                or "https://api.deepseek.com"
            ),
            model=(
                os.getenv("SORA_SURFACE_MODEL", "").strip()
                or os.getenv("SORA_LLM_MODEL", "").strip()
                or os.getenv("OPENAI_MODEL", "").strip()
                or "deepseek-v4-flash"
            ),
            timeout_seconds=_env_float("SORA_SURFACE_TIMEOUT_SECONDS", 45.0),
            prompt_version=os.getenv("SORA_SURFACE_PROMPT_VERSION", "surface_policy_prompt.v1"),
            json_repair_pass_enabled=os.getenv("SORA_SURFACE_JSON_REPAIR_PASS", "1") != "0",
        )

    def compress(self, request: JsonDict) -> JsonDict:
        text = str(request.get("text") or "")
        budget = max(int(request.get("token_budget") or 0), 0)
        if not text or not budget or _estimate_tokens(text) <= budget:
            return {"text": text, "degraded": False, "usage": {}, "cost": {}, "latency_ms": 0}
        payload = {
            "task": "compress_memory_gist",
            "memory_id": request.get("memory_id"),
            "token_budget": budget,
            "reason": request.get("reason"),
            "text": text,
            "constraints": [
                "Preserve the durable fact and memory_id mapping.",
                "Do not add facts not present in text.",
                "Do not expose raw evidence or quote source transcript.",
                "Return a concise natural-language gist only in compressed_text.",
            ],
        }
        try:
            result = self._chat_json(
                system=(
                    "You compress Project Sora memory gists for safe context injection. "
                    "Return JSON only: {\"compressed_text\":\"...\",\"fidelity_notes\":[\"...\"],"
                    "\"omitted_details\":[\"...\"],\"risk_flags\":[\"...\"]}."
                ),
                payload=payload,
                max_tokens=max(160, budget * 8),
            )
        except Exception as exc:
            return self._degraded_result(
                kind="compress",
                text=_truncate_for_budget(text, budget),
                error=str(exc),
                started_at=None,
            )
        compressed = str(result.get("compressed_text") or result.get("text") or "").strip()
        if not compressed:
            compressed = _truncate_for_budget(text, budget)
            result["degraded"] = True
            result["fallback"] = "empty_llm_compression"
        if _estimate_tokens(compressed) > budget:
            compressed = _truncate_for_budget(compressed, budget)
            result["truncated_after_llm"] = True
        return {
            "text": compressed,
            "degraded": bool(result.get("degraded")),
            "usage": result.get("usage", {}),
            "cost": result.get("cost", {}),
            "latency_ms": result.get("latency_ms", 0),
            "provider": self.name,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "fidelity_notes": result.get("fidelity_notes", []),
            "omitted_details": result.get("omitted_details", []),
            "risk_flags": result.get("risk_flags", []),
            "json_repair_pass": result.get("json_repair_pass", {"used": False}),
            "json_mode_retry": result.get("json_mode_retry", {"used": False}),
        }

    def relationship_explain(self, request: JsonDict) -> JsonDict:
        summary = str(request.get("summary") or request.get("text") or "")
        payload = {
            "task": "relationship_memory_explanation",
            "memory_id": request.get("memory_id"),
            "summary": summary,
            "relationship_impact": request.get("relationship_impact"),
            "sensitivity": request.get("sensitivity"),
            "constraints": [
                "Keep the explanation companion-safe and non-possessive.",
                "Do not intensify dependency, loyalty, jealousy, or exclusivity.",
                "Do not reveal raw evidence or private transcript.",
                "Return explanation as one concise gist suitable for context injection.",
            ],
        }
        try:
            result = self._chat_json(
                system=(
                    "You explain relationship-continuity memories for a companion product. "
                    "Return JSON only: {\"explanation\":\"...\",\"boundary_notes\":[\"...\"],"
                    "\"risk_flags\":[\"...\"]}."
                ),
                payload=payload,
                max_tokens=360,
            )
        except Exception as exc:
            return self._degraded_result(
                kind="relationship_explain",
                text=summary,
                error=str(exc),
                started_at=None,
            )
        explanation = str(result.get("explanation") or "").strip() or summary
        return {
            "text": explanation,
            "degraded": bool(result.get("degraded")),
            "usage": result.get("usage", {}),
            "cost": result.get("cost", {}),
            "latency_ms": result.get("latency_ms", 0),
            "provider": self.name,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "boundary_notes": result.get("boundary_notes", []),
            "risk_flags": result.get("risk_flags", []),
            "json_repair_pass": result.get("json_repair_pass", {"used": False}),
            "json_mode_retry": result.get("json_mode_retry", {"used": False}),
        }

    def _degraded_result(
        self,
        *,
        kind: str,
        text: str,
        error: str,
        started_at: float | None,
    ) -> JsonDict:
        return {
            "text": text,
            "degraded": True,
            "provider": self.name,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "usage": {},
            "cost": estimate_surface_llm_cost({}),
            "latency_ms": round((perf_counter() - started_at) * 1000, 2) if started_at else 0,
            "json_repair_pass": {"used": False},
            "json_mode_retry": {"used": False},
            "risk_flags": ["provider_degraded"],
            "error": error,
            "kind": kind,
        }

    def _chat_json(self, *, system: str, payload: JsonDict, max_tokens: int) -> JsonDict:
        started = perf_counter()
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        response = self._post_chat_completion(body)
        content = response["choices"][0]["message"].get("content") or ""
        repair_pass: JsonDict = {"used": False}
        retry_usage: JsonDict = {}
        json_mode_retry: JsonDict = {"used": False}
        try:
            result = _json_object_from_text(content)
        except (json.JSONDecodeError, ValueError) as exc:
            try:
                retry_body = dict(body)
                retry_body.pop("response_format", None)
                retry_body["messages"] = [
                    dict(message)
                    for message in body["messages"]
                ]
                retry_body["messages"][0]["content"] = (
                    str(retry_body["messages"][0]["content"])
                    + " If JSON mode is unavailable, still return exactly one JSON object and no prose."
                )
                retry_response = self._post_chat_completion(retry_body)
                retry_usage = retry_response.get("usage", {})
                retry_content = retry_response["choices"][0]["message"].get("content") or ""
                result = _json_object_from_text(retry_content)
                json_mode_retry = {
                    "used": True,
                    "error": str(exc),
                    "usage": retry_usage,
                }
            except Exception as retry_exc:
                json_mode_retry = {
                    "used": True,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "usage": retry_usage,
                }
                result = None
            if result is not None:
                usage = _combine_usage(response.get("usage", {}), retry_usage)
                result["usage"] = usage
                result["cost"] = estimate_surface_llm_cost(usage)
                result["latency_ms"] = round((perf_counter() - started) * 1000, 2)
                result["json_repair_pass"] = repair_pass
                result["json_mode_retry"] = json_mode_retry
                return result
            if not self.json_repair_pass_enabled:
                raise RuntimeError(f"surface provider returned non-json response: {content[:200]}") from exc
            repair_started = perf_counter()
            repair = self._post_chat_completion(
                {
                    "model": self.model,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Repair malformed JSON for a memory surface policy helper. "
                                "Do not add or reinterpret facts. Return one JSON object only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"error": str(exc), "malformed_output": content, "task_payload": payload},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                }
            )
            repair_content = repair["choices"][0]["message"].get("content") or ""
            result = _json_object_from_text(repair_content)
            repair_pass = {
                "used": True,
                "error": str(exc),
                "json_mode_retry": json_mode_retry,
                "usage": repair.get("usage", {}),
                "latency_ms": round((perf_counter() - repair_started) * 1000, 2),
            }
        usage = _combine_usage(
            _combine_usage(response.get("usage", {}), retry_usage),
            repair_pass.get("usage", {}),
        )
        result["usage"] = usage
        result["cost"] = estimate_surface_llm_cost(usage)
        result["latency_ms"] = round((perf_counter() - started) * 1000, 2)
        result["json_repair_pass"] = repair_pass
        result["json_mode_retry"] = json_mode_retry
        return result

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        request = urllib.request.Request(
            _chat_completions_url(self.base_url),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network/provider path.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"surface LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network/provider path.
            raise RuntimeError(f"surface LLM unavailable: {exc.reason}") from exc


@dataclass(frozen=True)
class SurfacePolicySelection:
    candidate: Any
    action: str
    legacy_action: str
    reason: str
    priority: float
    may_quote: bool = False
    may_disclose_gist: bool = True
    forbidden_disclosures: tuple[str, ...] = ("raw_evidence",)
    selected: bool = True
    suppression_reason: str | None = None

    @property
    def memory_id(self) -> str:
        return str(self.candidate.memory.get("id"))

    def pack_policy(self) -> JsonDict:
        return {
            "action": self.action,
            "legacy_action": self.legacy_action,
            "reason": self.reason,
            "priority": round(self.priority, 4),
            "may_quote": self.may_quote,
            "may_disclose_gist": self.may_disclose_gist,
            "forbidden_disclosures": list(self.forbidden_disclosures),
        }

    def trace_item(self, *, rank: int) -> JsonDict:
        return {
            "memory_id": self.memory_id,
            "rank": rank,
            "score": float(getattr(self.candidate, "score", 0.0) or 0.0),
            "priority": round(self.priority, 4),
            "action": self.action,
            "legacy_action": self.legacy_action,
            "selected": self.selected,
            "reason": self.reason,
            "suppression_reason": self.suppression_reason,
            "policy": self.pack_policy(),
        }


@dataclass(frozen=True)
class SurfacePolicyDecision:
    action: str
    legacy_action: str
    decision_reason: str
    selections: list[SurfacePolicySelection] = field(default_factory=list)
    suppressed: list[SurfacePolicySelection] = field(default_factory=list)
    forbidden_disclosures: tuple[str, ...] = ("raw_evidence",)
    consent_request: JsonDict | None = None
    policy_version: str = SURFACE_POLICY_VERSION
    policy_profile: str = DEFAULT_SURFACE_PROFILE
    provider_name: str = "deterministic_surface_policy"

    @property
    def selected_candidates(self) -> list[Any]:
        return [item.candidate for item in self.selections]

    @property
    def policy_by_memory_id(self) -> dict[str, JsonDict]:
        return {item.memory_id: item.pack_policy() for item in self.selections}

    @property
    def suppression_rows(self) -> list[JsonDict]:
        return [
            {
                "memory_id": item.memory_id,
                "reason": item.suppression_reason or item.reason,
                "action": item.action,
            }
            for item in self.suppressed
        ]

    def trace_items(self) -> list[JsonDict]:
        rows: list[JsonDict] = []
        rank = 1
        for item in self.selections:
            rows.append(item.trace_item(rank=rank))
            rank += 1
        for item in self.suppressed:
            rows.append(item.trace_item(rank=rank))
            rank += 1
        return rows

    def public_dict(self) -> JsonDict:
        return {
            "version": self.policy_version,
            "profile": self.policy_profile,
            "provider": self.provider_name,
            "surface_action": self.action,
            "legacy_surface_action": self.legacy_action,
            "decision_reason": self.decision_reason,
            "forbidden_disclosures": list(self.forbidden_disclosures),
            "consent_request": self.consent_request,
            "selected_ids": [item.memory_id for item in self.selections],
            "suppressed": self.suppression_rows,
        }


class SurfacePolicyEngine:
    def __init__(
        self,
        *,
        provider: SurfacePolicyProvider | None = None,
        policy_profile: str = DEFAULT_SURFACE_PROFILE,
        policy_version: str = SURFACE_POLICY_VERSION,
    ) -> None:
        self.provider = provider or DeterministicSurfacePolicyProvider()
        self.policy_profile = policy_profile
        self.policy_version = policy_version

    def decide(
        self,
        plan: RecallPlan,
        candidates: list[Any],
        *,
        token_budget: int,
        context_use_policy: JsonDict | None = None,
        turn_frame: JsonDict | None = None,
        limit: int | None = None,
        now: str | None = None,
    ) -> SurfacePolicyDecision:
        timestamp = now or _utc_now()
        selected: list[SurfacePolicySelection] = []
        suppressed: list[SurfacePolicySelection] = []
        consent_candidates: list[SurfacePolicySelection] = []
        for candidate in candidates:
            selection = self._classify_candidate(
                plan,
                candidate,
                token_budget=token_budget,
                context_use_policy=context_use_policy or {},
                turn_frame=turn_frame or {},
                now=timestamp,
            )
            if selection.selected:
                selected.append(selection)
            else:
                suppressed.append(selection)
                if selection.action == "ask_consent":
                    consent_candidates.append(selection)

        selected.sort(
            key=lambda item: (
                round(item.priority, 2),
                float(getattr(item.candidate, "score", 0.0) or 0.0),
            ),
            reverse=True,
        )

        if limit is not None:
            overflow = selected[limit:]
            selected = selected[:limit]
            suppressed.extend(
                SurfacePolicySelection(
                    candidate=item.candidate,
                    action="suppress",
                    legacy_action="none",
                    reason="rank_limit",
                    priority=item.priority,
                    selected=False,
                    suppression_reason="rank_limit",
                )
                for item in overflow
            )

        action = self._overall_action(plan, selected, consent_candidates)
        legacy_action = legacy_surface_action(action)
        consent_request = self._consent_request(consent_candidates, plan) if consent_candidates else None
        if selected:
            reason = self._decision_reason(action, selected)
        elif consent_request:
            reason = "memory available but consent is required before visible use"
        else:
            reason = "no memories passed surface policy"
        return SurfacePolicyDecision(
            action=action,
            legacy_action=legacy_action,
            decision_reason=reason,
            selections=selected,
            suppressed=suppressed,
            forbidden_disclosures=tuple(sorted(self._forbidden_disclosures(selected))),
            consent_request=consent_request,
            policy_version=self.policy_version,
            policy_profile=self.policy_profile,
            provider_name=self.provider.name,
        )

    def _classify_candidate(
        self,
        plan: RecallPlan,
        candidate: Any,
        *,
        token_budget: int,
        context_use_policy: JsonDict,
        turn_frame: JsonDict,
        now: str,
    ) -> SurfacePolicySelection:
        memory = candidate.memory
        base_action = self._base_action(plan, memory)
        permission = str(memory.get("surface_permission") or "auto")
        disclosure = str(memory.get("disclosure_permission") or "gist_only")
        use_permission = str(memory.get("use_permission") or "allow_implicit")
        sensitivity = str(memory.get("sensitivity") or "low")
        reason_parts: list[str] = []
        feedback = memory.get("surface_feedback") if isinstance(memory.get("surface_feedback"), dict) else {}
        consent = memory.get("surface_consent") if isinstance(memory.get("surface_consent"), dict) else {}
        has_consent = bool(consent.get("granted"))
        feedback_score = float(feedback.get("score") or 0.0)

        if use_permission == "deny":
            return _suppressed(candidate, "use_denied")
        if feedback_score <= -0.75 and not plan.explicit_recall:
            return _suppressed(candidate, "negative_surface_feedback")
        if permission in {"none", "hidden", "deny"}:
            return _suppressed(candidate, "surface_override_hidden")
        if permission == "silent_only":
            base_action = "silent_use"
            reason_parts.append("surface_override_silent_only")
        if permission in {"ask", "always_ask"}:
            return _consent(candidate, "surface_override_requires_consent")
        if _cooldown_active(memory.get("surface_cooldown_until"), now) and not plan.explicit_recall:
            return _suppressed(candidate, "surface_cooldown_active")
        if disclosure in {"requires_consent", "consent"} and not has_consent:
            return _consent(candidate, "disclosure_requires_consent")
        if disclosure in {"none", "deny"}:
            if plan.explicit_recall:
                return _consent(candidate, "disclosure_denied_for_visible_answer")
            return _suppressed(candidate, "disclosure_denied")
        if disclosure == "silent_only" and plan.explicit_recall and not has_consent:
            return _consent(candidate, "silent_only_requires_consent_for_answer")
        if disclosure == "silent_only":
            if has_consent and plan.explicit_recall:
                base_action = "answer_memory_question"
                reason_parts.append("silent_only_user_consent_granted")
            else:
                base_action = "silent_use"
                reason_parts.append("disclosure_silent_only")
        if base_action == "quote_memory" and disclosure not in {"allow", "quote_ok"}:
            base_action = "answer_memory_question"
            reason_parts.append("quote_not_permitted_use_gist")
        if sensitivity in {"sensitive", "high"} and base_action in {"answer_memory_question", "quote_memory"}:
            base_action = "redacted_gist"
            reason_parts.append("sensitive_visible_use_redacted")
        if feedback_score < 0:
            reason_parts.append("negative_surface_feedback_priority_penalty")
        elif feedback_score > 0:
            reason_parts.append("positive_surface_feedback_priority_boost")

        priority = _priority(candidate, memory, base_action, plan=plan)
        reason_parts.append(f"surface_action={base_action}")
        may_quote = base_action == "quote_memory" and disclosure in {"allow", "quote_ok"}
        may_disclose_gist = base_action not in {"silent_use", "suppress", "ask_consent"}
        forbidden = ["raw_evidence"]
        if not may_quote:
            forbidden.append("verbatim_quote")
        if base_action == "silent_use":
            forbidden.append("direct_mention")
        return SurfacePolicySelection(
            candidate=candidate,
            action=base_action,
            legacy_action=legacy_surface_action(base_action),
            reason=", ".join(reason_parts),
            priority=priority,
            may_quote=may_quote,
            may_disclose_gist=may_disclose_gist,
            forbidden_disclosures=tuple(sorted(set(forbidden))),
        )

    def _base_action(self, plan: RecallPlan, memory: JsonDict) -> str:
        if not plan.explicit_recall:
            if plan.query_intent == "relationship_context":
                return "relationship_context"
            return "implicit_continuity"
        if plan.surface_hint == "strict_gist":
            return "answer_memory_question"
        if _quote_requested(plan.query_text):
            return "quote_memory"
        return "answer_memory_question"

    def _overall_action(
        self,
        plan: RecallPlan,
        selected: list[SurfacePolicySelection],
        consent_candidates: list[SurfacePolicySelection],
    ) -> str:
        if consent_candidates and not selected:
            return "ask_consent"
        if not selected:
            return "none"
        order = [
            "quote_memory",
            "redacted_gist",
            "answer_memory_question",
            "relationship_context",
            "implicit_continuity",
            "silent_use",
        ]
        present = {item.action for item in selected}
        for action in order:
            if action in present:
                return action
        return "implicit_continuity" if not plan.explicit_recall else "answer_memory_question"

    def _decision_reason(self, action: str, selected: list[SurfacePolicySelection]) -> str:
        if action == "answer_memory_question":
            return "explicit recall request with governed memory answer"
        if action == "quote_memory":
            return "explicit quote request with quote-permitted memories"
        if action == "redacted_gist":
            return "explicit recall request with redacted memory gist"
        if action == "relationship_context":
            return "relationship context used with companion-aware surface policy"
        if action == "silent_use":
            return "memory used silently under disclosure policy"
        if selected:
            return "relevant canonical memories used for continuity"
        return "no memories passed surface policy"

    def _consent_request(
        self,
        consent_candidates: list[SurfacePolicySelection],
        plan: RecallPlan,
    ) -> JsonDict:
        first = consent_candidates[0]
        return {
            "type": "memory_disclosure",
            "memory_ids": [item.memory_id for item in consent_candidates],
            "reason": first.reason,
            "query_text": plan.query_text,
            "prompt": "Ask whether the user wants this memory used visibly in the answer.",
        }

    def _forbidden_disclosures(self, selected: list[SurfacePolicySelection]) -> set[str]:
        if not selected:
            return set()
        forbidden: set[str] = set()
        for item in selected:
            forbidden.update(item.forbidden_disclosures)
        return forbidden


def legacy_surface_action(action: str) -> str:
    if action in {"answer_memory_question", "quote_memory", "redacted_gist"}:
        return "gist"
    if action in {"implicit_continuity", "silent_use", "relationship_context", "control_surface"}:
        return "implicit"
    return "none"


def surface_decision_for_trace(decision: SurfacePolicyDecision) -> JsonDict:
    return {
        "policy": decision.public_dict(),
        "items": decision.trace_items(),
        "serialized_size": len(json_dumps(decision.public_dict())),
    }


def create_surface_policy_from_env() -> SurfacePolicyEngine:
    mode = (
        os.getenv("SORA_SURFACE_POLICY_PROVIDER", "").strip().lower()
        or os.getenv("SORA_MEMORY_SURFACE_PROVIDER", "").strip().lower()
        or os.getenv("SORA_SURFACE_PROVIDER", "").strip().lower()
        or "deterministic"
    )
    if mode in {"llm", "openai", "openai_compatible", "deepseek"}:
        return SurfacePolicyEngine(provider=OpenAICompatibleSurfacePolicyProvider.from_env())
    if mode in {"deterministic", "rules", "local", "off"}:
        return SurfacePolicyEngine(provider=DeterministicSurfacePolicyProvider())
    raise RuntimeError(f"unknown SORA_SURFACE_POLICY_PROVIDER: {mode}")


def estimate_surface_llm_cost(usage: JsonDict) -> JsonDict:
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
    input_cost_per_1k = float(os.environ.get("SORA_SURFACE_INPUT_COST_PER_1K") or os.environ.get("SORA_LLM_INPUT_COST_PER_1K", "0") or 0)
    output_cost_per_1k = float(os.environ.get("SORA_SURFACE_OUTPUT_COST_PER_1K") or os.environ.get("SORA_LLM_OUTPUT_COST_PER_1K", "0") or 0)
    estimated = (prompt_tokens / 1000.0 * input_cost_per_1k) + (
        completion_tokens / 1000.0 * output_cost_per_1k
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_cost_per_1k": input_cost_per_1k,
        "output_cost_per_1k": output_cost_per_1k,
        "estimated_cost": round(estimated, 8),
        "currency": os.environ.get("SORA_SURFACE_COST_CURRENCY") or os.environ.get("SORA_LLM_COST_CURRENCY", "USD"),
    }


def _suppressed(candidate: Any, reason: str) -> SurfacePolicySelection:
    return SurfacePolicySelection(
        candidate=candidate,
        action="suppress",
        legacy_action="none",
        reason=reason,
        priority=_priority(candidate, candidate.memory, "suppress", plan=None),
        selected=False,
        suppression_reason=reason,
        may_disclose_gist=False,
        forbidden_disclosures=("direct_mention", "raw_evidence", "verbatim_quote"),
    )


def _consent(candidate: Any, reason: str) -> SurfacePolicySelection:
    return SurfacePolicySelection(
        candidate=candidate,
        action="ask_consent",
        legacy_action="none",
        reason=reason,
        priority=_priority(candidate, candidate.memory, "ask_consent", plan=None),
        selected=False,
        suppression_reason=reason,
        may_disclose_gist=False,
        forbidden_disclosures=("direct_mention", "raw_evidence", "verbatim_quote"),
    )


def _priority(candidate: Any, memory: JsonDict, action: str, *, plan: RecallPlan | None) -> float:
    score = float(getattr(candidate, "score", 0.0) or 0.0)
    confidence = float(memory.get("confidence") or 0.0)
    importance = float(memory.get("importance") or 0.0)
    relationship = float(memory.get("relationship_impact") or 0.0)
    feedback = memory.get("surface_feedback") if isinstance(memory.get("surface_feedback"), dict) else {}
    feedback_score = float(feedback.get("score") or 0.0)
    sensitivity = str(memory.get("sensitivity") or "low")
    risk_penalty = 0.08 if sensitivity in {"sensitive", "high"} else 0.0
    action_boost = {
        "answer_memory_question": 0.06,
        "quote_memory": 0.04,
        "relationship_context": 0.04,
        "implicit_continuity": 0.02,
        "silent_use": -0.02,
        "ask_consent": -0.04,
        "suppress": -1.0,
    }.get(action, 0.0)
    explicit_boost = 0.05 if plan is not None and plan.explicit_recall else 0.0
    return max(
        0.0,
        score
        + confidence * 0.08
        + importance * 0.12
        + relationship * 0.08
        + feedback_score * 0.12
        + action_boost
        + explicit_boost
        - risk_penalty,
    )


def _cooldown_active(value: Any, now: str) -> bool:
    if not value:
        return False
    try:
        cooldown = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return False
    if cooldown.tzinfo is None:
        cooldown = cooldown.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return cooldown > current


def _quote_requested(text: str) -> bool:
    lowered = text.lower()
    return any(
        term in lowered
        for term in (
            "quote",
            "verbatim",
            "read back",
            "引用",
            "原话",
            "逐字",
            "原文",
        )
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    ascii_chars = sum(1 for char in stripped if ord(char) < 128)
    non_ascii_chars = len(stripped) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def _truncate_for_budget(text: str, budget: int) -> str:
    if _estimate_tokens(text) <= budget:
        return text
    limit = max(16, budget * 2)
    return text[:limit].rstrip() + "..."


def _json_object_from_text(text: str) -> JsonDict:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty json")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("expected json object")
    return payload


def _combine_usage(primary: JsonDict, repair: JsonDict) -> JsonDict:
    if not repair:
        return primary
    prompt_tokens = int(primary.get("prompt_tokens") or primary.get("input_tokens") or 0) + int(
        repair.get("prompt_tokens") or repair.get("input_tokens") or 0
    )
    completion_tokens = int(primary.get("completion_tokens") or primary.get("output_tokens") or 0) + int(
        repair.get("completion_tokens") or repair.get("output_tokens") or 0
    )
    total_tokens = int(primary.get("total_tokens") or 0) + int(repair.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        **primary,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "repair_prompt_tokens": int(repair.get("prompt_tokens") or repair.get("input_tokens") or 0),
        "repair_completion_tokens": int(repair.get("completion_tokens") or repair.get("output_tokens") or 0),
    }


def _chat_completions_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/chat/completions"):
        return clean
    return f"{clean}/chat/completions"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default
