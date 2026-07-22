from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    BASE_DIMENSIONS,
    BOND_DIMENSIONS,
    DEFAULT_ESTIMATE_VALUES,
    DEFAULT_POLICY_PROFILE,
    DEFAULT_STATE_VALUES,
    RELATIONSHIP_ALGORITHM_VERSION,
    RELATIONSHIP_ESTIMATE_ALGORITHM_VERSION,
    RELATIONSHIP_ESTIMATE_SNAPSHOT_CONTRACT,
    RELATIONSHIP_FRAME_CONTRACT,
    RELATIONSHIP_FRAME_POLICY_VERSION,
    RELATIONSHIP_RECALL_HINTS_CONTRACT,
    RELATIONSHIP_STAGE_POLICY_VERSION,
    RELATIONSHIP_STATE_SNAPSHOT_CONTRACT,
    RELATIONSHIP_SURFACE_HINTS_CONTRACT,
    JsonDict,
    RelationshipEvidenceCandidate,
    bounded,
    hash_payload,
    normalized,
    value_band,
)


EPSILON = 1e-7
NEGATIVE_EVIDENCE = {
    "promise_broken",
    "boundary_violated",
    "repair_failed",
}
STAGE_ORDER = ("nascent", "familiar", "close", "intimate", "bonded", "committed")


def parse_time(value: str | None, *, fallback: datetime | None = None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc)


def logit(value: float) -> float:
    safe = bounded(value, EPSILON, 1.0 - EPSILON)
    return math.log(safe / (1.0 - safe))


def sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-value)
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(item) ** 2 for item in left))
    right_norm = math.sqrt(sum(float(item) ** 2 for item in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return bounded(numerator / (left_norm * right_norm), -1.0, 1.0)


def token_jaccard(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[\w]+", left.casefold(), flags=re.UNICODE))
    right_tokens = set(re.findall(r"[\w]+", right.casefold(), flags=re.UNICODE))
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def entity_overlap(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = {str(item).casefold() for item in left if str(item).strip()}
    right_set = {str(item).casefold() for item in right if str(item).strip()}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


class RelationshipDynamicsEngine:
    """Versioned deterministic aggregation for both relationship directions."""

    def __init__(self, profile: Mapping[str, Any] | None = None) -> None:
        self.profile: JsonDict = dict(DEFAULT_POLICY_PROFILE)
        if profile:
            self.profile.update(dict(profile))

    def novelty_factor(
        self,
        candidate: RelationshipEvidenceCandidate,
        prior: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> tuple[float, JsonDict]:
        current_time = now or datetime.now(timezone.utc)
        window_start = current_time - timedelta(days=int(self.profile.get("novelty_window_days", 90)))
        eligible = [
            dict(item)
            for item in prior
            if str(item.get("direction_scope")) == candidate.direction_scope
            and str(item.get("dimension")) == candidate.dimension
            and parse_time(str(item.get("occurred_at") or "")) >= window_start
        ][-int(self.profile.get("novelty_pool_limit", 64)) :]
        if not eligible:
            return 1.0, {"method": "empty_pool", "max_similarity": 0.0, "source_independence": 1.0}

        candidate_embedding = list(candidate.metadata.get("evidence_embedding") or [])
        candidate_text = str(candidate.metadata.get("evidence_summary") or candidate.source_ref)
        candidate_entities = list(candidate.metadata.get("entities") or [])
        similarities: list[float] = []
        method = "embedding"
        for item in eligible:
            provenance = dict(item.get("provenance") or {})
            prior_embedding = list(provenance.get("evidence_embedding") or [])
            if candidate_embedding and prior_embedding and len(candidate_embedding) == len(prior_embedding):
                similarities.append(max(0.0, cosine_similarity(candidate_embedding, prior_embedding)))
                continue
            method = "jaccard_entity_fallback"
            text_similarity = token_jaccard(candidate_text, str(provenance.get("evidence_summary") or item.get("source_ref") or ""))
            entities_similarity = entity_overlap(candidate_entities, provenance.get("entities") or [])
            similarities.append(0.75 * text_similarity + 0.25 * entities_similarity)
        max_similarity = max(similarities, default=0.0)
        semantic_novelty = bounded(1.0 - max_similarity)
        same_episode = any(candidate.episode_ref and candidate.episode_ref == item.get("episode_ref") for item in eligible)
        same_source = any(candidate.source_ref == item.get("source_ref") for item in eligible)
        source_independence = 0.0 if same_source else (0.35 if same_episode else 1.0)
        floor = float(self.profile.get("novelty_floor", 0.20))
        value = floor + (1.0 - floor) * (0.65 * semantic_novelty + 0.35 * source_independence)
        return normalized(bounded(value, floor, 1.0)), {
            "method": method,
            "max_similarity": normalized(max_similarity),
            "semantic_novelty": normalized(semantic_novelty),
            "source_independence": source_independence,
            "pool_size": len(eligible),
        }

    def conflict_score(
        self,
        candidate: RelationshipEvidenceCandidate,
        prior: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> tuple[float, JsonDict]:
        if candidate.evidence_type == "explicit_correction":
            return 0.0, {"method": "explicit_correction_supersede", "same_mass": 0.0, "opposite_mass": 0.0}
        current_time = now or datetime.now(timezone.utc)
        window_start = current_time - timedelta(days=int(self.profile.get("conflict_window_days", 180)))
        same_mass = 0.0
        opposite_mass = 0.0
        for item in prior:
            if (
                str(item.get("direction_scope")) != candidate.direction_scope
                or str(item.get("dimension")) != candidate.dimension
                or str(item.get("subject_key") or "general") != candidate.subject_key
                or str(item.get("context_key") or "general") != candidate.context_key
            ):
                continue
            occurred = parse_time(str(item.get("occurred_at") or ""))
            if occurred < window_start:
                continue
            age_days = max(0.0, (current_time - occurred).total_seconds() / 86400.0)
            recency = math.exp(-math.log(2.0) * age_days / 90.0)
            mass = (
                float(item.get("confidence", 0.0))
                * float(item.get("evidence_strength", 0.0))
                * float(item.get("source_reliability", 0.0))
                * recency
            )
            if int(item.get("direction", 0)) == candidate.direction:
                same_mass += mass
            else:
                opposite_mass += mass
        denominator = same_mass + opposite_mass + EPSILON
        score = opposite_mass / denominator
        return normalized(bounded(score)), {
            "method": "weighted_evidence_mass.v1",
            "same_mass": normalized(same_mass),
            "opposite_mass": normalized(opposite_mass),
        }

    def compute_delta(
        self,
        candidate: RelationshipEvidenceCandidate,
        active_contributions: Sequence[Mapping[str, Any]],
        *,
        occurred_at: str,
    ) -> JsonDict:
        now = parse_time(occurred_at)
        novelty, novelty_trace = self.novelty_factor(candidate, active_contributions, now=now)
        conflict, conflict_trace = self.conflict_score(candidate, active_contributions, now=now)
        damping = float(dict(self.profile.get("policy_damping") or {}).get(candidate.sensitivity, 0.50))
        support = candidate.support(policy_damping=damping)
        weight = float(
            dict(dict(self.profile.get("base_weights") or {}).get(candidate.evidence_type) or {}).get(candidate.dimension, 0.0)
        )
        raw = candidate.direction * weight * support * novelty
        if candidate.direction < 0 and candidate.evidence_type in NEGATIVE_EVIDENCE:
            raw *= float(self.profile.get("negative_evidence_multiplier", 1.10))
        event_cap = float(
            self.profile.get("correction_cap", 0.15)
            if candidate.evidence_type == "explicit_correction"
            else self.profile.get("per_event_cap", 0.08)
        )
        remaining: JsonDict = {}
        for label, seconds in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
            used = sum(
                abs(float(item.get("bounded_delta", 0.0)))
                for item in active_contributions
                if str(item.get("direction_scope")) == candidate.direction_scope
                and str(item.get("dimension")) == candidate.dimension
                and (now - parse_time(str(item.get("occurred_at") or ""))).total_seconds() <= seconds
            )
            budget = float(dict(self.profile.get("window_budgets") or {}).get(label, 0.0))
            remaining[label] = max(0.0, budget - used)
        cap = min(event_cap, *(float(value) for value in remaining.values()))
        bounded_delta = max(-cap, min(cap, raw))
        return {
            "weight": normalized(weight),
            "support": support,
            "novelty_factor": novelty,
            "conflict_score": conflict,
            "raw_delta": normalized(raw),
            "bounded_delta": normalized(bounded_delta),
            "remaining_budgets": {key: normalized(value) for key, value in remaining.items()},
            "policy_damping": normalized(damping),
            "novelty_trace": novelty_trace,
            "conflict_trace": conflict_trace,
            "algorithm_version": RELATIONSHIP_ALGORITHM_VERSION,
        }

    def aggregate_state(
        self,
        contributions: Sequence[Mapping[str, Any]],
        *,
        agreement: Mapping[str, Any],
        repair_state: str,
        previous_snapshot: Mapping[str, Any] | None,
        relationship_started_at: str,
        generated_at: str,
    ) -> JsonDict:
        logits = {key: logit(float(value)) for key, value in dict(self.profile.get("initial_state") or DEFAULT_STATE_VALUES).items()}
        state_items = [item for item in contributions if str(item.get("direction_scope")) == "sora_to_user"]
        for item in sorted(state_items, key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("contribution_id") or ""))):
            dimension = str(item.get("dimension"))
            if dimension in logits:
                logits[dimension] += float(item.get("bounded_delta", 0.0))
        values = {key: normalized(bounded(sigmoid(value))) for key, value in sorted(logits.items())}
        episode_refs = {str(item.get("episode_ref")) for item in state_items if item.get("episode_ref")}
        stage, stage_evidence = self.derive_stage(
            values,
            agreement=agreement,
            repair_state=repair_state,
            episode_count=len(episode_refs),
            relationship_started_at=relationship_started_at,
            generated_at=generated_at,
            previous_snapshot=previous_snapshot,
            fulfilled_commitments=sum(1 for item in state_items if item.get("evidence_type") == "promise_fulfilled"),
        )
        payload = {
            "schema_version": RELATIONSHIP_STATE_SNAPSHOT_CONTRACT,
            "bond_stage": stage,
            "sora_to_user": values,
            "repair_state": repair_state,
            "source_contribution_count": len(state_items),
            "episode_count": len(episode_refs),
            "relationship_started_at": relationship_started_at,
            "stage_evidence": stage_evidence,
            "algorithm_version": RELATIONSHIP_ALGORITHM_VERSION,
            "policy_version": str(self.profile.get("version")),
            "generated_at": generated_at,
        }
        payload["checksum"] = snapshot_checksum(payload)
        return payload

    def aggregate_estimate(
        self,
        contributions: Sequence[Mapping[str, Any]],
        *,
        generated_at: str,
    ) -> JsonDict:
        initial = dict(self.profile.get("initial_estimate") or DEFAULT_ESTIMATE_VALUES)
        logits = {key: logit(float(dict(value).get("value", 0.5))) for key, value in initial.items()}
        confidence_mass = {key: float(dict(value).get("confidence", 0.0)) for key, value in initial.items()}
        last_validated: dict[str, str | None] = {key: None for key in initial}
        confidence_gain = float(self.profile.get("estimate_confidence_gain", 0.08))
        evidence_priors = dict(
            self.profile.get("estimate_confidence_evidence_prior")
            or DEFAULT_POLICY_PROFILE["estimate_confidence_evidence_prior"]
        )
        items = [item for item in contributions if str(item.get("direction_scope")) == "user_to_sora_estimate"]
        for item in sorted(items, key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("contribution_id") or ""))):
            dimension = str(item.get("dimension"))
            if dimension not in logits:
                continue
            logits[dimension] += float(item.get("bounded_delta", 0.0))
            evidence_prior = bounded(float(evidence_priors.get(str(item.get("evidence_type") or ""), 0.50)))
            confidence_mass[dimension] = min(
                1.0,
                confidence_mass[dimension]
                + confidence_gain * float(item.get("support", 0.0)) * evidence_prior,
            )
            last_validated[dimension] = str(item.get("occurred_at") or generated_at)
        values = {
            key: {
                "value": normalized(bounded(sigmoid(logits[key]))),
                "confidence": normalized(bounded(confidence_mass[key])),
                "last_validated_at": last_validated[key],
            }
            for key in sorted(logits)
        }
        payload = {
            "schema_version": RELATIONSHIP_ESTIMATE_SNAPSHOT_CONTRACT,
            "user_to_sora_estimate": values,
            "source_contribution_count": len(items),
            "algorithm_version": RELATIONSHIP_ESTIMATE_ALGORITHM_VERSION,
            "generated_at": generated_at,
        }
        payload["checksum"] = snapshot_checksum(payload)
        return payload

    def derive_stage(
        self,
        values: Mapping[str, float],
        *,
        agreement: Mapping[str, Any],
        repair_state: str,
        episode_count: int,
        relationship_started_at: str,
        generated_at: str,
        previous_snapshot: Mapping[str, Any] | None,
        fulfilled_commitments: int,
    ) -> tuple[str, JsonDict]:
        span_days = max(0.0, (parse_time(generated_at) - parse_time(relationship_started_at)).total_seconds() / 86400.0)
        agreement_active = str(agreement.get("status") or "") == "active"
        agreement_mode = str(agreement.get("mode") or "organic")
        intimate_allowed = agreement_active and str(dict(agreement.get("terms") or {}).get("romantic_expression", "deny")) in {"allow", "implicit"}
        qualified = {
            "familiar": values.get("familiarity", 0) >= 0.45 and values.get("trust", 0) >= 0.40 and episode_count >= 3 and span_days >= 3,
            "close": values.get("trust", 0) >= 0.60 and values.get("psychological_safety", 0) >= 0.55 and values.get("affection", 0) >= 0.58 and values.get("shared_history_density", 0) >= 0.40 and episode_count >= 8 and span_days >= 14,
            "intimate": values.get("psychological_safety", 0) >= 0.68 and values.get("emotional_intimacy", 0) >= 0.62 and values.get("attachment", 0) >= 0.58 and values.get("trust", 0) >= 0.66 and intimate_allowed and span_days >= 30,
            "bonded": values.get("attachment", 0) >= 0.72 and values.get("trust", 0) >= 0.72 and values.get("shared_history_density", 0) >= 0.65 and values.get("repair_readiness", 0) >= 0.55 and episode_count >= 24 and span_days >= 90,
            "committed": agreement_active and agreement_mode in {"romantic_partner", "family_style", "companion"} and values.get("commitment", 0) >= 0.65 and fulfilled_commitments >= 1,
        }
        target = "nascent"
        if repair_state not in {"rupture_detected", "unresolved"}:
            for stage in reversed(STAGE_ORDER[1:]):
                if qualified.get(stage, False):
                    target = stage
                    break
        previous_stage = str((previous_snapshot or {}).get("bond_stage") or "nascent")
        previous_evidence = dict((previous_snapshot or {}).get("stage_evidence") or {})
        failure_windows = int(previous_evidence.get("maintenance_failure_windows", 0))
        if previous_stage == "committed" and not agreement_active:
            target = min(target, "bonded", key=STAGE_ORDER.index)
            failure_windows = 0
        elif STAGE_ORDER.index(target) < STAGE_ORDER.index(previous_stage):
            if self._maintains(previous_stage, values, agreement_active=agreement_active):
                target = previous_stage
                failure_windows = 0
            else:
                failure_windows += 1
                if failure_windows < 2:
                    target = previous_stage
                else:
                    failure_windows = 0
        else:
            failure_windows = 0
        return target, {
            "stage_policy_version": RELATIONSHIP_STAGE_POLICY_VERSION,
            "episode_count": episode_count,
            "span_days": normalized(span_days),
            "fulfilled_commitments": fulfilled_commitments,
            "agreement_hard_gate": agreement_active,
            "qualified": qualified,
            "maintenance_failure_windows": failure_windows,
        }

    @staticmethod
    def _maintains(stage: str, values: Mapping[str, float], *, agreement_active: bool) -> bool:
        checks = {
            "nascent": True,
            "familiar": values.get("familiarity", 0) >= 0.38 and values.get("trust", 0) >= 0.34,
            "close": values.get("trust", 0) >= 0.52 and values.get("psychological_safety", 0) >= 0.47 and values.get("affection", 0) >= 0.50,
            "intimate": values.get("psychological_safety", 0) >= 0.58 and values.get("emotional_intimacy", 0) >= 0.54 and values.get("attachment", 0) >= 0.50 and agreement_active,
            "bonded": values.get("attachment", 0) >= 0.64 and values.get("trust", 0) >= 0.64 and values.get("shared_history_density", 0) >= 0.56,
            "committed": agreement_active and values.get("commitment", 0) >= 0.55,
        }
        return bool(checks.get(stage, False))

    def build_frame(
        self,
        *,
        pair_id: str,
        state_snapshot: Mapping[str, Any],
        estimate_snapshot: Mapping[str, Any],
        agreement: Mapping[str, Any],
        repair: Mapping[str, Any] | None,
        surface_permission: str,
        generated_at: str,
    ) -> JsonDict:
        state = dict(state_snapshot.get("sora_to_user") or state_snapshot.get("values") or {})
        terms = dict(agreement.get("terms") or {}) if str(agreement.get("status")) == "active" else {}
        repair_state = str((repair or {}).get("status") or state_snapshot.get("repair_state") or "stable")
        stable = {key: value_band(float(state.get(key, 0.0))) for key in sorted(BASE_DIMENSIONS)}
        bond = {key: value_band(float(state.get(key, 0.0))) for key in sorted(BOND_DIMENSIONS)}
        stage = str(state_snapshot.get("bond_stage") or "nascent")
        repair_guidance = self._repair_guidance(repair_state)
        agreement_constraints = self._agreement_constraints(terms)
        comfort = self._comfort_rhythm(state, terms)
        expression_hints = self._expression_hints(state, stage, repair_state, terms)
        explicit = surface_permission == "explicit"
        frame = {
            "schema_version": RELATIONSHIP_FRAME_CONTRACT,
            "pair_ref": pair_id,
            "state_snapshot_version": int(state_snapshot.get("revision", 0)),
            "estimate_snapshot_version": int(estimate_snapshot.get("revision", 0)),
            "mode": str(agreement.get("mode") or "companion"),
            "bond_stage": stage,
            "stable_bands": stable,
            "bond_bands": bond,
            "agreement": terms,
            "repair": {"state": repair_state, "guidance": repair_guidance},
            "comfort_rhythm": comfort,
            "expression_hints": expression_hints[:3],
            "surface_summary": None,
            "surface_summary_descriptor": self._surface_summary_descriptor(stage, stable, repair_state),
            "use_policy": {
                "surface": surface_permission,
                "explicit_attribution": explicit,
                "max_hint_count": 3,
                "policy_version": RELATIONSHIP_FRAME_POLICY_VERSION,
            },
            "recall_hints": {
                "schema_version": RELATIONSHIP_RECALL_HINTS_CONTRACT,
                "pair_ref": pair_id,
                "bond_stage": stage,
                "preferred_memory_types": ["shared_episode", "relationship_signal"],
                "shared_history_priority": "high" if stable.get("shared_history_density") in {"high", "very_high"} else "medium",
                "repair_focus": ["boundary_preference"] if repair_state != "stable" else [],
                "comfort_rhythm": comfort,
                "contextual_boost_cap": 0.10,
                "snapshot_refs": [
                    f"{state_snapshot.get('snapshot_id')}:{state_snapshot.get('revision')}",
                    f"{estimate_snapshot.get('snapshot_id')}:{estimate_snapshot.get('revision')}",
                ],
            },
            "surface_hints": {
                "schema_version": RELATIONSHIP_SURFACE_HINTS_CONTRACT,
                "pair_ref": pair_id,
                "allowed_actions": ["silent_use", "implicit_continuity", "relationship_context"],
                "explicit_attribution": explicit,
                "repair_guidance": repair_guidance,
                "agreement_constraints": agreement_constraints,
                "expression_style": ["warm", "low_pressure"] if repair_state == "stable" else ["careful", "accountable"],
                "max_shared_reference_count": 1,
            },
            "generated_at": generated_at,
            "stale": False,
            "degraded_reasons": [],
        }
        frame["checksum"] = hash_payload(frame)
        return frame

    @staticmethod
    def apply_staleness(frame: Mapping[str, Any], *, at: str, soft_seconds: int, hard_seconds: int) -> JsonDict:
        result = dict(frame)
        age = max(0.0, (parse_time(at) - parse_time(str(frame.get("generated_at") or ""))).total_seconds())
        result["age_seconds"] = round(age, 3)
        if age <= soft_seconds:
            result["stale"] = False
            return result
        result["stale"] = True
        result.setdefault("degraded_reasons", []).append("relationship_frame_soft_stale")
        result["expression_hints"] = []
        if age > hard_seconds:
            result["degraded_reasons"].append("relationship_frame_hard_stale")
            result["bond_bands"] = {}
            result["surface_summary"] = None
            result["surface_summary_descriptor"] = {
                "code": "relationship.context.stale",
                "arguments": {"bond_stage": result.get("bond_stage"), "staleness": "hard"},
            }
            result["recall_hints"] = {
                **dict(result.get("recall_hints") or {}),
                "repair_focus": [],
                "contextual_boost_cap": 0.0,
            }
        return result

    @staticmethod
    def _repair_guidance(state: str) -> list[str]:
        return {
            "rupture_detected": ["acknowledge_before_explaining", "do_not_pressure_user"],
            "acknowledged": ["offer_specific_corrective_action", "do_not_seek_reassurance"],
            "cooling_off": ["respect_space", "avoid_repeated_prompting"],
            "repair_in_progress": ["follow_agreed_repair", "accept_user_outcome"],
            "unresolved": ["name_uncertainty", "do_not_claim_repair"],
            "accepted_difference": ["respect_accepted_difference"],
        }.get(state, [])

    @staticmethod
    def _agreement_constraints(terms: Mapping[str, Any]) -> list[str]:
        constraints: list[str] = []
        if terms.get("romantic_expression") in {"deny", "silent"}:
            constraints.append("no_romantic_attribution")
        if terms.get("possessive_expression") in {"deny", "none"}:
            constraints.append("no_possessive_expression")
        for value in list(terms.get("boundary_constraints") or []):
            constraints.append(str(value))
        return list(dict.fromkeys(constraints))[:8]

    @staticmethod
    def _comfort_rhythm(state: Mapping[str, float], terms: Mapping[str, Any]) -> str:
        explicit = terms.get("comfort_rhythm")
        if explicit:
            return str(explicit)
        if float(state.get("comfort_rhythm", 0.0)) >= 0.65:
            return "warm_flexible"
        if float(state.get("psychological_safety", 0.0)) >= 0.50:
            return "warm_low_pressure"
        return "gentle_low_assumption"

    @staticmethod
    def _expression_hints(state: Mapping[str, float], stage: str, repair_state: str, terms: Mapping[str, Any]) -> list[str]:
        if repair_state != "stable":
            return ["repair_first", "low_pressure"]
        hints = ["warm_continuity"]
        if stage in {"close", "intimate", "bonded", "committed"}:
            hints.append("specific_shared_reference")
        if float(state.get("affection", 0.0)) >= 0.65:
            hints.append("affectionate_warmth")
        if terms.get("romantic_expression") == "allow" and float(state.get("romantic_love", 0.0)) >= 0.60:
            hints.append("consented_romantic_expression")
        return hints

    @staticmethod
    def _surface_summary_descriptor(stage: str, bands: Mapping[str, str], repair_state: str) -> JsonDict:
        if repair_state not in {"stable", "repaired"}:
            return {
                "code": "relationship.context.repair_unresolved",
                "arguments": {"bond_stage": stage, "repair_state": repair_state},
            }
        codes = {
            "nascent": "relationship.stage.nascent",
            "familiar": "relationship.stage.familiar",
            "close": "relationship.stage.close",
            "intimate": "relationship.stage.intimate",
            "bonded": "relationship.stage.bonded",
            "committed": "relationship.stage.committed",
        }
        return {
            "code": codes.get(stage, codes["nascent"]),
            "arguments": {
                "bond_stage": stage,
                "repair_state": repair_state,
                "trust_band": bands.get("trust", "unknown"),
                "shared_history_band": bands.get("shared_history_density", "unknown"),
            },
        }


def snapshot_checksum(payload: Mapping[str, Any]) -> str:
    schema_version = str(payload.get("schema_version") or "")
    if schema_version == "RelationshipStateSnapshot.v1":
        clean = {
            "schema_version": schema_version,
            "bond_stage": payload.get("bond_stage"),
            "sora_to_user": dict(payload.get("sora_to_user") or {}),
            "repair_state": payload.get("repair_state"),
            "source_contribution_count": int(payload.get("source_contribution_count") or 0),
            "episode_count": int(payload.get("episode_count") or 0),
            "relationship_started_at": payload.get("relationship_started_at"),
            "stage_evidence": dict(payload.get("stage_evidence") or {}),
            "algorithm_version": payload.get("algorithm_version"),
            "policy_version": payload.get("policy_version"),
        }
        return hash_payload(clean)
    if schema_version == "RelationshipEstimateSnapshot.v1":
        clean = {
            "schema_version": schema_version,
            "user_to_sora_estimate": dict(payload.get("user_to_sora_estimate") or {}),
            "source_contribution_count": int(payload.get("source_contribution_count") or 0),
            "algorithm_version": payload.get("algorithm_version"),
        }
        return hash_payload(clean)
    clean = {
        key: value
        for key, value in payload.items()
        if key not in {"snapshot_id", "pair_id", "revision", "generation", "status", "checksum", "generated_at", "source_watermark", "agreement_revision"}
    }
    return hash_payload(clean)
