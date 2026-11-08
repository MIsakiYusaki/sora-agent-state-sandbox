from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import replace
from time import perf_counter
from typing import Protocol

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict, RecallBudget, RecallPlan, RecallSubquery
from .turn_observability import observe_http


RECALL_PLANNER_VERSION = "recall_planner_v2"

EXPLICIT_RECALL_TERMS = (
    "remember",
    "do you recall",
    "last time",
    "we said",
    "you said",
    "记得",
    "记不记得",
    "之前",
    "上次",
    "回忆",
    "记忆",
    "说过",
    "提过",
)
DOMAIN_CONTINUITY_TERMS = (
    "project",
    "plan",
    "workflow",
    "architecture",
    "design",
    "implementation",
    "principle",
    "strategy",
    "research",
    "writing",
    "study",
    "creative",
    "business",
    "next step",
    "项目",
    "计划",
    "工作",
    "学习",
    "研究",
    "创作",
    "方案",
    "架构",
    "设计",
    "原则",
    "流程",
    "策略",
    "下一步",
)
RELATIONSHIP_TERMS = (
    "relationship",
    "companionship",
    "closeness",
    "loyalty",
    "dependency",
    "love",
    "together",
    "陪伴",
    "亲密",
    "关系",
    "依赖",
    "忠诚",
    "占有",
    "喜欢我",
    "我们一起",
)
PREFERENCE_TERMS = (
    "prefer",
    "preference",
    "like",
    "dislike",
    "habit",
    "style",
    "喜欢",
    "讨厌",
    "偏好",
    "习惯",
    "风格",
)
EPISODE_TERMS = (
    "episode",
    "shared",
    "together",
    "last time",
    "那次",
    "一起",
    "共同",
    "经历",
    "上次",
)
WORLD_TERMS = (
    "world",
    "state",
    "desktop",
    "environment",
    "世界",
    "状态",
    "环境",
    "桌面",
)
DEICTIC_TERMS = (
    "that",
    "this",
    "it",
    "那个",
    "这个",
    "这件事",
    "那件事",
    "刚才",
    "继续",
)

QUOTED_TERM_RE = re.compile(r"[\"'“”‘’](?P<term>[^\"'“”‘’]{1,60})[\"'“”‘’]")
TOPIC_PATTERNS = (
    re.compile(r"about\s+(?P<term>[A-Za-z0-9_. -]{1,60})", re.IGNORECASE),
    re.compile(r"regarding\s+(?P<term>[A-Za-z0-9_. -]{1,60})", re.IGNORECASE),
    re.compile(r"for\s+(?P<term>[A-Za-z0-9_. -]{1,60})", re.IGNORECASE),
    re.compile(r"关于(?P<term>[^，。！？?]{1,60})"),
    re.compile(r"推进(?P<term>[^，。！？?]{1,40})"),
    re.compile(r"设计(?P<term>[^，。！？?]{1,40})"),
    re.compile(r"我的(?P<term>[^，。！？?]{1,40})"),
)


class RecallPlanner(Protocol):
    def plan(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None,
        context_use_policy: JsonDict | None,
        top_k: int,
        planner_context: JsonDict | None = None,
    ) -> RecallPlan:
        ...


class RecallPlannerProvider(Protocol):
    def plan(self, input_package: JsonDict) -> JsonDict:
        ...


