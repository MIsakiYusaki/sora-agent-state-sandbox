from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from ..memory_schema import new_id, utc_now
from .contracts import (
    AFFECT_APPRAISAL_CONTRACT,
    AFFECT_DYNAMICS_VERSION,
    AFFECT_FRAME_CONTRACT,
    AFFECT_SIGNAL_CONTRACT,
    AffectAppraisal,
    AffectStimulusEvent,
    JsonDict,
    bounded,
    capacity_band,
    mood_band,
    normalized,
    normalized_vector,
)


FACTOR_WEIGHTS = {
    "persona_sensitivity": 0.18,
    "relationship_modifier": 0.18,
    "goal_importance": 0.22,
    "novelty_modifier": 0.12,
    "capacity_modifier": 0.10,
    "evidence": 0.20,
}

FAST_EVENT_MAP: dict[str, tuple[str | None, float, tuple[str, ...]]] = {
    "neutral_interaction": (None, 0.0, ("no_fast_affect_signal",)),
    "explicit_affection": ("affection", 0.60, ("explicit_positive_relation_signal",)),
    "gratitude": ("joy", 0.42, ("explicit_gratitude",)),
    "apology": ("relief", 0.30, ("repair_attempt",)),
    "correction": ("guilt", 0.32, ("explicit_correction", "repair_needed")),
    "hostility": ("frustration", 0.42, ("explicit_hostility", "bounded_negative_response")),
    "user_distress": ("sadness", 0.22, ("empathic_appraisal", "anti_mirroring_cap")),
    "curiosity_invite": ("curiosity", 0.40, ("exploration_invitation",)),
    "goal_progress": ("contentment", 0.45, ("goal_progress",)),
    "goal_completed": ("joy", 0.58, ("goal_completed",)),
    "worldops_uncertainty": ("anxiety", 0.30, ("worldops_uncertainty",)),
    "relationship_repair": ("relief", 0.52, ("relationship_repair",)),
    "relationship_rupture": ("sadness", 0.48, ("relationship_rupture",)),
}


