from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .interruptibility_contracts import (
    EXPANSION_PERMISSIONS,
    FOLLOW_UP_PERMISSIONS,
    INTERRUPTIBILITY_DIMENSIONS,
    RESPONSE_SHAPES,
    TOPIC_SHIFT_PERMISSIONS,
    BoundedResolution,
    BoundedResolutionRequest,
    CanonicalControlEvent,
    InterruptibilityFrame,
    InterruptibilityInput,
    InterruptibilityResolutionProvider,
    ModelAssistTrace,
    PolicyEvidence,
    ResponseBudgets,
    StateSurfacePermission,
)
from .llm import LLMClient


JsonDict = dict[str, Any]

POLICY_PROFILE = "text_first_default.v1"
POLICY_VERSION = "1.0.0"
STATE_SCHEMA_VERSION = 1
TRACE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
LEASE_INACTIVITY_SECONDS = 15 * 60
PENDING_CONFIRMATION_SECONDS = 10 * 60
MAX_TRACES = 2000
MAX_OVERRIDE_HISTORY = 500
MAX_FEEDBACK = 500

_SCOPE_PRIORITY = {"default": 0, "persistent": 1, "duration": 2, "session": 2, "turn": 3}
_EXPANSION_RANK = {value: index for index, value in enumerate(EXPANSION_PERMISSIONS)}
_TOPIC_RANK = {value: index for index, value in enumerate(TOPIC_SHIFT_PERMISSIONS)}
_FOLLOW_UP_RANK = {value: index for index, value in enumerate(FOLLOW_UP_PERMISSIONS)}
_SHAPE_RANK = {"ack": 0, "brief": 1, "repair_brief": 1, "normal": 2, "deep": 3, "safety": 0}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_enum(value: Any, allowed: Iterable[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def default_interruptibility_state() -> JsonDict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "profile": {
            "profile_id": "default",
            "policy_profile": POLICY_PROFILE,
            "policy_version": POLICY_VERSION,
            "persistent_preferences": {
                "reply_depth": "balanced",
                "follow_up": "one",
                "topic_shift": "same_topic",
                "state_surface": "implicit",
            },
            "confirmation_proofs": [],
            "updated_at": None,
        },
        "overrides": [],
        "pending_confirmations": [],
        "session": {
            "session_id": None,
            "initiative_lease": None,
            "last_user_activity_at": None,
            "user_led_depth_streak": 0,
            "last_control_at": None,
        },
        "traces": [],
        "feedback": [],
    }


def ensure_interruptibility_state(state: JsonDict) -> bool:
    changed = False
    current = state.get("interruptibility")
    if not isinstance(current, dict):
        state["interruptibility"] = default_interruptibility_state()
        current = state["interruptibility"]
        changed = True
    defaults = default_interruptibility_state()
    for key, value in defaults.items():
        if key not in current:
            current[key] = value
            changed = True
    profile = current.setdefault("profile", {})
    for key, value in defaults["profile"].items():
        if key not in profile:
            profile[key] = value
            changed = True
    preferences = profile.setdefault("persistent_preferences", {})
    for key, value in defaults["profile"]["persistent_preferences"].items():
        if key not in preferences:
            preferences[key] = value
            changed = True
    session = current.setdefault("session", {})
    for key, value in defaults["session"].items():
        if key not in session:
            session[key] = value
            changed = True
    if current.get("schema_version") != STATE_SCHEMA_VERSION:
        current["schema_version"] = STATE_SCHEMA_VERSION
        changed = True
    return changed


class ForbiddenFeatureGate:
    """Drops relational pressure features before policy, prompts, or traces."""

    _FORBIDDEN_FRAGMENTS = (
        "love",
        "dependency",
        "dependent",
        "loyalty",
        "possessiveness",
        "possessive",
        "attachment_pressure",
        "retention_pressure",
        "abandonment_pressure",
    )

    def sanitize(self, value: Mapping[str, Any]) -> tuple[JsonDict, tuple[str, ...]]:
        filtered: list[str] = []

        def walk(node: Any, path: str) -> Any:
            if isinstance(node, Mapping):
                clean: JsonDict = {}
                for raw_key, child in node.items():
                    key = str(raw_key)
                    identifier = f"{path}.{key}" if path else key
                    normalized = re.sub(r"[^a-z0-9_]+", "_", key.lower())
                    if any(fragment in normalized for fragment in self._FORBIDDEN_FRAGMENTS):
                        filtered.append(identifier)
                        continue
                    clean[key] = walk(child, identifier)
                return clean
            if isinstance(node, list):
                return [walk(item, f"{path}[]") for item in node]
            return node

        return walk(value, ""), tuple(sorted(set(filtered)))


@dataclass(slots=True)
class CanonicalControlParser:
    """Central locale adapter for unmistakable text controls.

    Turn Interpreter may produce the same canonical events. Hard controls are
    accepted only when this parser also finds explicit supporting language.
    """

    locale: str = "zh-CN"

    def parse(self, text: str) -> list[CanonicalControlEvent]:
        normalized = " ".join(text.strip().lower().split())
        if not normalized:
            return []
        scope = self._scope(normalized)
        duration = self._duration_seconds(normalized)
        if duration is not None:
            scope = "duration"
        events: list[CanonicalControlEvent] = []

        def add(
            control: str,
            value: Any,
            *,
            default_scope: str | None = None,
            requires_confirmation: bool = False,
        ) -> None:
            selected_scope = scope if scope != "turn" or default_scope is None else default_scope
            persistent_confirmation = selected_scope == "persistent" and control in {
                "response_mute",
                "state_surface",
            }
            events.append(
                CanonicalControlEvent(
                    event_id=_new_id("ctrl"),
                    control=control,
                    value=value,
                    scope=selected_scope,
                    duration_seconds=duration,
                    requires_confirmation=requires_confirmation or persistent_confirmation,
                )
            )

        if self._contains(normalized, ("查看控制", "控制状态", "当前设置", "show controls")):
            add("status", True)
            return events
        if self._contains(normalized, ("确认设置", "确认这个设置", "confirm setting", "confirm control")):
            add("confirm", self._confirmation_token(normalized))
            return events
        if self._contains(normalized, ("取消所有控制", "重置控制", "revoke all controls")):
            add("revoke", "all")
            return events
        if self._contains(normalized, ("可以说话了", "恢复回复", "恢复说话", "继续说吧", "resume replies", "you can reply now")):
            add("resume", True)
            return events

        if self._contains(normalized, ("先别回复", "不用回复", "不要回复", "别回我", "无需回复", "do not reply", "don't reply")):
            default_scope = "turn" if self._contains(normalized, ("这条", "这次", "这一条", "this message")) else "session"
            add("response_mute", True, default_scope=default_scope)
        if self._contains(normalized, ("先安静一下", "别主动说话", "不要主动找我", "关闭主动消息", "quiet mode", "do not initiate")):
            add("quiet", True, default_scope="session")
        if self._contains(normalized, ("简短点", "简单说", "说短一点", "只说重点", "keep it brief", "brief answer")):
            add("reply_depth", "brief")
        if self._contains(normalized, ("详细讲", "展开说", "说深入一点", "详细一点", "go deeper", "explain in detail")):
            add("reply_depth", "deep")
        if self._contains(normalized, ("别问我", "不要追问", "别再问了", "不用问我", "no follow-up", "don't ask me")):
            default_scope = "turn" if self._contains(normalized, ("这次", "这条", "this time")) else "session"
            add("follow_up", "none", default_scope=default_scope)
        if self._contains(normalized, ("你可以问我", "可以追问", "可以问问题", "you may ask", "follow-up is okay")):
            add("follow_up", "one", default_scope="session")
        if self._contains(normalized, ("别换话题", "不要转移话题", "只聊这个", "stay on topic", "no topic changes")):
            add("topic_shift", "blocked", default_scope="session")
        if self._contains(normalized, ("可以换话题", "随便聊点别的", "话题可以自由一点", "topic changes are okay")):
            add("topic_shift", "open", default_scope="session")
        if self._contains(normalized, ("陪我聊会儿", "陪我聊一会", "主动和我聊聊", "keep me company", "chat with me for a while")):
            add("initiative_lease", True, default_scope="session")
        if self._contains(normalized, ("你还记得吗", "可以直接说你记得的", "可以提以前的事", "explicitly use memory")):
            add("state_surface", {"memory": "explicit"})
        return events

    @staticmethod
    def _contains(text: str, phrases: tuple[str, ...]) -> bool:
        return any(phrase in text for phrase in phrases)

    @staticmethod
    def _scope(text: str) -> str:
        if any(marker in text for marker in ("以后都", "以后一直", "永远", "永久", "from now on", "always")):
            return "persistent"
        if any(marker in text for marker in ("这次会话", "本次会话", "今天先", "this session")):
            return "session"
        return "turn"

    @staticmethod
    def _duration_seconds(text: str) -> int | None:
        match = re.search(r"(?P<amount>\d{1,4})\s*(?P<unit>分钟|小时|天|mins?|minutes?|hours?|days?)", text)
        if not match:
            return None
        amount = int(match.group("amount"))
        unit = match.group("unit")
        multiplier = 60 if unit in {"分钟", "min", "mins", "minute", "minutes"} else 3600
        if unit in {"天", "day", "days"}:
            multiplier = 86400
        return min(amount * multiplier, 365 * 86400)

    @staticmethod
    def _confirmation_token(text: str) -> str | None:
        match = re.search(r"(?:confirm|确认)[:：\s]+([a-z0-9_-]{6,80})", text)
        return match.group(1) if match else None


@dataclass(slots=True)
class LLMInterruptibilityResolutionProvider:
    llm: LLMClient

    def resolve(self, request: BoundedResolutionRequest) -> BoundedResolution:
        system = (
            "You are a bounded interruptibility resolver. Decide only the requested fields. "
            "Never add fields, never exceed allowed choices, and return strict JSON only."
        )
        user = json.dumps(
            {
                "task": "resolve_interruptibility",
                "schema_version": "bounded_resolution.v1",
                "request_id": request.request_id,
                "unresolved_dimensions": list(request.unresolved_dimensions),
                "allowed_choices": {key: list(value) for key, value in request.allowed_choices.items()},
                "permission_ceiling": dict(request.permission_ceiling),
                "conservative_defaults": dict(request.conservative_defaults),
                "turn_interpretation": dict(request.turn_interpretation),
                "evidence": [item.to_dict() for item in request.evidence],
                "output": {
                    "schema_version": "bounded_resolution.v1",
                    "request_id": request.request_id,
                    "decisions": {"requested_dimension": "allowed choice"},
                    "evidence_refs": ["evidence_id"],
                    "reason_codes": ["SHORT_CODE"],
                    "confidence": "0..1",
                },
            },
            ensure_ascii=False,
        )
        result = self.llm.chat(
            system=system,
            user=user,
            workload="interruptibility_resolution",
            temperature=0.0,
            max_tokens=500,
            thinking={"type": "disabled"},
            timeout=max(0.1, request.deadline_ms / 1000),
        )
        payload = _extract_json_object(str(result.get("content", "")))
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
        return BoundedResolution(
            request_id=str(payload.get("request_id") or ""),
            decisions={str(key): str(value) for key, value in decisions.items()},
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", []) if item),
            reason_codes=tuple(str(item)[:80] for item in payload.get("reason_codes", []) if item),
            confidence=_clamp01(payload.get("confidence", 0.0)),
            provider=str(result.get("model") or "llm"),
            schema_version=str(payload.get("schema_version") or ""),
        )


def _extract_json_object(content: str) -> JsonDict:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("provider did not return a JSON object")


@dataclass(slots=True)
class InterruptibilityService:
    provider: InterruptibilityResolutionProvider | None = None
    parser: CanonicalControlParser = field(default_factory=CanonicalControlParser)
    feature_gate: ForbiddenFeatureGate = field(default_factory=ForbiddenFeatureGate)
    policy_profile: str = POLICY_PROFILE
    policy_version: str = POLICY_VERSION
    provider_deadline_ms: int = 700
    provider_min_confidence: float = 0.80

    def evaluate(
        self,
        state: JsonDict,
        *,
        user_input: str,
        turn_interpretation: Mapping[str, Any],
        turn_kind: str,
        correlation_id: str,
        proactive_trigger: Mapping[str, Any] | None = None,
        safety_privacy_frame: Mapping[str, Any] | None = None,
        domain_features: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> JsonDict:
        now = int(timestamp_ms or _now_ms())
        ensure_interruptibility_state(state)
        self._maintain_state(state, now)
        safe_features, filtered_ids = self.feature_gate.sanitize(domain_features or {})
        turn_id = str(state.get("session", {}).get("turn_id", "unknown"))
        parsed_events = self.parser.parse(user_input)
        events = self._merge_control_events(parsed_events, turn_interpretation)
        control_result = self._apply_control_events(state, events, turn_id=turn_id, now=now)
        snapshot = self.control_snapshot(state, turn_id=turn_id, turn_values=control_result["turn_values"], now=now)
        self._update_activity_lease(
            state,
            events,
            snapshot,
            user_input=user_input,
            turn_kind=turn_kind,
            now=now,
        )
        snapshot = self.control_snapshot(state, turn_id=turn_id, turn_values=control_result["turn_values"], now=now)
        session_state = self._session_interaction_state(state, turn_interpretation, now)
        policy_input = InterruptibilityInput(
            turn_id=turn_id,
            correlation_id=correlation_id,
            turn_kind=turn_kind,
            user_input_present=bool(user_input.strip()),
            timestamp_ms=now,
            turn_interpretation=turn_interpretation,
            user_control_snapshot=snapshot,
            session_interaction_state=session_state,
            safety_privacy_frame=safety_privacy_frame or {},
            domain_features=safe_features,
        )
        try:
            frame = self._resolve(
                policy_input,
                control_result=control_result,
                filtered_ids=filtered_ids,
            )
        except Exception as exc:  # hard rules are recomputed in the fallback
            frame = self._fallback_frame(
                policy_input,
                snapshot=snapshot,
                filtered_ids=filtered_ids,
                receipts=control_result["receipts"],
                reason=f"{type(exc).__name__}:{exc}",
            )
        payload = frame.to_dict()
        self._record_trace(state, payload, user_input=user_input, timestamp_ms=now)
        state["interruptibility"]["last_frame"] = payload
        return payload

    def _resolve(
        self,
        policy_input: InterruptibilityInput,
        *,
        control_result: JsonDict,
        filtered_ids: tuple[str, ...],
    ) -> InterruptibilityFrame:
        interpretation = policy_input.turn_interpretation
        controls = policy_input.user_control_snapshot
        session_state = policy_input.session_interaction_state
        risk = str(interpretation.get("domain", {}).get("risk_level", "low"))
        speech_act = str(interpretation.get("speech_act", "status_update"))
        move = str(interpretation.get("conversation_move", "new_topic"))
        intent = str(interpretation.get("intent", {}).get("primary", "continue_chat"))
        requirements = interpretation.get("response_requirements", {})
        evidence: list[PolicyEvidence] = []
        reason_codes: list[str] = []
        hard_overrides: list[str] = []

        response_obligation = "required" if policy_input.user_input_present else "optional"
        expansion = "limited"
        topic_shift = "same_topic"
        follow_up = "none"
        initiative = "blocked"
        response_shape = "normal"
        state_surface = StateSurfacePermission()
        confidence = 0.86

        if policy_input.turn_kind == "proactive":
            response_obligation = "optional"
            expansion = "blocked"
            topic_shift = "blocked"
            response_shape = "brief"
            reason_codes.append("SYSTEM_INITIATED")
        else:
            reason_codes.append("USER_INITIATED")

        if speech_act == "greeting" or move == "greeting_reentry":
            expansion = "blocked"
            topic_shift = "blocked"
            follow_up = "none"
            response_shape = "ack"
            reason_codes.append("GREETING_ACK")
        elif self._is_closing_move(interpretation):
            expansion = "blocked"
            topic_shift = "blocked"
            follow_up = "none"
            response_shape = "brief"
            reason_codes.append("USER_CLOSING_MOVE")
        elif intent in {"ask_for_action", "ask_for_advice", "ask_for_information", "ask_for_explanation"} or speech_act in {"question", "request", "planning"}:
            expansion = "limited"
            topic_shift = "blocked"
            follow_up = "one" if bool(requirements.get("needs_clarification")) else "none"
            response_shape = "normal"
            reason_codes.append("DIRECT_REQUEST")
            evidence.append(PolicyEvidence("ev_direct_request", "direct_user_request", "expand", 1.0, "turn_interpreter"))
        elif move == "emotional_disclosure" or intent in {"share_state", "express_emotion"}:
            expansion = "limited"
            topic_shift = "same_topic"
            follow_up = "one" if bool(requirements.get("needs_clarification")) else "none"
            response_shape = "normal"
            reason_codes.append("EMOTIONAL_DISCLOSURE")

        if move == "repair_or_correction" and self._looks_like_control_repair(interpretation, control_result):
            expansion = "blocked"
            topic_shift = "blocked"
            follow_up = "none"
            response_shape = "repair_brief"
            reason_codes.append("CONTROL_REPAIR")

        depth_streak = int(session_state.get("user_led_depth_streak", 0) or 0)
        if depth_streak >= 3 and move == "continue_previous_topic":
            expansion = "allowed"
            topic_shift = "same_topic"
            follow_up = "none"
            response_shape = "deep"
            reason_codes.append("USER_LED_DEEPENING")

        active_lease = session_state.get("initiative_lease")
        if active_lease and active_lease.get("active"):
            initiative = "session_only"
            reason_codes.append("ACTIVE_INITIATIVE_LEASE")
        if any(event.control == "initiative_lease" and event.value for event in control_result["events"]):
            expansion = "invited"
            topic_shift = "adjacent"
            follow_up = "one"
            response_shape = "deep"
            initiative = "session_only"
            reason_codes.append("EXPLICIT_OPEN_INVITATION")
            evidence.append(PolicyEvidence("ev_open_invitation", "explicit_open_invitation", "expand", 1.0, "control"))

        reply_depth = controls.get("reply_depth", {}).get("value", "balanced")
        if reply_depth == "brief":
            expansion = "blocked"
            follow_up = "none"
            if response_shape != "repair_brief":
                response_shape = "brief"
            hard_overrides.append("reply_depth:brief")
            reason_codes.append("EXPLICIT_BRIEF")
        elif reply_depth == "deep" and response_shape not in {"safety", "repair_brief"}:
            expansion = "invited"
            response_shape = "deep"
            hard_overrides.append("reply_depth:deep")
            reason_codes.append("EXPLICIT_DEEP")

        follow_up_value = controls.get("follow_up", {}).get("value")
        if follow_up_value in FOLLOW_UP_PERMISSIONS:
            ceiling = str(follow_up_value)
            if _FOLLOW_UP_RANK[follow_up] > _FOLLOW_UP_RANK[ceiling]:
                follow_up = ceiling
            if controls.get("follow_up", {}).get("scope") not in {"default", "persistent_profile"}:
                hard_overrides.append(f"follow_up:{ceiling}")
        topic_value = controls.get("topic_shift", {}).get("value")
        if topic_value in TOPIC_SHIFT_PERMISSIONS:
            ceiling = str(topic_value)
            if _TOPIC_RANK[topic_shift] > _TOPIC_RANK[ceiling]:
                topic_shift = ceiling
            if controls.get("topic_shift", {}).get("scope") not in {"default", "persistent_profile"}:
                hard_overrides.append(f"topic_shift:{ceiling}")

        surface_value = controls.get("state_surface", {}).get("value", "implicit")
        if surface_value == "explicit":
            state_surface = StateSurfacePermission(
                memory="explicit",
                relationship="explicit",
                worldops="explicit",
                affect="explicit",
                persona="explicit",
            )
            hard_overrides.append("state_surface:explicit_consent")
            reason_codes.append("EXPLICIT_STATE_SURFACE_CONSENT")
        elif isinstance(surface_value, Mapping):
            scoped = state_surface.to_dict()
            accepted = False
            for domain, permission in surface_value.items():
                if domain in scoped and permission in {"silent", "implicit", "explicit"}:
                    scoped[str(domain)] = str(permission)
                    accepted = True
            if accepted:
                state_surface = StateSurfacePermission(**scoped)
                hard_overrides.append("state_surface:scoped_consent")
                reason_codes.append("EXPLICIT_STATE_SURFACE_CONSENT")

        privacy = policy_input.safety_privacy_frame
        if privacy.get("state_surface") == "silent" or privacy.get("privacy_level") == "high":
            state_surface = StateSurfacePermission(
                memory="silent", relationship="silent", worldops="silent", affect="silent", persona="silent"
            )
            reason_codes.append("PRIVACY_TIGHTENED")
        if risk == "high":
            expansion = "limited"
            topic_shift = "blocked"
            follow_up = "one" if bool(requirements.get("needs_clarification")) else "none"
            initiative = "blocked"
            response_shape = "safety"
            state_surface = StateSurfacePermission(
                memory="silent", relationship="silent", worldops="silent", affect="silent", persona="silent"
            )
            reason_codes.append("HIGH_RISK_SAFETY_FRAME")

        quiet_active = bool(controls.get("quiet", {}).get("value"))
        mute_active = bool(controls.get("response_mute", {}).get("value"))
        if quiet_active:
            initiative = "blocked"
            hard_overrides.append("quiet")
            reason_codes.append("EXPLICIT_QUIET")
        if mute_active:
            response_obligation = "forbidden"
            expansion = "blocked"
            topic_shift = "blocked"
            follow_up = "none"
            initiative = "blocked"
            response_shape = "brief"
            state_surface = StateSurfacePermission(
                memory="silent", relationship="silent", worldops="silent", affect="silent", persona="silent"
            )
            hard_overrides.append("response_mute")
            reason_codes.append("EXPLICIT_RESPONSE_MUTE")

        if not policy_input.user_input_present:
            reason_codes.append("NO_USER_INPUT")
        reason_codes.append("NO_PRESENCE_EVIDENCE")

        values = {
            "expansion_permission": expansion,
            "topic_shift_permission": topic_shift,
            "follow_up_permission": follow_up,
            "response_shape": response_shape,
        }
        model_trace = ModelAssistTrace(deadline_ms=self.provider_deadline_ms, min_confidence=self.provider_min_confidence)
        resolved_by_hard_control: set[str] = set()
        if any(item.startswith("reply_depth:") for item in hard_overrides):
            resolved_by_hard_control.update({"expansion_permission", "response_shape"})
            if reply_depth == "brief":
                resolved_by_hard_control.add("follow_up_permission")
        if any(item.startswith("follow_up:") for item in hard_overrides):
            resolved_by_hard_control.add("follow_up_permission")
        if any(item.startswith("topic_shift:") for item in hard_overrides):
            resolved_by_hard_control.add("topic_shift_permission")
        if "EXPLICIT_OPEN_INVITATION" in reason_codes:
            resolved_by_hard_control.update(INTERRUPTIBILITY_DIMENSIONS)
        if (
            _clamp01(interpretation.get("confidence", 0.0)) >= 0.80
            and set(reason_codes).intersection(
                {
                    "DIRECT_REQUEST",
                    "GREETING_ACK",
                    "USER_CLOSING_MOVE",
                    "EMOTIONAL_DISCLOSURE",
                    "CONTROL_REPAIR",
                    "USER_LED_DEEPENING",
                }
            )
        ):
            resolved_by_hard_control.update(INTERRUPTIBILITY_DIMENSIONS)
        unresolved = self._unresolved_dimensions(
            interpretation,
            mute_active=mute_active,
            risk=risk,
            resolved_dimensions=resolved_by_hard_control,
        )
        if unresolved and self.provider is not None:
            values, model_trace = self._resolve_with_provider(
                interpretation,
                values,
                tuple(unresolved),
                tuple(evidence),
            )
            expansion = values["expansion_permission"]
            topic_shift = values["topic_shift_permission"]
            follow_up = values["follow_up_permission"]
            response_shape = values["response_shape"]
            if model_trace.fallback_used:
                reason_codes.append("MODEL_ASSIST_FALLBACK")
            elif model_trace.accepted_dimensions:
                reason_codes.append("BOUNDED_MODEL_ASSIST")

        budgets = self._project_budgets(
            response_obligation,
            expansion,
            topic_shift,
            follow_up,
            response_shape,
            state_surface,
        )
        decision_path = "bounded_model_assist" if model_trace.invoked and not model_trace.fallback_used else "deterministic_fast_path"
        if model_trace.fallback_used:
            decision_path = "conservative_fallback"
        return InterruptibilityFrame(
            turn_id=policy_input.turn_id,
            correlation_id=policy_input.correlation_id,
            availability="unknown",
            response_obligation=response_obligation,
            expansion_permission=expansion,
            topic_shift_permission=topic_shift,
            follow_up_permission=follow_up,
            initiative_permission=initiative,
            state_surface_permission=state_surface,
            response_shape=response_shape,
            decision_path=decision_path,
            model_assist=model_trace,
            budgets=budgets,
            hard_overrides=tuple(hard_overrides),
            evidence=tuple(evidence),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            confidence=confidence,
            policy_profile=self.policy_profile,
            policy_version=self.policy_version,
            trace_id=_new_id("itrace"),
            control_receipts=tuple(control_result["receipts"]),
            filtered_feature_ids=filtered_ids,
        )

    def _fallback_frame(
        self,
        policy_input: InterruptibilityInput,
        *,
        snapshot: Mapping[str, Any],
        filtered_ids: tuple[str, ...],
        receipts: list[JsonDict],
        reason: str,
    ) -> InterruptibilityFrame:
        muted = bool(snapshot.get("response_mute", {}).get("value"))
        response_obligation = "forbidden" if muted else ("required" if policy_input.user_input_present else "optional")
        shape = "brief"
        surfaces = StateSurfacePermission(
            memory="silent", relationship="silent", worldops="silent", affect="silent", persona="silent"
        )
        return InterruptibilityFrame(
            turn_id=policy_input.turn_id,
            correlation_id=policy_input.correlation_id,
            availability="unknown",
            response_obligation=response_obligation,
            expansion_permission="blocked",
            topic_shift_permission="blocked",
            follow_up_permission="none",
            initiative_permission="blocked",
            state_surface_permission=surfaces,
            response_shape=shape,
            decision_path="engine_failure_fallback",
            model_assist=ModelAssistTrace(
                fallback_used=True,
                fallback_reason=reason[:240],
                deadline_ms=self.provider_deadline_ms,
                min_confidence=self.provider_min_confidence,
            ),
            budgets=self._project_budgets(response_obligation, "blocked", "blocked", "none", shape, surfaces),
            hard_overrides=("response_mute",) if muted else (),
            evidence=(),
            reason_codes=("ENGINE_FAILURE_FALLBACK", "NO_PRESENCE_EVIDENCE"),
            confidence=1.0 if muted else 0.5,
            policy_profile=self.policy_profile,
            policy_version=self.policy_version,
            trace_id=_new_id("itrace"),
            control_receipts=tuple(receipts),
            filtered_feature_ids=filtered_ids,
        )

    def _resolve_with_provider(
        self,
        interpretation: Mapping[str, Any],
        values: JsonDict,
        unresolved: tuple[str, ...],
        evidence: tuple[PolicyEvidence, ...],
    ) -> tuple[JsonDict, ModelAssistTrace]:
        request_id = _new_id("iresolve")
        allowed_choices = self._allowed_choices(values, unresolved)
        request = BoundedResolutionRequest(
            request_id=request_id,
            turn_interpretation=interpretation,
            unresolved_dimensions=unresolved,
            allowed_choices=allowed_choices,
            permission_ceiling={key: values[key] for key in unresolved},
            conservative_defaults={key: values[key] for key in unresolved},
            evidence=evidence,
            policy_version=self.policy_version,
            deadline_ms=self.provider_deadline_ms,
            min_confidence=self.provider_min_confidence,
        )
        started = time.perf_counter()
        try:
            resolution = self.provider.resolve(request) if self.provider else None
            latency = round((time.perf_counter() - started) * 1000, 2)
            if resolution is None:
                raise ValueError("provider returned no resolution")
            invalid_reason = self._validate_resolution(request, resolution, latency)
            if invalid_reason:
                return values, ModelAssistTrace(
                    invoked=True,
                    provider=resolution.provider,
                    reason="unresolved_policy_dimensions",
                    requested_dimensions=unresolved,
                    rejected_dimensions=tuple(resolution.decisions),
                    deadline_ms=self.provider_deadline_ms,
                    min_confidence=self.provider_min_confidence,
                    latency_ms=latency,
                    fallback_used=True,
                    fallback_reason=invalid_reason,
                )
            accepted: list[str] = []
            rejected: list[str] = []
            updated = dict(values)
            evidence_ids = {item.evidence_id for item in evidence}
            for dimension, decision in resolution.decisions.items():
                more_permissive = self._rank(dimension, decision) > self._rank(dimension, values[dimension])
                if more_permissive and (
                    resolution.confidence < 0.90
                    or not set(resolution.evidence_refs).intersection(evidence_ids)
                ):
                    rejected.append(dimension)
                    continue
                updated[dimension] = decision
                accepted.append(dimension)
            return updated, ModelAssistTrace(
                invoked=True,
                provider=resolution.provider,
                reason="unresolved_policy_dimensions",
                requested_dimensions=unresolved,
                accepted_dimensions=tuple(accepted),
                rejected_dimensions=tuple(rejected),
                deadline_ms=self.provider_deadline_ms,
                min_confidence=self.provider_min_confidence,
                latency_ms=latency,
                fallback_used=not accepted and bool(unresolved),
                fallback_reason="no_provider_decision_accepted" if not accepted else None,
            )
        except Exception as exc:
            latency = round((time.perf_counter() - started) * 1000, 2)
            return values, ModelAssistTrace(
                invoked=True,
                reason="unresolved_policy_dimensions",
                requested_dimensions=unresolved,
                deadline_ms=self.provider_deadline_ms,
                min_confidence=self.provider_min_confidence,
                latency_ms=latency,
                fallback_used=True,
                fallback_reason=f"{type(exc).__name__}:{exc}"[:240],
            )

    def _validate_resolution(
        self,
        request: BoundedResolutionRequest,
        resolution: BoundedResolution,
        latency_ms: float,
    ) -> str | None:
        if latency_ms > request.deadline_ms:
            return "provider_deadline_exceeded"
        if resolution.schema_version != "bounded_resolution.v1":
            return "invalid_schema_version"
        if resolution.request_id != request.request_id:
            return "request_id_mismatch"
        if not set(resolution.decisions).issubset(request.unresolved_dimensions):
            return "provider_returned_unrequested_dimension"
        for dimension, value in resolution.decisions.items():
            if value not in request.allowed_choices.get(dimension, ()):
                return f"provider_value_outside_allowed_choices:{dimension}"
        if resolution.confidence < request.min_confidence:
            return "provider_confidence_below_threshold"
        return None

    def _allowed_choices(self, values: Mapping[str, str], unresolved: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        domains = {
            "expansion_permission": EXPANSION_PERMISSIONS,
            "topic_shift_permission": TOPIC_SHIFT_PERMISSIONS,
            "follow_up_permission": FOLLOW_UP_PERMISSIONS,
            "response_shape": RESPONSE_SHAPES,
        }
        choices: dict[str, tuple[str, ...]] = {}
        for dimension in unresolved:
            ceiling = values[dimension]
            ceiling_rank = self._rank(dimension, ceiling)
            choices[dimension] = tuple(
                value for value in domains[dimension] if self._rank(dimension, value) <= ceiling_rank
            )
        return choices

    @staticmethod
    def _rank(dimension: str, value: str) -> int:
        mapping = {
            "expansion_permission": _EXPANSION_RANK,
            "topic_shift_permission": _TOPIC_RANK,
            "follow_up_permission": _FOLLOW_UP_RANK,
            "response_shape": _SHAPE_RANK,
        }[dimension]
        return mapping.get(value, -1)

    def _unresolved_dimensions(
        self,
        interpretation: Mapping[str, Any],
        *,
        mute_active: bool,
        risk: str,
        resolved_dimensions: set[str] | None = None,
    ) -> list[str]:
        if mute_active or risk == "high":
            return []
        assist = interpretation.get("interruptibility")
        if not isinstance(assist, Mapping):
            return []
        requested = assist.get("unresolved_dimensions", [])
        if not assist.get("ambiguous_boundary") and not requested:
            return []
        resolved = resolved_dimensions or set()
        return [
            str(item)
            for item in requested
            if str(item) in INTERRUPTIBILITY_DIMENSIONS and str(item) not in resolved
        ]

    @staticmethod
    def _project_budgets(
        response_obligation: str,
        expansion: str,
        topic_shift: str,
        follow_up: str,
        shape: str,
        surfaces: StateSurfacePermission,
    ) -> ResponseBudgets:
        if response_obligation == "forbidden":
            return ResponseBudgets(0, 0, 0, 0)
        max_sentences = {
            "ack": 2,
            "brief": 2,
            "repair_brief": 2,
            "normal": 5,
            "deep": 10,
            "safety": 8,
        }[shape]
        if expansion == "blocked":
            max_sentences = min(max_sentences, 2)
        question_budget = {"none": 0, "one": 1, "multiple": 2}[follow_up]
        topic_budget = 0 if topic_shift in {"blocked", "same_topic"} else (1 if topic_shift == "adjacent" else 2)
        surface_values = surfaces.to_dict().values()
        surface_budget = sum(1 for item in surface_values if item != "silent")
        if expansion == "blocked":
            surface_budget = min(surface_budget, 1)
        return ResponseBudgets(max_sentences, question_budget, topic_budget, surface_budget)

    def control_snapshot(
        self,
        state: JsonDict,
        *,
        turn_id: str | None = None,
        turn_values: Mapping[str, JsonDict] | None = None,
        now: int | None = None,
    ) -> JsonDict:
        ensure_interruptibility_state(state)
        current_time = int(now or _now_ms())
        current_turn = str(turn_id or state.get("session", {}).get("turn_id", "unknown"))
        result: JsonDict = {
            "quiet": {"value": False, "scope": "default"},
            "response_mute": {"value": False, "scope": "default"},
            "reply_depth": {"value": "balanced", "scope": "default"},
            "follow_up": {"value": "one", "scope": "default"},
            "topic_shift": {"value": "same_topic", "scope": "default"},
            "state_surface": {"value": "implicit", "scope": "default"},
        }
        profile = state["interruptibility"]["profile"].get("persistent_preferences", {})
        for key in ("reply_depth", "follow_up", "topic_shift", "state_surface"):
            if key in profile:
                result[key] = {"value": profile[key], "scope": "persistent_profile"}
        candidates = []
        session_id = state.get("session", {}).get("session_id")
        for override in state["interruptibility"].get("overrides", []):
            if not self._override_active(override, current_turn, session_id, current_time):
                continue
            candidates.append(override)
        candidates.sort(key=lambda item: (_SCOPE_PRIORITY.get(str(item.get("scope")), 0), int(item.get("created_at", 0))))
        for override in candidates:
            result[str(override["control"])] = {
                "value": override.get("value"),
                "scope": override.get("scope"),
                "override_id": override.get("override_id"),
                "expires_at": override.get("expires_at"),
            }
        for control, payload in (turn_values or {}).items():
            result[str(control)] = {"value": payload.get("value"), "scope": "turn", "event_id": payload.get("event_id")}
        return result

    def _merge_control_events(
        self,
        parsed: list[CanonicalControlEvent],
        interpretation: Mapping[str, Any],
    ) -> list[CanonicalControlEvent]:
        events = list(parsed)
        deterministic_hard = {(item.control, str(item.value)) for item in parsed}
        raw_events = interpretation.get("explicit_commands", [])
        if not isinstance(raw_events, list):
            return events
        allowed_controls = {
            "quiet",
            "response_mute",
            "reply_depth",
            "follow_up",
            "topic_shift",
            "state_surface",
            "initiative_lease",
            "resume",
            "confirm",
            "revoke",
            "status",
        }
        seen = {(item.control, str(item.value), item.scope) for item in events}
        for raw in raw_events[:8]:
            if not isinstance(raw, Mapping):
                continue
            control = str(raw.get("control") or "")
            value = raw.get("value")
            confidence = _clamp01(raw.get("confidence", 0.0))
            if control not in allowed_controls or confidence < 0.85:
                continue
            if control in {"quiet", "response_mute", "resume"} and (control, str(value)) not in deterministic_hard:
                continue
            scope = _safe_enum(raw.get("scope"), ("turn", "session", "duration", "persistent"), "turn")
            key = (control, str(value), scope)
            if key in seen:
                continue
            events.append(
                CanonicalControlEvent(
                    event_id=_new_id("ctrl"),
                    control=control,
                    value=value,
                    scope=scope,
                    source="turn_interpreter",
                    duration_seconds=int(raw.get("duration_seconds") or 0) or None,
                    requires_confirmation=bool(raw.get("requires_confirmation", False))
                    or (scope == "persistent" and control in {"response_mute", "state_surface"}),
                    confidence=confidence,
                )
            )
            seen.add(key)
        return events

    def _apply_control_events(
        self,
        state: JsonDict,
        events: list[CanonicalControlEvent],
        *,
        turn_id: str,
        now: int,
    ) -> JsonDict:
        runtime = state["interruptibility"]
        turn_values: JsonDict = {}
        receipts: list[JsonDict] = []
        for event in events:
            if event.control == "status":
                receipts.append({"event_id": event.event_id, "status": "accepted", "action": "show_status"})
                continue
            if event.control == "resume":
                count = self._revoke_matching(state, {"quiet", "response_mute"}, reason="explicit_resume", now=now)
                receipts.append({"event_id": event.event_id, "status": "applied", "action": "resume", "revoked_count": count})
                runtime["session"]["last_control_at"] = now
                continue
            if event.control == "revoke":
                controls = {"quiet", "response_mute", "reply_depth", "follow_up", "topic_shift", "state_surface"}
                count = self._revoke_matching(state, controls, reason="explicit_revoke", now=now)
                receipts.append({"event_id": event.event_id, "status": "applied", "action": "revoke", "revoked_count": count})
                continue
            if event.control == "confirm":
                receipt = self._confirm_pending(state, token=event.value, turn_id=turn_id, now=now)
                receipt["event_id"] = event.event_id
                receipts.append(receipt)
                continue
            if event.control == "initiative_lease":
                runtime["session"]["initiative_lease"] = {
                    "lease_id": _new_id("lease"),
                    "active": True,
                    "created_at": now,
                    "last_user_activity_at": now,
                    "expires_at": now + LEASE_INACTIVITY_SECONDS * 1000,
                    "source_event_id": event.event_id,
                }
                receipts.append({"event_id": event.event_id, "status": "applied", "action": "initiative_lease"})
                continue
            if event.requires_confirmation:
                pending = {
                    "confirmation_token": _new_id("confirm"),
                    "event": event.to_dict(),
                    "status": "pending",
                    "created_at": now,
                    "expires_at": now + PENDING_CONFIRMATION_SECONDS * 1000,
                }
                runtime["pending_confirmations"].append(pending)
                receipts.append({
                    "event_id": event.event_id,
                    "status": "pending_confirmation",
                    "control": event.control,
                    "confirmation_token": pending["confirmation_token"],
                    "expires_at": pending["expires_at"],
                })
                continue
            if event.scope == "turn":
                turn_values[event.control] = {"value": event.value, "event_id": event.event_id}
                receipts.append({"event_id": event.event_id, "status": "applied", "scope": "turn", "control": event.control})
                continue
            override = self._create_override(state, event, turn_id=turn_id, now=now)
            receipts.append({
                "event_id": event.event_id,
                "status": "applied",
                "scope": event.scope,
                "control": event.control,
                "override_id": override["override_id"],
                "expires_at": override.get("expires_at"),
            })
        runtime["overrides"] = runtime["overrides"][-MAX_OVERRIDE_HISTORY:]
        runtime["pending_confirmations"] = runtime["pending_confirmations"][-100:]
        return {"events": events, "turn_values": turn_values, "receipts": receipts}

    def _create_override(
        self,
        state: JsonDict,
        event: CanonicalControlEvent,
        *,
        turn_id: str,
        now: int,
        confirmation_token: str | None = None,
        effective_from_turn: int | None = None,
    ) -> JsonDict:
        expires_at = None
        if event.scope == "duration":
            expires_at = now + int(event.duration_seconds or 0) * 1000
        override = {
            "override_id": _new_id("ioverride"),
            "control": event.control,
            "value": event.value,
            "scope": event.scope,
            "source": event.source,
            "session_id": state.get("session", {}).get("session_id") if event.scope in {"session", "duration"} else None,
            "created_at": now,
            "created_turn_id": turn_id,
            "effective_from_turn": effective_from_turn,
            "expires_at": expires_at,
            "confirmation_token": confirmation_token,
            "revoked_at": None,
            "revoke_reason": None,
        }
        state["interruptibility"]["overrides"].append(override)
        if event.scope == "persistent" and event.control in {
            "reply_depth",
            "follow_up",
            "topic_shift",
            "state_surface",
        }:
            profile = state["interruptibility"]["profile"]
            profile["persistent_preferences"][event.control] = event.value
            profile["updated_at"] = now
        return override

    def _confirm_pending(self, state: JsonDict, *, token: Any, turn_id: str, now: int) -> JsonDict:
        candidates = [
            item for item in state["interruptibility"].get("pending_confirmations", [])
            if item.get("status") == "pending" and int(item.get("expires_at", 0)) > now
        ]
        if token:
            candidates = [item for item in candidates if item.get("confirmation_token") == token]
        if not candidates:
            return {"status": "not_found", "action": "confirm"}
        pending = max(candidates, key=lambda item: int(item.get("created_at", 0)))
        raw = pending["event"]
        event = CanonicalControlEvent(
            event_id=str(raw["event_id"]),
            control=str(raw["control"]),
            value=raw.get("value"),
            scope=str(raw.get("scope") or "persistent"),
            source=str(raw.get("source") or "natural_language"),
            duration_seconds=raw.get("duration_seconds"),
            requires_confirmation=False,
            confidence=_clamp01(raw.get("confidence", 1.0)),
        )
        effective_from = int(turn_id) + 1 if event.control == "response_mute" and str(turn_id).isdigit() else None
        override = self._create_override(
            state,
            event,
            turn_id=turn_id,
            now=now,
            confirmation_token=str(pending["confirmation_token"]),
            effective_from_turn=effective_from,
        )
        pending["status"] = "confirmed"
        pending["confirmed_at"] = now
        proof = {
            "confirmation_token_hash": _hash_text(str(pending["confirmation_token"])),
            "control": event.control,
            "confirmed_at": now,
        }
        state["interruptibility"]["profile"]["confirmation_proofs"].append(proof)
        if event.scope == "persistent" and event.control in {"reply_depth", "follow_up", "topic_shift", "state_surface"}:
            state["interruptibility"]["profile"]["persistent_preferences"][event.control] = event.value
            state["interruptibility"]["profile"]["updated_at"] = now
        return {
            "status": "applied",
            "action": "confirm",
            "control": event.control,
            "override_id": override["override_id"],
            "effective_from_turn": effective_from,
        }

    @staticmethod
    def _override_active(override: Mapping[str, Any], turn_id: str, session_id: Any, now: int) -> bool:
        if override.get("revoked_at"):
            return False
        if override.get("expires_at") and int(override["expires_at"]) <= now:
            return False
        if override.get("scope") in {"session", "duration"} and override.get("session_id") != session_id:
            return False
        effective = override.get("effective_from_turn")
        if effective is not None and str(turn_id).isdigit() and int(turn_id) < int(effective):
            return False
        return True

    def _revoke_matching(self, state: JsonDict, controls: set[str], *, reason: str, now: int) -> int:
        count = 0
        for override in state["interruptibility"].get("overrides", []):
            if override.get("control") in controls and not override.get("revoked_at"):
                override["revoked_at"] = now
                override["revoke_reason"] = reason
                count += 1
        profile = state["interruptibility"]["profile"]["persistent_preferences"]
        defaults = default_interruptibility_state()["profile"]["persistent_preferences"]
        for control in controls.intersection(defaults):
            profile[control] = defaults[control]
        if controls.intersection({"quiet", "response_mute"}):
            lease = state["interruptibility"]["session"].get("initiative_lease")
            if lease and reason != "explicit_resume":
                lease["active"] = False
                lease["ended_reason"] = reason
                lease["ended_at"] = now
        return count

    def _update_activity_lease(
        self,
        state: JsonDict,
        events: list[CanonicalControlEvent],
        snapshot: Mapping[str, Any],
        *,
        user_input: str,
        turn_kind: str,
        now: int,
    ) -> None:
        session = state["interruptibility"]["session"]
        lease = session.get("initiative_lease")
        stop_controls = {event.control for event in events}.intersection({"quiet", "response_mute", "revoke"})
        if lease and stop_controls:
            lease["active"] = False
            lease["ended_at"] = now
            lease["ended_reason"] = f"control:{sorted(stop_controls)[0]}"
            return
        if not lease or not lease.get("active"):
            return
        if int(lease.get("expires_at", 0)) <= now:
            lease["active"] = False
            lease["ended_at"] = now
            lease["ended_reason"] = "inactivity_ttl"
            return
        if (
            turn_kind == "reactive"
            and bool(user_input.strip())
            and not snapshot.get("quiet", {}).get("value")
            and not snapshot.get("response_mute", {}).get("value")
        ):
            lease["last_user_activity_at"] = now
            lease["expires_at"] = now + LEASE_INACTIVITY_SECONDS * 1000
            session["last_user_activity_at"] = now

    def _session_interaction_state(
        self,
        state: JsonDict,
        interpretation: Mapping[str, Any],
        now: int,
    ) -> JsonDict:
        runtime_session = state["interruptibility"]["session"]
        current_session_id = state.get("session", {}).get("session_id")
        if runtime_session.get("session_id") not in {None, current_session_id}:
            runtime_session.update(default_interruptibility_state()["session"])
        runtime_session["session_id"] = current_session_id
        move = str(interpretation.get("conversation_move", ""))
        speech_act = str(interpretation.get("speech_act", ""))
        if move == "continue_previous_topic" and speech_act in {"question", "request"}:
            runtime_session["user_led_depth_streak"] = int(runtime_session.get("user_led_depth_streak", 0)) + 1
        elif speech_act not in {"greeting", "status_update"}:
            runtime_session["user_led_depth_streak"] = 1
        lease = runtime_session.get("initiative_lease")
        if lease and lease.get("active") and int(lease.get("expires_at", 0)) <= now:
            lease["active"] = False
            lease["ended_reason"] = "inactivity_ttl"
            lease["ended_at"] = now
        return {
            "recent_sora_question_streak": self._recent_question_streak(state),
            "current_topic": state.get("session", {}).get("dialogue_state", {}).get("active_topic"),
            "user_led_depth_streak": runtime_session.get("user_led_depth_streak", 0),
            "last_user_activity_at": runtime_session.get("last_user_activity_at"),
            "initiative_lease": lease,
        }

    @staticmethod
    def _recent_question_streak(state: JsonDict) -> int:
        streak = 0
        for turn in reversed(state.get("session", {}).get("recent_turns", [])):
            if turn.get("role") != "sora":
                continue
            text = str(turn.get("content", ""))
            if "?" in text or "？" in text:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _is_closing_move(interpretation: Mapping[str, Any]) -> bool:
        assist = interpretation.get("interruptibility", {})
        return bool(isinstance(assist, Mapping) and assist.get("closing_move"))

    @staticmethod
    def _looks_like_control_repair(interpretation: Mapping[str, Any], control_result: Mapping[str, Any]) -> bool:
        if control_result.get("events"):
            return True
        assist = interpretation.get("interruptibility", {})
        return bool(isinstance(assist, Mapping) and assist.get("control_violation_correction"))

    def _maintain_state(self, state: JsonDict, now: int) -> None:
        runtime = state["interruptibility"]
        for pending in runtime.get("pending_confirmations", []):
            if pending.get("status") == "pending" and int(pending.get("expires_at", 0)) <= now:
                pending["status"] = "expired"
        cutoff = now - TRACE_RETENTION_MS
        runtime["traces"] = [
            item for item in runtime.get("traces", []) if int(item.get("created_at", 0)) >= cutoff
        ][-MAX_TRACES:]
        runtime["feedback"] = runtime.get("feedback", [])[-MAX_FEEDBACK:]

    def _record_trace(self, state: JsonDict, frame: JsonDict, *, user_input: str, timestamp_ms: int) -> None:
        trace = {
            "trace_id": frame["trace_id"],
            "correlation_id": frame["correlation_id"],
            "turn_id": frame["turn_id"],
            "created_at": timestamp_ms,
            "expires_at": timestamp_ms + TRACE_RETENTION_MS,
            "input_hash": _hash_text(user_input),
            "input_present": bool(user_input.strip()),
            "policy_profile": frame["policy_profile"],
            "policy_version": frame["policy_version"],
            "decision_path": frame["decision_path"],
            "hard_overrides": frame["hard_overrides"],
            "reason_codes": frame["reason_codes"],
            "filtered_feature_ids": frame["filtered_feature_ids"],
            "model_assist": frame["model_assist"],
            "frame": {
                key: frame[key]
                for key in (
                    "response_obligation",
                    "expansion_permission",
                    "topic_shift_permission",
                    "follow_up_permission",
                    "initiative_permission",
                    "state_surface_permission",
                    "response_shape",
                    "budgets",
                )
            },
            "control_receipts": _redact_control_receipts(frame["control_receipts"]),
            "enforcement": None,
        }
        state["interruptibility"]["traces"].append(trace)
        state["interruptibility"]["traces"] = state["interruptibility"]["traces"][-MAX_TRACES:]

    def record_enforcement(
        self,
        state: JsonDict,
        *,
        trace_id: str,
        emitted: bool,
        output_text: str,
        response_plan: Mapping[str, Any] | None,
        route: str | None,
    ) -> JsonDict:
        trace = next(
            (item for item in reversed(state.get("interruptibility", {}).get("traces", [])) if item.get("trace_id") == trace_id),
            None,
        )
        if trace is None:
            raise ValueError(f"interruptibility trace not found: {trace_id}")
        frame = trace["frame"]
        questions = output_text.count("?") + output_text.count("？")
        sentences = _sentence_count(output_text)
        violations: list[str] = []
        if frame["response_obligation"] == "forbidden" and (emitted or output_text):
            violations.append("response_mute_leakage")
        if questions > int(frame["budgets"]["question_budget"]):
            violations.append("question_budget_exceeded")
        if sentences > int(frame["budgets"]["max_sentences"]):
            violations.append("sentence_budget_exceeded")
        if frame["response_obligation"] == "required" and (not emitted or not output_text.strip()):
            violations.append("required_response_missing")
        control_lags: list[int] = []
        current_turn = str(trace.get("turn_id", ""))
        if current_turn.isdigit():
            for override in state.get("interruptibility", {}).get("overrides", []):
                if override.get("revoked_at") or override.get("first_applied_turn") is not None:
                    continue
                intended_turn = override.get("effective_from_turn") or override.get("created_turn_id")
                if str(intended_turn).isdigit() and int(current_turn) >= int(intended_turn):
                    override["first_applied_turn"] = int(current_turn)
                    override["first_applied_at"] = _now_ms()
                    control_lags.append(int(current_turn) - int(intended_turn))
        enforcement = {
            "recorded_at": _now_ms(),
            "emitted": emitted,
            "route": route,
            "output_hash": _hash_text(output_text),
            "question_count": questions,
            "sentence_count": sentences,
            "planner_reported": dict((response_plan or {}).get("interruptibility_compliance", {})),
            "violations": violations,
            "compliant": not violations,
            "control_effect_turn_lag": (
                max(control_lags)
                if control_lags
                else (0 if trace.get("control_receipts") else None)
            ),
        }
        trace["enforcement"] = enforcement
        state["interruptibility"]["last_enforcement"] = enforcement
        return enforcement

    def record_feedback(self, state: JsonDict, *, kind: str, trace_id: str | None = None) -> JsonDict:
        ensure_interruptibility_state(state)
        feedback = {
            "feedback_id": _new_id("ifeedback"),
            "kind": kind,
            "trace_id": trace_id,
            "created_at": _now_ms(),
        }
        state["interruptibility"]["feedback"].append(feedback)
        state["interruptibility"]["feedback"] = state["interruptibility"]["feedback"][-MAX_FEEDBACK:]
        return feedback

    def status(self, state: JsonDict) -> JsonDict:
        ensure_interruptibility_state(state)
        now = _now_ms()
        return {
            "schema_version": state["interruptibility"]["schema_version"],
            "policy_profile": self.policy_profile,
            "policy_version": self.policy_version,
            "controls": self.control_snapshot(state, now=now),
            "initiative_lease": state["interruptibility"]["session"].get("initiative_lease"),
            "pending_confirmations": [
                {
                    "confirmation_token": item.get("confirmation_token"),
                    "control": item.get("event", {}).get("control"),
                    "status": item.get("status"),
                    "expires_at": item.get("expires_at"),
                }
                for item in state["interruptibility"].get("pending_confirmations", [])
                if item.get("status") == "pending" and int(item.get("expires_at", 0)) > now
            ],
            "trace_count": len(state["interruptibility"].get("traces", [])),
        }

    def set_control(
        self,
        state: JsonDict,
        *,
        control: str,
        value: Any,
        scope: str = "session",
        duration_seconds: int | None = None,
    ) -> JsonDict:
        if control not in {"quiet", "response_mute", "reply_depth", "follow_up", "topic_shift", "state_surface", "initiative_lease"}:
            raise ValueError(f"unsupported interruptibility control: {control}")
        selected_scope = _safe_enum(scope, ("turn", "session", "duration", "persistent"), "session")
        event = CanonicalControlEvent(
            event_id=_new_id("ctrl_admin"),
            control=control,
            value=value,
            scope=selected_scope,
            source="admin",
            duration_seconds=duration_seconds,
            requires_confirmation=(selected_scope == "persistent" and control in {"response_mute", "state_surface"}),
        )
        result = self._apply_control_events(
            state,
            [event],
            turn_id=str(state.get("session", {}).get("turn_id", "unknown")),
            now=_now_ms(),
        )
        return {"event": event.to_dict(), "receipts": result["receipts"], "status": self.status(state)}

    def confirm(self, state: JsonDict, token: str | None = None) -> JsonDict:
        return self._confirm_pending(
            state,
            token=token,
            turn_id=str(state.get("session", {}).get("turn_id", "unknown")),
            now=_now_ms(),
        )

    def revoke(self, state: JsonDict, control: str = "all") -> JsonDict:
        supported = {"quiet", "response_mute", "reply_depth", "follow_up", "topic_shift", "state_surface"}
        selected = supported if control == "all" else {control}
        unknown = selected - supported
        if unknown:
            raise ValueError(f"unsupported interruptibility control: {sorted(unknown)[0]}")
        count = self._revoke_matching(state, selected, reason="admin_revoke", now=_now_ms())
        return {"status": "applied", "revoked_count": count, "controls": sorted(selected)}

    def list_traces(self, state: JsonDict, *, limit: int = 50) -> list[JsonDict]:
        ensure_interruptibility_state(state)
        return list(state["interruptibility"].get("traces", []))[-max(1, min(limit, 500)):]

    def export_traces(
        self,
        state: JsonDict,
        *,
        output_path: str | Path | None = None,
        limit: int = 500,
    ) -> JsonDict:
        payload = {
            "schema_version": "interruptibility_trace_export.v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "policy_profile": self.policy_profile,
            "policy_version": self.policy_version,
            "traces": self.list_traces(state, limit=limit),
        }
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["output_path"] = str(path)
        return payload


def _sentence_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    parts = [item for item in re.split(r"(?<=[。！？.!?])\s*|\n+", stripped) if item.strip()]
    return max(1, len(parts))


def _redact_control_receipts(receipts: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    redacted: list[JsonDict] = []
    for receipt in receipts:
        item = dict(receipt)
        token = item.pop("confirmation_token", None)
        if token:
            item["confirmation_token_hash"] = _hash_text(str(token))
        redacted.append(item)
    return redacted


def tighten_dialogue_control(dialogue_control: JsonDict, frame: Mapping[str, Any]) -> JsonDict:
    budgets = frame.get("budgets", {})
    question_budget = min(
        int(dialogue_control.get("question_budget", 0) or 0),
        int(budgets.get("question_budget", 0) or 0),
    )
    # Explicit frame permission may open one question even when the legacy
    # heuristic was conservative; the frame is now the canonical authority.
    if frame.get("follow_up_permission") in {"one", "multiple"} and not dialogue_control.get("hard_question_block"):
        question_budget = int(budgets.get("question_budget", 0) or 0)
    dialogue_control.update(
        {
            "should_ask_question": question_budget > 0,
            "question_budget": question_budget,
            "interruptibility_source": frame.get("schema_version"),
            "response_shape": frame.get("response_shape"),
            "max_sentences": budgets.get("max_sentences"),
            "topic_shift_budget": budgets.get("topic_shift_budget", 0),
            "state_surface_budget": budgets.get("state_surface_budget", 0),
        }
    )
    if question_budget == 0:
        dialogue_control["preferred_closing"] = "brief_statement"
    return dialogue_control


def enforce_interruptibility_output(text: str, frame: Mapping[str, Any]) -> tuple[str, JsonDict]:
    if frame.get("response_obligation") == "forbidden":
        return "", {"muted": True, "question_trimmed": False, "sentence_trimmed": False}
    budgets = frame.get("budgets", {})
    output = text.strip()
    question_budget = int(budgets.get("question_budget", 0) or 0)
    before_questions = output.count("?") + output.count("？")
    if before_questions > question_budget:
        output = _trim_questions(output, question_budget)
    max_sentences = int(budgets.get("max_sentences", 5) or 5)
    before_sentences = _sentence_count(output)
    if before_sentences > max_sentences:
        output = _truncate_sentences(output, max_sentences)
    return output.strip(), {
        "muted": False,
        "question_trimmed": before_questions > question_budget,
        "sentence_trimmed": before_sentences > max_sentences,
        "question_count": output.count("?") + output.count("？"),
        "sentence_count": _sentence_count(output),
        "question_budget": question_budget,
        "max_sentences": max_sentences,
    }


def _trim_questions(text: str, budget: int) -> str:
    seen = 0
    output: list[str] = []
    for character in text:
        if character in {"?", "？"}:
            seen += 1
            output.append(character if seen <= budget else "。")
        else:
            output.append(character)
    return "".join(output)


def _truncate_sentences(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    chunks = [item for item in re.split(r"(?<=[。！？.!?])\s*|\n+", text) if item.strip()]
    return " ".join(chunks[:limit]).strip()