class RuleRecallPlanner:
    def plan(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None,
        context_use_policy: JsonDict | None,
        top_k: int,
        planner_context: JsonDict | None = None,
    ) -> RecallPlan:
        policy = context_use_policy or {}
        context = planner_context or {}
        mode = str(context.get("mode") or "response_generation")
        scene = _scene_from_turn_frame(turn_frame)
        blocked = set(policy.get("blocked_context_sources", []))
        if "memory_recollection" in blocked:
            return _skip_plan(
                query_text,
                top_k=top_k,
                scene=scene,
                mode="rules",
                reason="memory_recollection_blocked_by_context_policy",
                decision_reasons=["context_use_policy blocked memory_recollection"],
            )
        if not query_text.strip() and mode != "prefetch":
            return _skip_plan(
                query_text,
                top_k=top_k,
                scene=scene,
                mode="rules",
                reason="empty_or_proactive_query",
                decision_reasons=["empty query has no stable recall target"],
            )

        explicit = is_explicit_recall(query_text)
        intent = classify_query_intent(query_text, mode=mode, turn_frame=turn_frame)
        decision = decide_recall_depth(
            query_text,
            mode=mode,
            intent=intent,
            explicit=explicit,
            turn_frame=turn_frame,
        )
        budget = allocate_budget(decision, explicit=explicit, requested=context)
        salient_terms = extract_salient_terms(query_text, turn_frame=turn_frame)
        subqueries = build_subqueries(
            query_text,
            intent=intent,
            explicit=explicit,
            top_k=top_k,
            salient_terms=salient_terms,
            turn_frame=turn_frame,
        )
        if decision == "skip":
            return _skip_plan(
                query_text,
                top_k=top_k,
                scene=scene,
                mode="rules",
                reason="planner_decision_skip",
                decision_reasons=["query classified as not needing memory"],
            )

        backends = _union_backends(subqueries) or choose_backends(query_text, intent=intent, explicit_recall=explicit)
        tokens = _tokens_for_subqueries(subqueries) or search_tokens(" ".join(salient_terms) if salient_terms else query_text)
        return RecallPlan(
            query_text=query_text,
            top_k=top_k,
            tokens=tokens,
            salient_terms=salient_terms,
            explicit_recall=explicit,
            skipped_reason=None,
            backends=backends,
            service_budget_ms=budget.service_budget_ms,
            planner_mode="rules",
            planner_version=RECALL_PLANNER_VERSION,
            decision=decision,
            query_intent=intent,
            scene=scene,
            subqueries=subqueries,
            budget=budget,
            surface_hint=surface_hint(intent, explicit=explicit, decision=decision),
            fallbacks=["rules_fast_path", "fts_vector_only", "skip_with_trace"],
            decision_reasons=decision_reasons_for(
                query_text,
                mode=mode,
                intent=intent,
                explicit=explicit,
                decision=decision,
                turn_frame=turn_frame,
            ),
            planner_trace={
                "mode": mode,
                "policy_risk_mode": policy.get("risk_mode", "normal"),
                "rule_features": rule_features(query_text, turn_frame=turn_frame),
            },
            prefetch={"cacheable": mode == "prefetch", "ttl_seconds": 600} if decision == "prefetch_only" else {},
        )


class HybridRecallPlanner:
    def __init__(
        self,
        rule_planner: RecallPlanner | None = None,
        llm_provider: RecallPlannerProvider | None = None,
        *,
        enable_llm: bool = False,
    ) -> None:
        self.rule_planner = rule_planner or RuleRecallPlanner()
        self.llm_provider = llm_provider
        self.enable_llm = enable_llm

    def plan(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None,
        context_use_policy: JsonDict | None,
        top_k: int,
        planner_context: JsonDict | None = None,
    ) -> RecallPlan:
        rule_plan = self.rule_planner.plan(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            top_k=top_k,
            planner_context=planner_context,
        )
        if not self.enable_llm or self.llm_provider is None:
            return replace(rule_plan, planner_mode="hybrid_rules")
        if rule_plan.skipped_reason or not _needs_llm_planner(rule_plan, query_text, turn_frame):
            return replace(rule_plan, planner_mode="hybrid_rules")

        started = perf_counter()
        try:
            payload = self.llm_provider.plan(
                {
                    "task": "plan_domain_neutral_memory_recall",
                    "schema_version": RECALL_PLANNER_VERSION,
                    "query_text": query_text,
                    "turn_frame": turn_frame or {},
                    "context_use_policy": context_use_policy or {},
                    "planner_context": planner_context or {},
                    "rule_plan": plan_to_json(rule_plan),
                }
            )
            llm_plan = plan_from_llm_json(payload, fallback=rule_plan)
            trace = {
                **rule_plan.planner_trace,
                "llm_planner": {
                    "used": True,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                    "model": payload.get("model", {}),
                    "usage": payload.get("usage", {}),
                    "repairs": payload.get("repairs", []),
                },
            }
            return replace(
                llm_plan,
                planner_mode="hybrid_llm",
                planner_trace=trace,
                llm_usage=payload.get("usage", {}),
            )
        except Exception as exc:
            trace = {
                **rule_plan.planner_trace,
                "llm_planner": {
                    "used": False,
                    "degraded": True,
                    "error": str(exc),
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                },
            }
            return replace(
                rule_plan,
                planner_mode="hybrid_rules_fallback",
                planner_trace=trace,
                decision_reasons=list(rule_plan.decision_reasons) + ["llm planner failed; used rule fallback"],
            )


class OpenAICompatibleRecallPlannerProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: int = 45,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "OpenAICompatibleRecallPlannerProvider":
        api_key = (
            os.environ.get("SORA_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        )
        if not api_key:
            raise RuntimeError("missing SORA_LLM_API_KEY/OPENAI_API_KEY/DEEPSEEK_API_KEY for recall planner")
        return cls(
            api_key=api_key,
            base_url=(
                os.environ.get("SORA_LLM_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com"
            ),
            model=(
                os.environ.get("SORA_RECALL_PLANNER_MODEL")
                or os.environ.get("SORA_LLM_MODEL")
                or os.environ.get("OPENAI_MODEL")
                or "deepseek-v4-flash"
            ),
            timeout=int(os.environ.get("SORA_RECALL_PLANNER_TIMEOUT", "45") or 45),
        )

    def plan(self, input_package: JsonDict) -> JsonDict:
        started = perf_counter()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _llm_system_prompt()},
                {"role": "user", "content": json.dumps(input_package, ensure_ascii=False)},
            ],
            "temperature": 0.0,
            "max_tokens": int(os.environ.get("SORA_RECALL_PLANNER_MAX_TOKENS", "1200") or 1200),
            "response_format": {"type": "json_object"},
        }
        payload = self._post_chat_completion(body)
        content = payload["choices"][0]["message"].get("content") or ""
        result, repairs = _parse_planner_json(content)
        result["model"] = {
            "provider": "openai_compatible",
            "model": payload.get("model") or self.model,
            "prompt_version": RECALL_PLANNER_VERSION,
        }
        result["usage"] = payload.get("usage", {})
        result["repairs"] = repairs
        result["latency_ms"] = round((perf_counter() - started) * 1000, 2)
        return result

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with observe_http("provider.http.compat_recall_plan"), urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"recall planner LLM API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"recall planner LLM connection failed: {exc}") from exc


def create_recall_planner_from_env() -> RecallPlanner:
    mode = os.environ.get("SORA_RECALL_PLANNER_PROVIDER", "hybrid_rules").lower()
    if mode in {"rules", "rule"}:
        return RuleRecallPlanner()
    if mode in {"llm", "hybrid_llm", "openai", "deepseek"}:
        return HybridRecallPlanner(llm_provider=OpenAICompatibleRecallPlannerProvider.from_env(), enable_llm=True)
    return HybridRecallPlanner(enable_llm=False)


def plan_from_llm_json(payload: JsonDict, *, fallback: RecallPlan) -> RecallPlan:
    decision = _clean_choice(str(payload.get("decision") or fallback.decision), {
        "skip",
        "light_recall",
        "deep_recall",
        "prefetch_only",
    }, fallback.decision)
    intent = _clean_choice(str(payload.get("query_intent") or fallback.query_intent), set(QUERY_INTENTS), fallback.query_intent)
    subqueries = _subqueries_from_json(payload.get("subqueries", []), fallback=fallback, intent=intent)
    budget = budget_from_json(payload.get("budget", {}), fallback=fallback.budget)
    skipped_reason = str(payload.get("skip_reason") or "") or None
    if decision == "skip" and not skipped_reason:
        skipped_reason = "llm_planner_decision_skip"
    return RecallPlan(
        query_text=fallback.query_text,
        top_k=fallback.top_k,
        tokens=_tokens_for_subqueries(subqueries) or fallback.tokens,
        salient_terms=_dedupe_terms(
            term for subquery in subqueries for term in subquery.salient_terms
        )
        or fallback.salient_terms,
        explicit_recall=bool(payload.get("explicit_recall", fallback.explicit_recall)),
        skipped_reason=skipped_reason,
        backends=_union_backends(subqueries) or fallback.backends,
        service_budget_ms=budget.service_budget_ms,
        planner_mode="llm",
        planner_version=RECALL_PLANNER_VERSION,
        decision=decision,
        query_intent=intent,
        scene=str(payload.get("scene") or fallback.scene),
        subqueries=subqueries,
        budget=budget,
        surface_hint=str(payload.get("surface_hint") or fallback.surface_hint),
        fallbacks=[str(item) for item in payload.get("fallbacks", fallback.fallbacks)],
        decision_reasons=[str(item) for item in payload.get("decision_reasons", [])] or fallback.decision_reasons,
        planner_trace={**fallback.planner_trace, "llm_raw_keys": sorted(payload.keys())},
        prefetch=dict(payload.get("prefetch", fallback.prefetch) or {}),
    )