class AffectDynamicsEngine:
    """Deterministic Affect integration, replay, and frame construction."""

    def __init__(self, profile: Mapping[str, Any]) -> None:
        self.profile = copy.deepcopy(dict(profile))
        self.profile_version = str(self.profile.get("version") or AFFECT_DYNAMICS_VERSION)
        self.enabled_emotions = set(self.profile.get("enabled_emotions") or [])
        self.emotion_specs = dict(self.profile.get("emotion_specs") or {})

    def fast_appraise(self, event: AffectStimulusEvent, snapshot: Mapping[str, Any]) -> AffectAppraisal:
        event.validate()
        structured = dict(event.payload.get("structured_appraisal") or {})
        if structured:
            emotion_type = structured.get("emotion_type")
            strength = bounded(float(structured.get("strength", 0.0)))
            reason_codes = tuple(str(item) for item in structured.get("reason_codes") or ("structured_fast_signal",))
            vector = normalized_vector(structured.get("vector") or self._emotion_spec(emotion_type).get("vector"))
        else:
            emotion_type, strength, reason_codes = FAST_EVENT_MAP.get(
                event.event_kind,
                (None, 0.0, ("unrecognized_fast_event_kind",)),
            )
            vector = normalized_vector(self._emotion_spec(emotion_type).get("vector"))
        if emotion_type not in self.enabled_emotions:
            emotion_type = None
            strength = 0.0
            reason_codes = (*reason_codes, "emotion_not_production_enabled")
            vector = normalized_vector()
        active_capacity = self._active_capacity(snapshot)
        factors = self._factor_defaults(event, active_capacity=active_capacity)
        return AffectAppraisal(
            appraisal_id=new_id("aapp"),
            event_id=event.event_id,
            sora_id=event.sora_id,
            lane="fast",
            revision=1,
            emotion_type=emotion_type,
            target_kind=event.target_kind,
            target_ref=event.target_ref,
            vector=vector,
            strength=strength,
            confidence=max(0.80, event.source_confidence) if emotion_type else 1.0,
            persona_sensitivity=factors["persona_sensitivity"],
            relationship_modifier=factors["relationship_modifier"],
            goal_importance=factors["goal_importance"],
            novelty_modifier=factors["novelty_modifier"],
            capacity_modifier=factors["capacity_modifier"],
            source_confidence=event.source_confidence,
            evidence_refs=event.evidence_refs or (f"event:{event.event_id}",),
            reason_codes=reason_codes,
            occurred_at=event.occurred_at,
            effective_at=event.occurred_at,
            provider_trace={"provider": "deterministic_fast_appraiser", "profile_version": self.profile_version},
        )

    def semantic_appraisal(
        self,
        event: AffectStimulusEvent,
        payload: Mapping[str, Any],
        *,
        revision: int,
        effective_at: str,
        provider_trace: Mapping[str, Any],
        supersedes_appraisal_id: str,
        snapshot: Mapping[str, Any],
    ) -> AffectAppraisal:
        emotion_type_value = payload.get("emotion_type")
        emotion_type = str(emotion_type_value) if emotion_type_value else None
        if emotion_type not in self.enabled_emotions:
            emotion_type = None
        allowed_refs = set(event.evidence_refs or (f"event:{event.event_id}",))
        evidence_refs = tuple(
            item for item in (str(ref) for ref in payload.get("evidence_refs") or ()) if item in allowed_refs
        ) or tuple(sorted(allowed_refs))
        vector = normalized_vector(
            payload.get("vector") or self._emotion_spec(emotion_type).get("vector")
        )
        factors = self._factor_defaults(event, active_capacity=self._active_capacity(snapshot))
        for key in (
            "persona_sensitivity",
            "relationship_modifier",
            "goal_importance",
            "novelty_modifier",
            "capacity_modifier",
        ):
            if key in payload:
                factors[key] = bounded(float(payload[key]))
        appraisal = AffectAppraisal(
            appraisal_id=new_id("aapp"),
            event_id=event.event_id,
            sora_id=event.sora_id,
            lane="semantic",
            revision=revision,
            emotion_type=emotion_type,
            target_kind=event.target_kind,
            target_ref=event.target_ref,
            vector=vector,
            strength=bounded(float(payload.get("strength", 0.0))) if emotion_type else 0.0,
            confidence=bounded(float(payload.get("confidence", 0.0))) if emotion_type else 1.0,
            persona_sensitivity=factors["persona_sensitivity"],
            relationship_modifier=factors["relationship_modifier"],
            goal_importance=factors["goal_importance"],
            novelty_modifier=factors["novelty_modifier"],
            capacity_modifier=factors["capacity_modifier"],
            source_confidence=event.source_confidence,
            evidence_refs=evidence_refs,
            reason_codes=tuple(str(item) for item in payload.get("reason_codes") or ("semantic_enrichment",)),
            occurred_at=event.occurred_at,
            effective_at=effective_at,
            provider_trace=dict(provider_trace),
            supersedes_appraisal_id=supersedes_appraisal_id,
        )
        appraisal.validate()
        return appraisal

    def should_apply_semantic(self, fast: AffectAppraisal, semantic: AffectAppraisal) -> tuple[bool, str]:
        if semantic.confidence < float(self.profile.get("semantic_min_confidence", 0.72)):
            return False, "semantic_confidence_below_threshold"
        if fast.emotion_type == semantic.emotion_type:
            difference = abs(fast.strength - semantic.strength)
        else:
            difference = max(fast.strength, semantic.strength, 0.25)
        if difference < float(self.profile.get("semantic_hysteresis", 0.12)):
            return False, "semantic_change_inside_hysteresis"
        return True, "semantic_revision_applies_forward"

    def contribution(
        self,
        appraisal: AffectAppraisal,
        *,
        correction_against: AffectAppraisal | None = None,
    ) -> JsonDict:
        appraisal.validate()
        current = self._absolute_contribution(appraisal)
        if correction_against is None:
            return current
        previous = self._absolute_contribution(correction_against)
        return {
            **current,
            "contribution_id": new_id("acontrib"),
            "lane": "semantic_correction",
            "core_delta": _vector_difference(current["core_delta"], previous["core_delta"]),
            "mood_delta": _vector_difference(current["mood_delta"], previous["mood_delta"]),
            "capacity_cost": _mapping_difference(current["capacity_cost"], previous["capacity_cost"]),
            "need_delta": _mapping_difference(current["need_delta"], previous["need_delta"]),
            "correction_of_appraisal_id": correction_against.appraisal_id,
        }

    def apply(self, snapshot: Mapping[str, Any], contribution: Mapping[str, Any], *, effective_at: str) -> JsonDict:
        advanced = self.advance(snapshot, to_time=effective_at)
        baseline = normalized_vector(self.profile.get("core_baseline"))
        saturation_scale = max(0.05, float(self.profile.get("saturation_scale", 0.65)))
        core_delta = _normalized_delta_vector(contribution.get("core_delta"))
        mood_delta = _normalized_delta_vector(contribution.get("mood_delta"))
        core: JsonDict = {}
        for key in ("valence", "arousal", "dominance"):
            current = float(advanced["core_affect"][key])
            mood_offset = 0.05 * (float(advanced["mood"][key]) - float(baseline[key]))
            pre = current + float(core_delta[key]) + mood_offset
            core[key] = normalized(
                bounded(
                    float(baseline[key])
                    + math.tanh((pre - float(baseline[key])) / saturation_scale) * saturation_scale,
                    0.0 if key == "arousal" else -1.0,
                    1.0,
                )
            )
        mood = {
            key: normalized(
                bounded(
                    float(advanced["mood"][key]) + float(mood_delta[key]),
                    0.0 if key == "arousal" else -1.0,
                    1.0,
                )
            )
            for key in ("valence", "arousal", "dominance")
        }
        mood["band"] = mood_band(mood)
        capacities = dict(advanced.get("capacities") or {})
        for key, cost in dict(contribution.get("capacity_cost") or {}).items():
            capacities[key] = normalized(bounded(float(capacities.get(key, 0.0)) - float(cost)))
        needs = dict(advanced.get("needs") or {})
        for key, delta in dict(contribution.get("need_delta") or {}).items():
            needs[key] = normalized(bounded(float(needs.get(key, 0.0)) + float(delta)))
        needs = self._couple_capacity_needs(capacities, needs)
        active_emotions = self._merge_emotion(
            list(advanced.get("active_emotions") or []),
            dict(contribution.get("emotion") or {}),
            effective_at,
        )
        target_contexts = self._target_contexts(active_emotions)
        incoming_emotion = dict(contribution.get("emotion") or {})
        primary_relationship_ref = (
            incoming_emotion.get("relationship_ref")
            or advanced.get("primary_relationship_ref")
        )
        degraded = list(advanced.get("degraded_reasons") or [])
        result = {
            **advanced,
            "snapshot_id": new_id("asnap"),
            "state_revision": int(advanced.get("state_revision", 0)) + 1,
            "observed_at": effective_at,
            "core_affect": core,
            "mood": mood,
            "active_emotions": active_emotions,
            "capacities": capacities,
            "needs": needs,
            "regulation": self._regulation(capacities, active_emotions),
            "active_subject_refs": sorted(
                {str(item["target_ref"]) for item in target_contexts if item.get("target_ref")}
            ),
            "target_contexts": target_contexts,
            "primary_relationship_ref": primary_relationship_ref,
            "dynamics_profile_version": self.profile_version,
            "taxonomy_version": str(self.profile.get("taxonomy_version") or advanced.get("taxonomy_version")),
            "source_event_watermark": int(advanced.get("source_event_watermark", 0)) + 1,
            "degraded_reasons": degraded,
        }
        return result

    def advance(self, snapshot: Mapping[str, Any], *, to_time: str) -> JsonDict:
        result = copy.deepcopy(dict(snapshot))
        start = _parse_time(str(result.get("observed_at") or to_time))
        end = _parse_time(to_time)
        delta_seconds = (end - start).total_seconds()
        degraded = list(result.get("degraded_reasons") or [])
        if delta_seconds < 0:
            degraded.append("AFF-TIME-401:clock_rollback_clamped")
            delta_seconds = 0.0
        delta_seconds = min(delta_seconds, 10 * 365 * 86400.0)
        baseline = normalized_vector(self.profile.get("core_baseline"))
        core_tau = dict(self.profile.get("core_tau_seconds") or {})
        mood_tau = dict(self.profile.get("mood_tau_seconds") or {})
        core = _decay_vector(result.get("core_affect") or {}, baseline, core_tau, delta_seconds)
        mood_values = _decay_vector(result.get("mood") or {}, baseline, mood_tau, delta_seconds)
        mood = {**mood_values, "band": mood_band(mood_values)}
        capacities = _recover_mapping(
            result.get("capacities") or {},
            self.profile.get("capacity_baselines") or {},
            self.profile.get("capacity_tau_seconds") or {},
            delta_seconds,
        )
        needs = _recover_mapping(
            result.get("needs") or {},
            self.profile.get("need_baselines") or {},
            self.profile.get("need_tau_seconds") or {},
            delta_seconds,
        )
        emotions: list[JsonDict] = []
        for emotion in result.get("active_emotions") or []:
            spec = self._emotion_spec(emotion.get("emotion_type"))
            tau = max(1.0, float(spec.get("tau_seconds", 1800.0)))
            persistence = bounded(float(emotion.get("persistence", spec.get("persistence", 0.3))))
            floor = 0.08 * persistence if emotion.get("resolution_state") == "lingering" else 0.0
            intensity = floor + (float(emotion.get("intensity", 0.0)) - floor) * math.exp(-delta_seconds / tau)
            if intensity >= 0.025:
                emotions.append(
                    {
                        **emotion,
                        "intensity": normalized(bounded(intensity)),
                        "resolution_state": _episode_lifecycle_state(
                            intensity,
                            float(emotion.get("peak_intensity", intensity)),
                            elapsed_seconds=delta_seconds,
                        ),
                        "updated_at": to_time,
                    }
                )
        result.update(
            {
                "observed_at": to_time,
                "core_affect": core,
                "mood": mood,
                "capacities": capacities,
                "needs": needs,
                "active_emotions": emotions,
                "target_contexts": self._target_contexts(emotions),
                "active_subject_refs": sorted(
                    {str(item.get("target_ref")) for item in emotions if item.get("target_ref")}
                ),
                "regulation": self._regulation(capacities, emotions),
                "degraded_reasons": sorted(set(degraded)),
            }
        )
        return result

    def frame(
        self,
        snapshot: Mapping[str, Any],
        *,
        interruptibility_frame: Mapping[str, Any] | None,
        correlation_id: str | None,
        user_id: str | None,
        max_tokens: int | None = None,
    ) -> JsonDict:
        interrupt = dict(interruptibility_frame or {})
        obligation = str(interrupt.get("response_obligation") or "required")
        response_shape = str(interrupt.get("response_shape") or "normal")
        surface = dict(interrupt.get("state_surface_permission") or {})
        permission = str(surface.get("affect") or "implicit")
        budget = max_tokens or {"ack": 64, "brief": 64, "repair_brief": 64, "deep": 160}.get(response_shape, 128)
        budget = max(32, min(160, int(budget)))
        dominant_internal = max(
            snapshot.get("active_emotions") or [],
            key=lambda item: float(item.get("intensity", 0.0)),
            default=None,
        )
        dominant = _public_emotion(dominant_internal)
        capacity_bands = {key: capacity_band(float(value)) for key, value in dict(snapshot.get("capacities") or {}).items()}
        floor_keys = sorted(key for key, band in capacity_bands.items() if band == "floor")
        expression_bias = self._expression_bias(snapshot, dominant_internal)
        reasons: list[str] = []
        if obligation == "forbidden":
            reasons.append("response_forbidden")
        if permission == "silent":
            reasons.append("affect_surface_silent")
        prompt_view = self._prompt_view(
            snapshot,
            dominant=dominant_internal,
            floor_keys=floor_keys,
            expression_bias=expression_bias,
            permission=permission,
            obligation=obligation,
            budget=budget,
        )
        return {
            "schema_version": AFFECT_FRAME_CONTRACT,
            "frame_id": new_id("aframe"),
            "sora_id": snapshot["sora_id"],
            "user_id": user_id,
            "snapshot_id": snapshot["snapshot_id"],
            "state_revision": snapshot["state_revision"],
            "observed_at": snapshot["observed_at"],
            "core_band": mood_band(snapshot["core_affect"]),
            "mood_band": snapshot["mood"]["band"],
            "dominant_emotion": copy.deepcopy(dominant),
            "capacity_bands": capacity_bands,
            "need_bands": {
                key: capacity_band(float(value)) for key, value in dict(snapshot.get("needs") or {}).items()
            },
            "regulation": copy.deepcopy(snapshot.get("regulation") or {}),
            "target_contexts": _public_target_contexts(snapshot.get("target_contexts") or []),
            "expression_bias": {} if permission == "silent" else expression_bias,
            "state_surface_permission": permission,
            "use_policy": {
                "surface_permission": permission,
                "include_state_expression": permission in {"implicit", "explicit"} and obligation != "forbidden",
                "include_self_reference": permission == "explicit" and obligation != "forbidden",
                "reason_codes": reasons or ["normal_affect_use"],
            },
            "prompt_budget_tokens": budget,
            "prompt_view": prompt_view,
            "correlation_id": correlation_id,
            "degraded_reasons": list(snapshot.get("degraded_reasons") or []),
            "provenance": {
                "snapshot_checksum": snapshot_checksum(snapshot),
                "dynamics_profile_version": snapshot.get("dynamics_profile_version"),
                "taxonomy_version": snapshot.get("taxonomy_version"),
            },
        }

    def signal(self, event_bus: Mapping[str, Any], frame: Mapping[str, Any]) -> JsonDict | None:
        dominant = dict(frame.get("dominant_emotion") or {})
        regulation = dict(frame.get("regulation") or {})
        if not dominant and not regulation.get("active_limits"):
            return None
        intensity = _band_intensity(str(dominant.get("intensity_band") or "none"))
        return {
            "id": f"sig_affect_state_{event_bus.get('turn_id')}",
            "source": "affect",
            "type": "internal_state",
            "priority": round(min(0.78, 0.34 + intensity * 0.45), 4),
            "confidence": 1.0,
            "visibility": "background" if intensity < 0.58 else "candidate",
            "interrupt_level": "none",
            "emotional_weight": round(intensity, 4),
            "relation_weight": 0.0,
            "expires_in_turns": 1,
            "reason": "Use internal state only as a bounded expression and regulation signal.",
            "contract": AFFECT_SIGNAL_CONTRACT,
            "emotion_type": dominant.get("emotion_type"),
            "target_ref": dominant.get("target_ref"),
            "tone_hint": (frame.get("expression_bias") or {}).get("tone"),
            "regulation_limits": regulation.get("active_limits") or [],
            "state_revision": frame.get("state_revision"),
        }

    def _absolute_contribution(self, appraisal: AffectAppraisal) -> JsonDict:
        if appraisal.emotion_type is None or appraisal.strength <= 0.0:
            intensity = 0.0
        else:
            evidence = math.sqrt(max(0.0, appraisal.source_confidence * appraisal.confidence))
            factors = {
                "persona_sensitivity": appraisal.persona_sensitivity,
                "relationship_modifier": appraisal.relationship_modifier,
                "goal_importance": appraisal.goal_importance,
                "novelty_modifier": appraisal.novelty_modifier,
                "capacity_modifier": appraisal.capacity_modifier,
                "evidence": evidence,
            }
            context = _weighted_geometric_context(
                factors,
                FACTOR_WEIGHTS,
                factor_floor=bounded(float(self.profile.get("factor_floor", 0.0))),
                zero_policy=str(self.profile.get("factor_zero_policy") or "veto"),
            )
            raw = appraisal.strength * context
            scale = max(0.05, float(self.profile.get("intensity_scale", 0.70)))
            soft = math.tanh(raw / scale) * scale
            type_cap = float(self._emotion_spec(appraisal.emotion_type).get("type_cap", 0.60))
            intensity = bounded(min(soft, type_cap))
        vector = normalized_vector(appraisal.vector)
        caps = dict(self.profile.get("per_event_vector_cap") or {})
        core_delta = {
            key: normalized(
                bounded(
                    float(vector[key]) * intensity,
                    -float(caps.get(key, 0.2)),
                    float(caps.get(key, 0.2)),
                )
            )
            for key in ("valence", "arousal", "dominance")
        }
        persistence = bounded(float(self._emotion_spec(appraisal.emotion_type).get("persistence", 0.0)))
        resolution_state = "rising"
        residuals = dict(self.profile.get("episode_resolution_residuals") or {})
        resolution_residual = bounded(float(residuals.get(resolution_state, 0.75)))
        evidence_quality = math.sqrt(max(0.0, appraisal.source_confidence * appraisal.confidence))
        mood_cap = bounded(float(self.profile.get("single_episode_mood_cap", 0.12)))
        mood_delta = {
            key: normalized(
                bounded(
                    float(vector[key]) * intensity * persistence * resolution_residual * evidence_quality,
                    -mood_cap,
                    mood_cap,
                )
            )
            for key in ("valence", "arousal", "dominance")
        }
        capacity_cost = self._capacity_cost(appraisal.emotion_type, intensity)
        need_delta = self._need_delta(appraisal.emotion_type, intensity)
        emotion = (
            {
                "episode_key": f"{appraisal.event_id}:{appraisal.emotion_type}:{appraisal.target_ref or 'none'}",
                "emotion_type": appraisal.emotion_type,
                "target_kind": appraisal.target_kind,
                "target_ref": appraisal.target_ref,
                "intensity": normalized(intensity),
                "peak_intensity": normalized(intensity),
                "persistence": normalized(persistence),
                "resolution_state": resolution_state,
                "evidence_refs": list(appraisal.evidence_refs),
                "appraisal_id": appraisal.appraisal_id,
            }
            if appraisal.emotion_type and intensity >= 0.025
            else {}
        )
        return {
            "contribution_id": new_id("acontrib"),
            "sora_id": appraisal.sora_id,
            "event_id": appraisal.event_id,
            "appraisal_id": appraisal.appraisal_id,
            "lane": appraisal.lane,
            "effective_at": appraisal.effective_at,
            "intensity": normalized(intensity),
            "core_delta": core_delta,
            "mood_delta": mood_delta,
            "capacity_cost": capacity_cost,
            "need_delta": need_delta,
            "emotion": emotion,
        }

    def _factor_defaults(self, event: AffectStimulusEvent, *, active_capacity: float) -> JsonDict:
        payload = dict(event.payload)
        return {
            "persona_sensitivity": bounded(float(payload.get("persona_sensitivity", 0.65))),
            "relationship_modifier": bounded(float(payload.get("relationship_modifier", 0.60))),
            "goal_importance": bounded(float(payload.get("goal_importance", 0.55))),
            "novelty_modifier": bounded(float(payload.get("novelty_modifier", 0.75))),
            "capacity_modifier": bounded(float(payload.get("capacity_modifier", active_capacity))),
        }

    @staticmethod
    def _active_capacity(snapshot: Mapping[str, Any]) -> float:
        values = [float(item) for item in dict(snapshot.get("capacities") or {}).values()]
        return sum(values) / len(values) if values else 0.75

    def _emotion_spec(self, emotion_type: Any) -> JsonDict:
        return dict(self.emotion_specs.get(str(emotion_type), {})) if emotion_type else {}

    @staticmethod
    def _capacity_cost(emotion_type: str | None, intensity: float) -> JsonDict:
        if not emotion_type or intensity <= 0.0:
            return {key: 0.0 for key in ("cognitive", "social", "expressive", "regulation", "attention")}
        high_regulation = emotion_type in {"anger", "fear", "guilt", "frustration", "anxiety"}
        relational = emotion_type in {"affection", "sadness", "longing", "guilt", "relief"}
        return {
            "cognitive": normalized(min(0.05, 0.008 + intensity * 0.025)),
            "social": normalized(min(0.05, 0.006 + intensity * (0.035 if relational else 0.018))),
            "expressive": normalized(min(0.04, 0.006 + intensity * 0.025)),
            "regulation": normalized(min(0.06, 0.004 + intensity * (0.055 if high_regulation else 0.015))),
            "attention": normalized(min(0.05, 0.006 + intensity * 0.035)),
        }

    @staticmethod
    def _need_delta(emotion_type: str | None, intensity: float) -> JsonDict:
        result = {"connection": 0.0, "rest": 0.0, "clarity": 0.0, "repair": 0.0}
        if emotion_type in {"affection", "longing", "sadness"}:
            result["connection"] = normalized(min(0.08, intensity * 0.08))
        if emotion_type in {"guilt", "frustration", "anger"}:
            result["repair"] = normalized(min(0.10, intensity * 0.10))
        if emotion_type in {"anxiety", "fear", "frustration"}:
            result["clarity"] = normalized(min(0.08, intensity * 0.08))
            result["rest"] = normalized(min(0.06, intensity * 0.06))
        return result

    def _couple_capacity_needs(self, capacities: Mapping[str, Any], needs: Mapping[str, Any]) -> JsonDict:
        result = {key: normalized(bounded(float(value))) for key, value in needs.items()}
        floor = bounded(float(self.profile.get("capacity_floor", 0.20)))
        max_pressure = bounded(float(self.profile.get("capacity_need_max_pressure", 0.15)))
        if floor <= 0.0 or max_pressure <= 0.0:
            return result
        coupling = dict(self.profile.get("capacity_need_coupling") or {})
        for capacity_key, raw_targets in coupling.items():
            capacity = bounded(float(capacities.get(capacity_key, 1.0)))
            if capacity >= floor:
                continue
            pressure = max_pressure * (floor - capacity) / floor
            for need_key, weight in dict(raw_targets or {}).items():
                result[str(need_key)] = normalized(
                    bounded(float(result.get(str(need_key), 0.0)) + pressure * bounded(float(weight)))
                )
        return result

    def _merge_emotion(self, active: list[JsonDict], incoming: JsonDict, effective_at: str) -> list[JsonDict]:
        if not incoming:
            return active
        merged: list[JsonDict] = []
        found = False
        for item in active:
            if item.get("episode_key") == incoming.get("episode_key"):
                intensity = bounded(float(item.get("intensity", 0.0)) + float(incoming.get("intensity", 0.0)))
                peak = max(float(item.get("peak_intensity", 0.0)), intensity)
                merged.append(
                    {
                        **item,
                        **incoming,
                        "intensity": normalized(intensity),
                        "peak_intensity": normalized(peak),
                        "resolution_state": _episode_lifecycle_state(intensity, peak, elapsed_seconds=0.0),
                        "updated_at": effective_at,
                    }
                )
                found = True
            else:
                merged.append(item)
        if not found:
            merged.append({**incoming, "started_at": effective_at, "updated_at": effective_at})
        return sorted(merged, key=lambda item: float(item.get("intensity", 0.0)), reverse=True)[:8]

    @staticmethod
    def _target_contexts(emotions: list[JsonDict]) -> list[JsonDict]:
        grouped: dict[tuple[str, str], list[JsonDict]] = {}
        for item in emotions:
            target_kind = str(item.get("target_kind") or "unknown")
            target_ref = str(item.get("target_ref") or "")
            key = (target_kind, target_ref)
            grouped.setdefault(key, []).append(item)
        contexts: list[JsonDict] = []
        for (target_kind, target_ref), items in grouped.items():
            ranked = sorted(items, key=lambda item: float(item.get("intensity", 0.0)), reverse=True)[:3]
            dominant = ranked[0]
            contexts.append(
                {
                    "target_kind": target_kind,
                    "target_ref": target_ref or None,
                    "emotion_type": dominant.get("emotion_type"),
                    "intensity": dominant.get("intensity"),
                    "resolution_state": dominant.get("resolution_state"),
                    "evidence_refs": list(dominant.get("evidence_refs") or []),
                    "emotions": [
                        {
                            "emotion_type": item.get("emotion_type"),
                            "intensity": item.get("intensity"),
                            "resolution_state": item.get("resolution_state"),
                            "evidence_refs": list(item.get("evidence_refs") or []),
                        }
                        for item in ranked
                    ],
                }
            )
        return sorted(contexts, key=lambda item: float(item.get("intensity", 0.0)), reverse=True)[:6]

    @staticmethod
    def _regulation(capacities: Mapping[str, Any], emotions: list[JsonDict]) -> JsonDict:
        floors = sorted(key for key, value in capacities.items() if float(value) < 0.20)
        dominant = max((float(item.get("intensity", 0.0)) for item in emotions), default=0.0)
        limits: list[str] = []
        if floors:
            limits.extend(["shorten_expression", "reduce_initiative", "reduce_parallel_goals"])
        if dominant >= 0.72:
            limits.extend(["avoid_escalation", "prefer_reversible_expression"])
        return {
            "mode": "protective" if limits else "balanced",
            "active_limits": sorted(set(limits)),
            "capacity_floor_keys": floors,
        }

    @staticmethod
    def _expression_bias(snapshot: Mapping[str, Any], dominant: Mapping[str, Any] | None) -> JsonDict:
        emotion = str((dominant or {}).get("emotion_type") or "")
        tone = {
            "joy": "bright_warm",
            "affection": "gentle_warm",
            "curiosity": "attentive_curious",
            "contentment": "calm_warm",
            "relief": "soft_relaxed",
            "sadness": "quiet_gentle",
            "guilt": "brief_accountable",
            "frustration": "measured_direct",
            "anger": "contained_direct",
            "fear": "careful_grounded",
            "anxiety": "careful_grounded",
            "longing": "soft_restrained",
        }.get(emotion, "warm_natural")
        return {
            "tone": tone,
            "density": "brief" if (snapshot.get("regulation") or {}).get("active_limits") else "normal",
            "self_disclosure": "bounded",
            "must_not_pressure_user": True,
        }

    @staticmethod
    def _prompt_view(
        snapshot: Mapping[str, Any],
        *,
        dominant: Mapping[str, Any] | None,
        floor_keys: list[str],
        expression_bias: Mapping[str, Any],
        permission: str,
        obligation: str,
        budget: int,
    ) -> JsonDict:
        if obligation == "forbidden":
            return {
                "response_forbidden": True,
                "regulation_limits": list((snapshot.get("regulation") or {}).get("active_limits") or []),
                "capacity_floor_keys": floor_keys,
                "budget_tokens": budget,
            }
        result: JsonDict = {
            "regulation_limits": list((snapshot.get("regulation") or {}).get("active_limits") or []),
            "capacity_floor_keys": floor_keys,
            "budget_tokens": budget,
        }
        if permission == "silent":
            result["state_expression"] = "silent"
            return result
        result.update(
            {
                "mood_band": (snapshot.get("mood") or {}).get("band"),
                "dominant_emotion": (dominant or {}).get("emotion_type"),
                "target_ref": (dominant or {}).get("target_ref"),
                "expression_bias": dict(expression_bias),
                "state_expression": "explicit_allowed" if permission == "explicit" else "implicit_only",
            }
        )
        return result


