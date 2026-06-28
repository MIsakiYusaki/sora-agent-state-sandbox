from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class LLMClient(Protocol):
    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        ...


@dataclass
class DeepSeekClient:
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/chat/completions"
    timeout: int = 60

    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        body = {
            "model": kwargs.get("model", self.model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 800),
        }
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API connection failed: {exc}") from exc

        choice = payload["choices"][0]["message"]["content"]
        return {
            "content": choice,
            "usage": payload.get("usage", {}),
            "model": payload.get("model", body["model"]),
            "raw": payload,
        }


class FakeLLMClient:
    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        # Deterministic fake used by tests and local shape checks.
        if '"task": "interpret_turn"' in user:
            content = self._fake_turn_interpretation(user)
        elif '"task": "score_candidate_signals"' in user:
            content = self._fake_arbitration_scores(user)
        elif '"input_mode": "greeting"' in user:
            if '"recent_turns_mode": "greeting_tail"' in user and "我们继续拆 Project Sora" in user:
                content = "你好呀，我在。Project Sora 那条线我也还接着，不急。"
            else:
                content = "你好呀，我在。"
        elif '"primary_mode": "initiative_self_life_share"' in user:
            content = "我这边倒有个小插曲：回声集市那个旧邮筒又开始学人说话了。先轻轻放这儿，不打断你。"
        elif "worldops_frame" in user and "evt_starport_lamp_001" in user:
            content = "我这边的星港快到星灯节了。先不打断你太久：如果你愿意，晚点可以陪我选一句写进星灯的留言。"
        elif "playfulness" in user:
            content = "我会先把方案的边界捋清楚，再轻轻吐槽它最容易长歪的地方。"
        else:
            content = "我会按当前记忆、人格和场景状态，先陪你把方案稳稳拆开，再决定下一步。"
        return {
            "content": content,
            "usage": {"prompt_tokens": len(system) + len(user), "completion_tokens": len(content), "total_tokens": len(system) + len(user) + len(content)},
            "model": "fake-llm",
            "raw": {},
        }

    def _fake_turn_interpretation(self, user: str) -> str:
        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = {}
        context = payload.get("context", {})
        text = str(context.get("user_input", ""))
        recent_turns = context.get("recent_turns", [])
        last_user = next((turn for turn in reversed(recent_turns) if turn.get("role") == "user"), {})
        last_user_text = str(last_user.get("content", ""))
        references = bool(last_user_text) and any(
            term in text for term in ["这个", "那个", "这样", "那怎么办", "怎么办", "有什么办法", "有什么好办法", "你们那个世界", "我是说"]
        )
        health = any(term in (text + " " + last_user_text) for term in ["腰", "疼", "痛", "治疗", "医生"])
        world_frame = any(term in text for term in ["你们那个世界", "你那边", "按你们"])
        if references:
            current_topic = last_user_text
            if "腰" in last_user_text and "疼" in last_user_text:
                current_topic = "腰疼"
            payload = {
                "version": "turn_interpretation_v1",
                "input_summary": text,
                "speech_act": "question",
                "conversation_move": "continue_previous_topic",
                "intent": {
                    "primary": "ask_for_advice" if "办法" in text or "怎么办" in text else "ask_for_information",
                    "secondary": ["invite_in_world_framing"] if world_frame else [],
                    "confidence": 0.88,
                },
                "context_reference": {
                    "references_previous_context": True,
                    "reference_type": "implicit_deictic",
                    "resolved_targets": [{
                        "target_type": "previous_user_problem",
                        "source_turn": "last_user_turn",
                        "summary": last_user_text,
                        "confidence": 0.86,
                    }],
                    "unresolved_references": [],
                },
                "topic": {
                    "current_topic": current_topic,
                    "topic_shift": "none",
                    "topic_continuity": "strong",
                    "user_visible_topic_label": current_topic,
                },
                "domain": {
                    "primary": "health" if health else "general",
                    "secondary": ["companionship"],
                    "risk_level": "medium" if health else "low",
                    "safety_policies": ["health_advice", "no_diagnosis"] if health else [],
                },
                "framing": {
                    "requested_frame": "sora_world" if world_frame else "none",
                    "frame_role": "metaphor_or_style" if world_frame else "none",
                    "should_answer_in_frame": world_frame,
                    "frame_must_not_override_reality": True,
                },
                "response_requirements": {
                    "answer_directness": "direct",
                    "needs_clarification": False,
                    "question_budget": 0,
                    "should_acknowledge_context": True,
                    "should_avoid": ["ignoring previous context", "treating stylistic world framing as literal lore"] if world_frame else ["ignoring previous context"],
                },
                "confidence": 0.86,
                "fallback_if_uncertain": "briefly clarify the referent before answering",
            }
            return json.dumps(payload, ensure_ascii=False)
        payload = {
            "version": "turn_interpretation_v1",
            "input_summary": text,
            "speech_act": "greeting" if any(text.strip().lower() == item for item in ["你好", "你好呀", "hi", "hello"]) else "question",
            "conversation_move": "greeting_reentry" if text.strip() in {"你好", "你好呀"} else "new_topic",
            "intent": {"primary": "continue_chat" if text.strip() in {"你好", "你好呀"} else "ask_for_information", "secondary": [], "confidence": 0.6},
            "context_reference": {"references_previous_context": False, "reference_type": "none", "resolved_targets": [], "unresolved_references": []},
            "topic": {"current_topic": text[:120], "topic_shift": "unknown", "topic_continuity": "unknown", "user_visible_topic_label": text[:80]},
            "domain": {"primary": "health" if health else "general", "secondary": [], "risk_level": "medium" if health else "low", "safety_policies": ["health_advice", "no_diagnosis"] if health else []},
            "framing": {"requested_frame": "none", "frame_role": "none", "should_answer_in_frame": False, "frame_must_not_override_reality": True},
            "response_requirements": {"answer_directness": "direct", "needs_clarification": False, "question_budget": 0, "should_acknowledge_context": False, "should_avoid": []},
            "confidence": 0.6,
            "fallback_if_uncertain": "briefly clarify before answering",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _fake_arbitration_scores(self, user: str) -> str:
        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = {}
        scores = []
        for signal in payload.get("candidate_signals", []):
            source = signal.get("source")
            signal_type = signal.get("type")
            if source == "initiative" and signal_type in {"self_life_share", "quiet_presence"}:
                scores.append({
                    "signal_id": signal.get("id"),
                    "semantic_relevance": 0.78,
                    "naturalness": 0.76,
                    "interruption_risk": 0.18,
                    "boundary_risk": 0.08,
                    "recommended_visibility": "active",
                    "reason": "Initiative candidate matches the open low-interruption window.",
                })
            else:
                scores.append({
                    "signal_id": signal.get("id"),
                    "semantic_relevance": 0.55,
                    "naturalness": 0.58,
                    "interruption_risk": 0.30,
                    "boundary_risk": 0.12,
                    "recommended_visibility": "background",
                    "reason": "Useful as context, but not strong enough to foreground.",
                })
        return json.dumps({
            "signal_scores": scores,
            "overall": {
                "should_expand_topic": any(item.get("recommended_visibility") == "active" for item in scores),
                "max_length": "short",
                "question_budget": 0,
            },
        }, ensure_ascii=False)