QUERY_INTENTS = (
    "open_chat",
    "explicit_memory_lookup",
    "preference_lookup",
    "shared_episode_lookup",
    "domain_continuity",
    "relationship_context",
    "world_ops_lookup",
    "write_related_memory",
    "negative_or_no_recall",
)


def classify_query_intent(
    query_text: str,
    *,
    mode: str = "response_generation",
    turn_frame: JsonDict | None = None,
) -> str:
    lowered = query_text.lower()
    if mode == "write_related_memory":
        return "write_related_memory"
    if mode == "prefetch":
        return "domain_continuity" if _contains_any(lowered, DOMAIN_CONTINUITY_TERMS) else "open_chat"
    if not query_text.strip():
        return "negative_or_no_recall"
    if _contains_any(lowered, RELATIONSHIP_TERMS):
        return "relationship_context"
    if is_explicit_recall(query_text):
        return "explicit_memory_lookup"
    if _contains_any(lowered, EPISODE_TERMS):
        return "shared_episode_lookup"
    if _contains_any(lowered, PREFERENCE_TERMS):
        return "preference_lookup"
    if _contains_any(lowered, WORLD_TERMS):
        return "world_ops_lookup"
    if _contains_any(lowered, DOMAIN_CONTINUITY_TERMS) or _turn_frame_has_domain_context(turn_frame):
        return "domain_continuity"
    return "open_chat"


def decide_recall_depth(
    query_text: str,
    *,
    mode: str,
    intent: str,
    explicit: bool,
    turn_frame: JsonDict | None,
) -> str:
    if mode == "prefetch":
        return "prefetch_only"
    if mode == "write_related_memory":
        return "deep_recall"
    if intent == "negative_or_no_recall":
        return "skip"
    if intent == "relationship_context":
        return "light_recall"
    if explicit or intent in {"explicit_memory_lookup", "shared_episode_lookup", "write_related_memory"}:
        return "deep_recall"
    if intent in {"domain_continuity", "preference_lookup", "relationship_context", "world_ops_lookup"}:
        return "light_recall"
    if _contains_any(query_text.lower(), DEICTIC_TERMS) and _turn_frame_has_recent_context(turn_frame):
        return "light_recall"
    if _is_low_value_greeting(query_text):
        return "skip"
    return "light_recall"


def allocate_budget(decision: str, *, explicit: bool, requested: JsonDict | None = None) -> RecallBudget:
    requested = requested or {}
    if decision == "skip":
        default = RecallBudget(token_budget=0, service_budget_ms=50, rerank_budget_ms=0)
    elif decision == "prefetch_only":
        default = RecallBudget(token_budget=0, service_budget_ms=800, rerank_budget_ms=120)
    elif decision == "deep_recall" or explicit:
        default = RecallBudget(token_budget=900, service_budget_ms=1200, rerank_budget_ms=300)
    else:
        default = RecallBudget(token_budget=420, service_budget_ms=600, rerank_budget_ms=150)
    return RecallBudget(
        token_budget=int(requested.get("default_token_budget") or requested.get("token_budget") or default.token_budget),
        service_budget_ms=int(requested.get("latency_budget_ms") or requested.get("service_budget_ms") or default.service_budget_ms),
        rerank_budget_ms=int(requested.get("rerank_budget_ms") or default.rerank_budget_ms),
        llm_planner_budget_tokens=int(requested.get("llm_planner_budget_tokens") or default.llm_planner_budget_tokens),
    )