def _weighted_geometric_context(
    factors: Mapping[str, float],
    weights: Mapping[str, float],
    *,
    factor_floor: float,
    zero_policy: str,
) -> float:
    values = {key: bounded(float(factors[key])) for key in weights}
    if zero_policy == "veto" and any(value <= 0.0 for value in values.values()):
        return 0.0
    floor = max(0.0, min(1.0, factor_floor))
    if floor <= 0.0 and any(value <= 0.0 for value in values.values()):
        return 0.0
    return math.exp(
        sum(float(weights[key]) * math.log(max(floor, values[key])) for key in weights)
    )


def _episode_lifecycle_state(intensity: float, peak: float, *, elapsed_seconds: float) -> str:
    if intensity < 0.025:
        return "resolved"
    peak_value = max(0.025, float(peak))
    ratio = bounded(float(intensity) / peak_value)
    if elapsed_seconds <= 0.0 and ratio < 0.98:
        return "rising"
    if ratio >= 0.85:
        return "peak"
    if ratio >= 0.35:
        return "decaying"
    return "lingering"


def snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    material = {
        key: snapshot.get(key)
        for key in (
            "sora_id",
            "state_revision",
            "observed_at",
            "core_affect",
            "mood",
            "active_emotions",
            "capacities",
            "needs",
            "regulation",
            "target_contexts",
            "dynamics_profile_version",
            "taxonomy_version",
        )
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _intensity_band(value: float) -> str:
    intensity = bounded(value)
    if intensity < 0.025:
        return "none"
    if intensity < 0.25:
        return "low"
    if intensity < 0.55:
        return "medium"
    if intensity < 0.78:
        return "high"
    return "very_high"


def _band_intensity(band: str) -> float:
    return {
        "none": 0.0,
        "low": 0.15,
        "medium": 0.40,
        "high": 0.66,
        "very_high": 0.84,
    }.get(band, 0.0)


def _public_emotion(emotion: Mapping[str, Any] | None) -> JsonDict | None:
    if not emotion:
        return None
    return {
        "emotion_type": emotion.get("emotion_type"),
        "target_kind": emotion.get("target_kind"),
        "target_ref": emotion.get("target_ref"),
        "intensity_band": _intensity_band(float(emotion.get("intensity", 0.0))),
        "resolution_state": emotion.get("resolution_state"),
    }


def _public_target_contexts(contexts: list[Mapping[str, Any]]) -> list[JsonDict]:
    public: list[JsonDict] = []
    for context in contexts[:6]:
        public.append(
            {
                "target_kind": context.get("target_kind"),
                "target_ref": context.get("target_ref"),
                "emotion_type": context.get("emotion_type"),
                "intensity_band": _intensity_band(float(context.get("intensity", 0.0))),
                "resolution_state": context.get("resolution_state"),
                "emotions": [
                    item
                    for item in (
                        _public_emotion(emotion)
                        for emotion in list(context.get("emotions") or [])[:3]
                    )
                    if item is not None
                ],
            }
        )
    return public


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decay_vector(
    current: Mapping[str, Any],
    target: Mapping[str, Any],
    tau: Mapping[str, Any],
    delta_seconds: float,
) -> JsonDict:
    result: JsonDict = {}
    for key in ("valence", "arousal", "dominance"):
        current_value = float(current.get(key, target.get(key, 0.0)))
        target_value = float(target.get(key, 0.0))
        time_constant = max(1.0, float(tau.get(key, 3600.0)))
        value = target_value + (current_value - target_value) * math.exp(-delta_seconds / time_constant)
        result[key] = normalized(bounded(value, 0.0 if key == "arousal" else -1.0, 1.0))
    return result


def _recover_mapping(
    current: Mapping[str, Any],
    target: Mapping[str, Any],
    tau: Mapping[str, Any],
    delta_seconds: float,
) -> JsonDict:
    result: JsonDict = {}
    for key in set(current) | set(target):
        current_value = float(current.get(key, target.get(key, 0.0)))
        target_value = float(target.get(key, 0.0))
        time_constant = max(1.0, float(tau.get(key, 3600.0)))
        value = target_value + (current_value - target_value) * math.exp(-delta_seconds / time_constant)
        result[key] = normalized(bounded(value))
    return result


def _vector_difference(left: Mapping[str, Any], right: Mapping[str, Any]) -> JsonDict:
    return {
        key: normalized(float(left.get(key, 0.0)) - float(right.get(key, 0.0)))
        for key in ("valence", "arousal", "dominance")
    }


def _normalized_delta_vector(value: Mapping[str, Any] | None) -> JsonDict:
    source = dict(value or {})
    return {
        key: normalized(bounded(float(source.get(key, 0.0)), -1.0, 1.0))
        for key in ("valence", "arousal", "dominance")
    }


def _mapping_difference(left: Mapping[str, Any], right: Mapping[str, Any]) -> JsonDict:
    return {
        key: normalized(float(left.get(key, 0.0)) - float(right.get(key, 0.0)))
        for key in set(left) | set(right)
    }