def build_subqueries(
    query_text: str,
    *,
    intent: str,
    explicit: bool,
    top_k: int,
    salient_terms: list[str],
    turn_frame: JsonDict | None,
) -> list[RecallSubquery]:
    if not query_text.strip():
        return []
    entities = extract_entities(query_text, turn_frame or {})
    base_text = " ".join(salient_terms) if salient_terms else query_text
    backends = choose_backends(query_text, intent=intent, explicit_recall=explicit)
    memory_types = memory_types_for_intent(intent)
    subqueries = [
        RecallSubquery(
            query_id="q1",
            text=base_text,
            tokens=search_tokens(base_text),
            salient_terms=salient_terms,
            memory_types=memory_types,
            entities=entities,
            time_range=time_range_for_intent(intent, explicit=explicit),
            backends=backends,
            filters={"memory_types": memory_types} if memory_types else {},
            top_k=top_k_for_intent(intent, top_k=top_k, explicit=explicit),
            min_confidence=0.45 if explicit else 0.55,
            weight=1.0,
            reason=f"{intent} primary query",
        )
    ]
    if intent == "domain_continuity" and entities:
        text = " ".join(entities + salient_terms) or query_text
        subqueries.append(
            RecallSubquery(
                query_id="q2",
                text=text,
                tokens=search_tokens(text),
                salient_terms=_dedupe_terms(entities + salient_terms),
                memory_types=["domain_principle", "project_principle", "shared_episode", "procedural_hint"],
                entities=entities,
                time_range="recent_or_any",
                backends=("fts", "vector", "temporal", "graph"),
                filters={"domain_context": True},
                top_k=max(top_k, 5),
                min_confidence=0.5,
                weight=0.8,
                reason="project continuity expansion",
            )
        )
    if intent == "relationship_context":
        relationship_terms = _dedupe_terms(
            list(salient_terms)
            + list(entities)
            + [
                term
                for term in ("relationship", "companionship", "shared episode", "preference")
                if term.lower() in query_text.lower() or not salient_terms
            ]
        )
        relationship_text = " ".join(relationship_terms) if relationship_terms else query_text
        subqueries.append(
            RecallSubquery(
                query_id="q2",
                text=relationship_text,
                tokens=search_tokens(relationship_text),
                salient_terms=relationship_terms,
                memory_types=["relationship_signal", "shared_episode", "preference"],
                entities=entities,
                time_range="any",
                backends=choose_backends(query_text, intent="relationship_context", explicit_recall=explicit),
                filters={"surface_policy": "strict_relationship"},
                top_k=max(3, top_k),
                min_confidence=0.65,
                weight=0.65,
                reason="relationship context must be evidence-backed and governable",
            )
        )
    return subqueries[:3]


def memory_types_for_intent(intent: str) -> list[str]:
    return {
        "explicit_memory_lookup": [],
        "preference_lookup": ["preference", "persona_delta", "domain_principle", "project_principle"],
        "shared_episode_lookup": ["shared_episode", "episode", "worldops_episode"],
        "domain_continuity": ["domain_principle", "project_principle", "procedural_hint", "shared_episode"],
        "relationship_context": ["relationship_signal", "shared_episode", "preference"],
        "world_ops_lookup": ["world_ops", "world_state", "worldops_episode"],
        "write_related_memory": [],
        "open_chat": ["preference", "shared_episode", "domain_principle", "project_principle"],
    }.get(intent, [])


def time_range_for_intent(intent: str, *, explicit: bool) -> str:
    if intent in {"shared_episode_lookup", "domain_continuity"}:
        return "recent_or_any"
    if explicit:
        return "any"
    return "active"


def top_k_for_intent(intent: str, *, top_k: int, explicit: bool) -> int:
    if intent == "write_related_memory":
        return max(top_k, 8)
    if explicit or intent in {"shared_episode_lookup", "domain_continuity"}:
        return max(top_k, 5)
    return top_k


def choose_backends(query_text: str, *, intent: str = "open_chat", explicit_recall: bool) -> tuple[str, ...]:
    if intent == "negative_or_no_recall":
        return ()
    if explicit_recall or intent == "explicit_memory_lookup":
        return ("fts", "vector", "temporal", "graph")
    if intent == "relationship_context":
        return ("vector", "temporal", "graph")
    if intent == "world_ops_lookup":
        return ("vector", "temporal", "graph")
    if intent == "write_related_memory":
        return ("fts", "vector", "temporal", "graph")
    if intent == "preference_lookup":
        return ("fts", "vector", "temporal")
    if intent == "shared_episode_lookup":
        return ("fts", "vector", "temporal", "graph")
    if intent == "domain_continuity":
        return ("fts", "vector", "temporal", "graph")
    return ("fts", "vector")


def extract_salient_terms(query_text: str, turn_frame: JsonDict | None = None) -> list[str]:
    terms: list[str] = []
    for match in QUOTED_TERM_RE.finditer(query_text):
        terms.append(match.group("term"))
    if terms:
        return _dedupe_terms(terms)
    for pattern in TOPIC_PATTERNS:
        match = pattern.search(query_text)
        if match:
            terms.append(_clean_term(match.group("term")))
    if not terms:
        terms.extend(extract_entities(query_text, turn_frame or {})[:3])
    return _dedupe_terms(term for term in terms if term)


def extract_entities(query_text: str, turn_frame: JsonDict | None = None) -> list[str]:
    entities: list[str] = []
    frame = turn_frame or {}
    for key in ("local_entities", "recent_topics", "entities"):
        value = frame.get(key)
        if isinstance(value, list):
            entities.extend(str(item) for item in value if str(item).strip())
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9_/-]{2,}\b", query_text):
        entities.append(match.group(0))
    return _dedupe_terms(entities)


def is_explicit_recall(query_text: str) -> bool:
    lowered = query_text.lower()
    return any(term.lower() in lowered for term in EXPLICIT_RECALL_TERMS)


def surface_hint(intent: str, *, explicit: bool, decision: str) -> str:
    if decision in {"skip", "prefetch_only"}:
        return "none"
    if intent == "relationship_context":
        return "strict_gist"
    if explicit:
        return "allow_gist"
    return "allow_implicit"


def decision_reasons_for(
    query_text: str,
    *,
    mode: str,
    intent: str,
    explicit: bool,
    decision: str,
    turn_frame: JsonDict | None,
) -> list[str]:
    reasons = [f"mode={mode}", f"intent={intent}", f"decision={decision}"]
    if explicit:
        reasons.append("explicit recall phrase matched")
    if _contains_any(query_text.lower(), DEICTIC_TERMS):
        reasons.append("deictic or continuation reference detected")
    if _turn_frame_has_recent_context(turn_frame):
        reasons.append("turn_frame supplied recent context")
    return reasons


def rule_features(query_text: str, *, turn_frame: JsonDict | None = None) -> JsonDict:
    lowered = query_text.lower()
    return {
        "explicit_recall": is_explicit_recall(query_text),
        "has_domain_continuity_terms": _contains_any(lowered, DOMAIN_CONTINUITY_TERMS),
        "has_relationship_terms": _contains_any(lowered, RELATIONSHIP_TERMS),
        "has_preference_terms": _contains_any(lowered, PREFERENCE_TERMS),
        "has_episode_terms": _contains_any(lowered, EPISODE_TERMS),
        "has_world_terms": _contains_any(lowered, WORLD_TERMS),
        "has_deictic_terms": _contains_any(lowered, DEICTIC_TERMS),
        "turn_frame_has_recent_context": _turn_frame_has_recent_context(turn_frame),
    }


def plan_to_json(plan: RecallPlan) -> JsonDict:
    return {
        "plan_id": plan.plan_id,
        "planner_version": plan.planner_version,
        "mode": plan.planner_mode,
        "decision": plan.decision,
        "skip_reason": plan.skipped_reason,
        "query_text": plan.query_text,
        "query_intent": plan.query_intent,
        "scene": plan.scene,
        "explicit_recall": plan.explicit_recall,
        "top_k": plan.top_k,
        "tokens": list(plan.tokens),
        "salient_terms": list(plan.salient_terms),
        "backends": list(plan.backends),
        "service_budget_ms": plan.service_budget_ms,
        "budget": budget_to_json(plan.budget),
        "surface_hint": plan.surface_hint,
        "fallbacks": list(plan.fallbacks),
        "decision_reasons": list(plan.decision_reasons),
        "subqueries": [subquery_to_json(item) for item in plan.subqueries],
        "llm_usage": plan.llm_usage,
        "planner_trace": plan.planner_trace,
        "prefetch": plan.prefetch,
    }


def subquery_to_json(subquery: RecallSubquery) -> JsonDict:
    return {
        "query_id": subquery.query_id,
        "text": subquery.text,
        "tokens": list(subquery.tokens),
        "salient_terms": list(subquery.salient_terms),
        "memory_types": list(subquery.memory_types),
        "entities": list(subquery.entities),
        "time_range": subquery.time_range,
        "backends": list(subquery.backends),
        "filters": subquery.filters,
        "top_k": subquery.top_k,
        "min_confidence": subquery.min_confidence,
        "weight": subquery.weight,
        "reason": subquery.reason,
    }


def budget_to_json(budget: RecallBudget) -> JsonDict:
    return {
        "token_budget": budget.token_budget,
        "service_budget_ms": budget.service_budget_ms,
        "rerank_budget_ms": budget.rerank_budget_ms,
        "llm_planner_budget_tokens": budget.llm_planner_budget_tokens,
    }


def budget_from_json(payload: JsonDict, *, fallback: RecallBudget) -> RecallBudget:
    if not isinstance(payload, dict):
        payload = {}
    return RecallBudget(
        token_budget=int(payload.get("token_budget", fallback.token_budget) or 0),
        service_budget_ms=int(payload.get("service_budget_ms", fallback.service_budget_ms) or 0),
        rerank_budget_ms=int(payload.get("rerank_budget_ms", fallback.rerank_budget_ms) or 0),
        llm_planner_budget_tokens=int(
            payload.get("llm_planner_budget_tokens", fallback.llm_planner_budget_tokens) or 0
        ),
    )


def _subqueries_from_json(payload: object, *, fallback: RecallPlan, intent: str) -> list[RecallSubquery]:
    if not isinstance(payload, list):
        return fallback.subqueries
    subqueries: list[RecallSubquery] = []
    for index, item in enumerate(payload[:3], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or fallback.query_text).strip()
        if not text:
            continue
        backends = tuple(str(value) for value in item.get("backends", fallback.backends) if str(value))
        memory_types = [str(value) for value in item.get("memory_types", memory_types_for_intent(intent))]
        entities = [str(value) for value in item.get("entities", [])]
        salient = [str(value) for value in item.get("salient_terms", [])] or extract_salient_terms(text)
        subqueries.append(
            RecallSubquery(
                query_id=str(item.get("query_id") or f"llm_q{index}"),
                text=text,
                tokens=[str(value) for value in item.get("tokens", [])] or search_tokens(text),
                salient_terms=salient,
                memory_types=memory_types,
                entities=entities,
                time_range=str(item.get("time_range") or "any"),
                backends=backends or fallback.backends,
                filters=dict(item.get("filters", {}) or {}),
                top_k=int(item.get("top_k") or fallback.top_k),
                min_confidence=float(item.get("min_confidence") or 0.0),
                weight=float(item.get("weight") or 1.0),
                reason=str(item.get("reason") or "llm planner subquery"),
            )
        )
    return subqueries or fallback.subqueries


def _skip_plan(
    query_text: str,
    *,
    top_k: int,
    scene: str,
    mode: str,
    reason: str,
    decision_reasons: list[str],
) -> RecallPlan:
    budget = RecallBudget(token_budget=0, service_budget_ms=50, rerank_budget_ms=0)
    return RecallPlan(
        query_text=query_text,
        top_k=top_k,
        tokens=[],
        skipped_reason=reason,
        backends=(),
        service_budget_ms=budget.service_budget_ms,
        planner_mode=mode,
        planner_version=RECALL_PLANNER_VERSION,
        decision="skip",
        query_intent="negative_or_no_recall",
        scene=scene,
        subqueries=[],
        budget=budget,
        surface_hint="none",
        decision_reasons=decision_reasons,
        planner_trace={"skip_reason": reason},
    )


def _scene_from_turn_frame(turn_frame: JsonDict | None) -> str:
    frame = turn_frame or {}
    scene = frame.get("scene")
    if isinstance(scene, str) and scene:
        return scene
    if isinstance(frame.get("intent"), dict):
        primary = frame["intent"].get("primary")
        if primary:
            return str(primary)
    return "unknown"


def _needs_llm_planner(plan: RecallPlan, query_text: str, turn_frame: JsonDict | None) -> bool:
    if plan.query_intent in {"relationship_context", "shared_episode_lookup", "write_related_memory"}:
        return True
    lowered = query_text.lower()
    return _contains_any(lowered, DEICTIC_TERMS) and _turn_frame_has_recent_context(turn_frame)


def _turn_frame_has_domain_context(turn_frame: JsonDict | None) -> bool:
    frame = turn_frame or {}
    text = " ".join(str(item) for key in ("recent_topics", "local_entities", "entities") for item in frame.get(key, []) if isinstance(frame.get(key), list))
    return _contains_any(text.lower(), DOMAIN_CONTINUITY_TERMS)


def _turn_frame_has_recent_context(turn_frame: JsonDict | None) -> bool:
    frame = turn_frame or {}
    for key in ("recent_topics", "local_entities", "entities", "resolved_targets"):
        value = frame.get(key)
        if isinstance(value, list) and value:
            return True
    if isinstance(frame.get("context_reference"), dict):
        return bool(frame["context_reference"].get("references_previous_context"))
    return False


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _is_low_value_greeting(query_text: str) -> bool:
    normalized = re.sub(r"[\s!！?？。,.，]", "", query_text.lower())
    return normalized in {"hi", "hello", "hey", "你好", "你好呀", "早", "早上好", "晚上好"}


def _union_backends(subqueries: list[RecallSubquery]) -> tuple[str, ...]:
    ordered: list[str] = []
    for subquery in subqueries:
        for backend in subquery.backends:
            if backend not in ordered:
                ordered.append(backend)
    return tuple(ordered)


def _tokens_for_subqueries(subqueries: list[RecallSubquery]) -> list[str]:
    return _dedupe_terms(token for subquery in subqueries for token in subquery.tokens)


def _clean_term(value: str) -> str:
    cleaned = value.strip(" ，。!！?？\"'“”‘’")
    cleanup_prefixes = ("which ", "what ", "the ", "哪个", "哪一个", "哪种", "关于")
    cleanup_suffixes = ("是什么", "是谁", "多少", "哪个", "哪种", "品牌")
    for prefix in cleanup_prefixes:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    for suffix in cleanup_suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned.strip(" ，。!！?？\"'“”‘’")


def _dedupe_terms(values) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        cleaned = _clean_term(str(value))
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)
    return terms


def _clean_choice(value: str, allowed: set[str], fallback: str) -> str:
    return value if value in allowed else fallback


def _parse_planner_json(content: str) -> tuple[JsonDict, list[str]]:
    repairs: list[str] = []
    json_text = content.strip()
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", json_text, flags=re.DOTALL)
        if not match:
            raise
        repairs.append("extracted_json_object")
        extracted = match.group(0)
        try:
            parsed = json.loads(extracted)
        except json.JSONDecodeError:
            repairs.append("local_trailing_comma_repair")
            parsed = json.loads(_repair_json_text(extracted))
    if not isinstance(parsed, dict):
        raise ValueError("recall planner LLM output must be a JSON object")
    parsed.setdefault("subqueries", [])
    return parsed, repairs


def _repair_json_text(text: str) -> str:
    repaired = text.strip()
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*{", "},{", repaired)
    repaired = re.sub(r"(?<=[\"}\]\w])\s*\n\s*(?=\"[A-Za-z_][A-Za-z0-9_]*\"\s*:)", ",\n", repaired)
    return repaired


def _llm_system_prompt() -> str:
    return (
        "You are a domain-neutral memory Recall Planner. Return one valid JSON object only. "
        "You plan memory retrieval; you never claim facts are true and never answer the user. "
        "Use fields: decision, query_intent, explicit_recall, scene, subqueries[], budget, "
        "surface_hint, fallbacks[], decision_reasons[]. A subquery has query_id, text, "
        "memory_types, entities, time_range, backends, filters, top_k, min_confidence, weight, reason. "
        "Allowed decisions: skip, light_recall, deep_recall, prefetch_only. "
        "Allowed backends: fts, vector, temporal, graph. Prefer at most three subqueries. "
        "For relationship or companionship context, plan low-risk gist retrieval and leave final "
        "surface decisions to the surface policy layer. For write_related_memory mode, optimize precision for "
        "UPDATE/MERGE target matching."
    )
