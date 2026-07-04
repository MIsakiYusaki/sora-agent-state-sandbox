from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from .memory_context_packer import MemoryContextPacker, PackedMemory
from .memory_graph_lite import graph_search
from .memory_index_fts import fts_search, temporal_search
from .memory_index_fts import search_tokens
from .memory_recall_planner import RecallPlanner, plan_to_json
from .memory_reranker import FeatureMemoryReranker, MemoryReranker
from .memory_schema import FusedMemoryHit, JsonDict, RecallPlan, RecallSubquery, RetrievalHit, json_dumps, new_id, utc_now
from .memory_store import row_value
from .memory_surface_policy import SurfacePolicyDecision, SurfacePolicyEngine, VISIBLE_ACTIONS
from .memory_trace import (
    cleanup_recall_traces,
    record_recall_plan_trace,
    record_recall_trace,
    record_surface_policy_trace,
)
from .memory_vector_index import VectorIndex


@dataclass
class MemoryRetrievalService:
    conn: Any
    lock: Any
    owner_user_id: str
    agent_id: str
    planner: RecallPlanner
    vector_index: VectorIndex
    packer: MemoryContextPacker
    reranker: MemoryReranker
    surface_policy: SurfacePolicyEngine = field(default_factory=SurfacePolicyEngine)

    def recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        top_k: int = 3,
        token_budget: int | None = None,
    ) -> JsonDict:
        started = time.perf_counter()
        plan = self.planner.plan(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            top_k=top_k,
            planner_context={"mode": "response_generation", "top_k": top_k, "token_budget": token_budget or 0},
        )
        budget = token_budget if token_budget is not None else plan.budget.token_budget
        if plan.skipped_reason:
            return self._empty_recollection(
                query_text,
                plan,
                plan.skipped_reason,
                latency_ms=(time.perf_counter() - started) * 1000,
                token_budget=budget,
            )

        with self.lock:
            groups, retrieval_errors = self._candidate_groups(plan)
            fused = self._fuse(groups, plan)
            reranked, rerank_metadata = self._rerank(fused, plan)
            governed, filtered = self._govern(reranked, plan, context_use_policy=context_use_policy)
            packed_candidates = [
                PackedMemory(
                    score=hit.score,
                    memory=self._memory_from_row(hit.row),
                    matched_backends=hit.matched_backends,
                    why_recalled=self._why_recalled(hit, plan),
                )
                for hit in governed[: max(top_k * 3, top_k)]
            ]

        surface_decision = self.surface_policy.decide(
            plan,
            packed_candidates,
            token_budget=budget,
            context_use_policy=context_use_policy,
            turn_frame=turn_frame,
            limit=max(top_k * 3, top_k),
        )
        packed = self.packer.pack(
            surface_decision.selected_candidates,
            token_budget=budget,
            explicit_recall=plan.explicit_recall,
            surface_action=surface_decision.action,
            legacy_surface_action=surface_decision.legacy_action,
            policy_by_memory_id=surface_decision.policy_by_memory_id,
            policy_profile=surface_decision.policy_profile,
            forbidden_disclosures=list(surface_decision.forbidden_disclosures),
            pre_suppressed=surface_decision.suppression_rows,
        )
        items = packed["items"][:top_k]
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        reason = surface_decision.decision_reason
        trace_items = self._trace_items(groups, fused, reranked, governed, filtered)
        degraded = bool(retrieval_errors) or bool(rerank_metadata.get("degraded"))
        trace_id = self._record_trace(
            query_text,
            plan=plan,
            surface_action=surface_decision.legacy_action,
            reason=reason,
            selected=[
                (float(item["score"]), self._memory_dict_for_id(item["memory_id"]))
                for item in items
            ],
            trace_items=trace_items,
            latency_ms=latency_ms,
            token_budget=budget,
            token_used=packed["token_used"],
            degraded=degraded,
            retrieval_errors=retrieval_errors,
            rerank_metadata=rerank_metadata,
            surface_decision=surface_decision,
            surface_provider_usage=packed.get("provider_usage", []),
        )
        self._record_access(
            items,
            trace_id=trace_id,
            surface_action=surface_decision.legacy_action,
            surface_action_v2=surface_decision.action,
        )
        return {
            "surface_action": surface_decision.legacy_action,
            "surface_action_v2": surface_decision.action,
            "surface_policy": surface_decision.public_dict(),
            "items": items,
            "retrieved_ids": [item["memory_id"] for item in items],
            "memory_summaries": [item["gist"] for item in items],
            "evidence_level": "strong" if items else "none",
            "forbidden_disclosures": packed.get("forbidden_disclosures", []),
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "hybrid_rrf",
            "recall_plan": self._plan_json(plan),
            "rerank": _public_rerank_metadata(rerank_metadata),
            "packing": {
                "strategy": packed["packing_strategy"],
                "token_budget": packed["token_budget"],
                "token_used": packed["token_used"],
                "suppressed": packed["suppressed"],
                "context_frame": packed["context_frame"],
                "provider_usage": packed.get("provider_usage", []),
            },
            "memory_context_frame": packed["context_frame"],
            "consent_request": surface_decision.consent_request,
            "degraded": degraded,
            "degradation_reasons": retrieval_errors + _rerank_errors(rerank_metadata),
            "latency_ms": latency_ms,
        }

    def related_memories(self, query_text: str, *, top_k: int = 8) -> list[JsonDict]:
        planned = self.planner.plan(
            query_text,
            turn_frame=None,
            context_use_policy=None,
            top_k=top_k,
            planner_context={"mode": "write_related_memory", "top_k": top_k},
        )
        plan = RecallPlan(
            query_text=query_text,
            top_k=top_k,
            tokens=planned.tokens,
            salient_terms=planned.salient_terms,
            explicit_recall=False,
            backends=planned.backends or ("fts", "vector", "temporal", "graph"),
            service_budget_ms=planned.service_budget_ms,
            planner_mode="extractor_related_memory",
            decision=planned.decision,
            query_intent="write_related_memory",
            scene=planned.scene,
            subqueries=planned.subqueries,
            budget=planned.budget,
            surface_hint="none",
            decision_reasons=list(planned.decision_reasons) + ["related-memory lookup for extractor write decision"],
            planner_trace=planned.planner_trace,
        )
        if not plan.tokens:
            return []
        with self.lock:
            groups, _retrieval_errors = self._candidate_groups(plan)
            hits, _rerank_metadata = self._rerank(self._fuse(groups, plan), plan)
            related: list[JsonDict] = []
            for hit in hits[:top_k]:
                memory = self._memory_from_row(hit.row)
                memory["related_score"] = hit.score
                memory["matched_backends"] = list(hit.matched_backends)
                related.append(memory)
            return related

    def cleanup_traces(self, *, older_than_days: int = 30, keep_pinned: bool = True) -> JsonDict:
        with self.lock:
            return cleanup_recall_traces(
                self.conn,
                older_than_days=older_than_days,
                keep_pinned=keep_pinned,
            )

    def _candidate_groups(self, plan: RecallPlan) -> tuple[list[list[RetrievalHit]], list[JsonDict]]:
        if plan.subqueries:
            groups: list[list[RetrievalHit]] = []
            errors: list[JsonDict] = []
            for subquery in plan.subqueries:
                subplan = _plan_for_subquery(plan, subquery)
                subgroups, suberrors = self._candidate_groups_for_single_plan(subplan)
                groups.extend(subgroups)
                errors.extend(
                    {
                        **error,
                        "query_id": subquery.query_id,
                        "subquery_text": subquery.text,
                    }
                    for error in suberrors
                )
            return [group for group in groups if group], errors
        return self._candidate_groups_for_single_plan(plan)

    def _candidate_groups_for_single_plan(self, plan: RecallPlan) -> tuple[list[list[RetrievalHit]], list[JsonDict]]:
        groups: list[list[RetrievalHit]] = []
        errors: list[JsonDict] = []
        limit = max(plan.top_k * 6, 18)
        if "fts" in plan.backends and plan.tokens:
            try:
                groups.append(
                    fts_search(
                        self.conn,
                        self.owner_user_id,
                        self.agent_id,
                        plan.tokens,
                        limit,
                    )
                )
            except Exception as exc:
                errors.append(_backend_error("fts", exc))
        if _plan_uses_vector(plan):
            try:
                groups.append(
                    self.vector_index.search(
                        plan.query_text,
                        owner_user_id=self.owner_user_id,
                        agent_id=self.agent_id,
                        limit=limit,
                    )
                )
            except Exception as exc:
                errors.append(_backend_error(self.vector_index.backend_name, exc))
        candidate_ids = {
            hit.memory_id
            for group in groups
            for hit in group
        }
        if "temporal" in plan.backends:
            try:
                groups.append(
                    temporal_search(
                        self.conn,
                        self.owner_user_id,
                        self.agent_id,
                        max(plan.top_k * 3, 9),
                        candidate_ids=candidate_ids or None,
                    )
                )
            except Exception as exc:
                errors.append(_backend_error("temporal", exc))
        if "graph" in plan.backends or "graph_lite" in plan.backends:
            try:
                groups.append(
                    graph_search(
                        self.conn,
                        owner_user_id=self.owner_user_id,
                        agent_id=self.agent_id,
                        query_text=plan.query_text,
                        limit=max(plan.top_k * 4, 12),
                    )
                )
            except Exception as exc:
                errors.append(_backend_error("graph", exc))
        return [group for group in groups if group], errors

    def _fuse(self, groups: list[list[RetrievalHit]], plan: RecallPlan) -> list[FusedMemoryHit]:
        weights = _weights_for_plan(plan)
        scores: dict[str, float] = defaultdict(float)
        rows: dict[str, Any] = {}
        backends: dict[str, set[str]] = defaultdict(set)
        for group in groups:
            for hit in group:
                weight = weights.get(hit.backend)
                if weight is None and hit.backend.startswith("vector"):
                    weight = weights.get("vector", weights.get("vector_local", 1.0))
                scores[hit.memory_id] += (weight or 1.0) / (60 + hit.rank)
                rows.setdefault(hit.memory_id, hit.row)
                backends[hit.memory_id].add(hit.backend)
        fused = [
            FusedMemoryHit(
                memory_id=memory_id,
                score=score,
                row=rows[memory_id],
                matched_backends=tuple(sorted(backends[memory_id])),
            )
            for memory_id, score in scores.items()
        ]
        fused.sort(key=lambda item: item.score, reverse=True)
        return fused

    def _rerank(self, hits: list[FusedMemoryHit], plan: RecallPlan) -> tuple[list[FusedMemoryHit], JsonDict]:
        try:
            result = self.reranker.rerank(plan.query_text, hits, plan)
            return result["hits"], result.get("metadata", {})
        except Exception as exc:
            fallback = FeatureMemoryReranker().rerank(plan.query_text, hits, plan)
            metadata = fallback.get("metadata", {})
            metadata.update(
                {
                    "degraded": True,
                    "fallback_provider": metadata.get("provider"),
                    "provider": getattr(self.reranker, "name", self.reranker.__class__.__name__),
                    "model": getattr(self.reranker, "model", "unknown"),
                    "error": str(exc),
                }
            )
            return fallback["hits"], metadata

    def _govern(
        self,
        hits: list[FusedMemoryHit],
        plan: RecallPlan,
        *,
        context_use_policy: JsonDict | None = None,
    ) -> tuple[list[FusedMemoryHit], list[tuple[FusedMemoryHit, str]]]:
        allowed: list[FusedMemoryHit] = []
        filtered: list[tuple[FusedMemoryHit, str]] = []
        for hit in hits:
            row = hit.row
            reason = self._filter_reason(row, plan, context_use_policy=context_use_policy)
            if reason:
                filtered.append((hit, reason))
            else:
                allowed.append(hit)
        return allowed, filtered

    def _filter_reason(
        self,
        row: Any,
        plan: RecallPlan,
        *,
        context_use_policy: JsonDict | None = None,
    ) -> str | None:
        if row["status"] != "active":
            return "inactive"
        if row["retrieval_permission"] != "allow":
            return "retrieval_denied"
        if row["use_permission"] == "deny":
            return "use_denied"
        if self._is_archived(row["id"]) and not _plan_allows_archived(plan, context_use_policy):
            return "archived"
        if row["sensitivity"] == "sensitive" and not plan.explicit_recall:
            return "sensitive_requires_explicit_recall"
        return None

    def _is_archived(self, memory_id: str) -> bool:
        row = self.conn.execute(
            "SELECT tier FROM memory_lifecycle_state WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return bool(row and row["tier"] == "archived")

    def _empty_recollection(
        self,
        query_text: str,
        plan: RecallPlan,
        reason: str,
        *,
        latency_ms: float,
        token_budget: int,
    ) -> JsonDict:
        trace_id = self._record_trace(
            query_text,
            plan=plan,
            surface_action="none",
            reason=reason,
            selected=[],
            trace_items=[],
            latency_ms=round(latency_ms, 2),
            token_budget=token_budget,
            token_used=0,
            degraded=False,
            retrieval_errors=[],
            rerank_metadata={},
        )
        return {
            "surface_action": "none",
            "surface_action_v2": "none",
            "items": [],
            "retrieved_ids": [],
            "memory_summaries": [],
            "evidence_level": "none",
            "forbidden_disclosures": [],
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "hybrid_rrf",
            "recall_plan": self._plan_json(plan),
            "packing": {
                "strategy": "template_deterministic_v1",
                "token_budget": token_budget,
                "token_used": 0,
                "suppressed": [],
                "context_frame": {
                    "version": "memory_context_frame_v2",
                    "policy_profile": "companion_default",
                    "surface_action": "none",
                    "legacy_surface_action": "none",
                    "items": [],
                    "forbidden_disclosures": [],
                    "budget": {"token_budget": token_budget, "token_used": 0, "pools": {}},
                    "packing_strategy": "template_budgeted_v2",
                },
            },
            "memory_context_frame": {
                "version": "memory_context_frame_v2",
                "policy_profile": "companion_default",
                "surface_action": "none",
                "legacy_surface_action": "none",
                "items": [],
                "forbidden_disclosures": [],
                "budget": {"token_budget": token_budget, "token_used": 0, "pools": {}},
                "packing_strategy": "template_budgeted_v2",
            },
            "degraded": False,
            "latency_ms": round(latency_ms, 2),
        }

    def _record_trace(
        self,
        query_text: str,
        *,
        plan: RecallPlan,
        surface_action: str,
        reason: str,
        selected: list[tuple[float, JsonDict]],
        trace_items: list[JsonDict],
        latency_ms: float,
        token_budget: int,
        token_used: int,
        degraded: bool,
        retrieval_errors: list[JsonDict] | None = None,
        rerank_metadata: JsonDict | None = None,
        surface_decision: SurfacePolicyDecision | None = None,
        surface_provider_usage: list[JsonDict] | None = None,
    ) -> str:
        with self.lock:
            plan_json = self._plan_json(plan)
            trace_id = record_recall_trace(
                self.conn,
                query_text,
                backend="hybrid_rrf",
                surface_action=surface_action,
                reason=reason,
                selected=selected,
                trace_items=trace_items,
                latency_ms=latency_ms,
                degraded=degraded,
                plan=plan_json,
                token_budget=token_budget,
                token_used=token_used,
                cost={
                    "embedding_provider": self.vector_index.provider.model,
                    "vector_backend": self.vector_index.backend_name,
                    "retrieval_errors": retrieval_errors or [],
                    "reranker": _public_rerank_metadata(rerank_metadata or {}),
                    "surface_policy": surface_decision.public_dict() if surface_decision else {},
                    "surface_provider_usage": surface_provider_usage or [],
                },
            )
            record_recall_plan_trace(
                self.conn,
                plan_json,
                recall_trace_id=trace_id,
                turn_id=plan.planner_trace.get("turn_id"),
                session_id=plan.planner_trace.get("session_id"),
            )
            if surface_decision is not None:
                record_surface_policy_trace(
                    self.conn,
                    recall_trace_id=trace_id,
                    policy_version=surface_decision.policy_version,
                    policy_profile=surface_decision.policy_profile,
                    provider=surface_decision.provider_name,
                    surface_action=surface_decision.action,
                    legacy_surface_action=surface_decision.legacy_action,
                    decision_reason=surface_decision.decision_reason,
                    forbidden_disclosures=list(surface_decision.forbidden_disclosures),
                    consent_request=surface_decision.consent_request,
                    items=surface_decision.trace_items(),
                    token_budget=token_budget,
                    token_used=token_used,
                    provider_usage=surface_provider_usage or [],
                    turn_id=plan.planner_trace.get("turn_id"),
                    session_id=plan.planner_trace.get("session_id"),
                )
            return trace_id

    def _record_access(
        self,
        items: list[JsonDict],
        *,
        trace_id: str,
        surface_action: str,
        surface_action_v2: str,
    ) -> None:
        if not items:
            return
        now = utc_now()
        surfaced_update = (
            ", surface_count = surface_count + 1, last_surfaced_at = ?"
            if surface_action_v2 in VISIBLE_ACTIONS
            else ""
        )
        with self.lock:
            for item in items:
                memory_id = item["memory_id"]
                if surfaced_update:
                    self.conn.execute(
                        f"""
                        UPDATE memory_record
                        SET access_count = access_count + 1,
                            last_accessed_at = ?
                            {surfaced_update}
                        WHERE id = ?
                        """,
                        (now, now, memory_id),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE memory_record
                        SET access_count = access_count + 1,
                            last_accessed_at = ?
                        WHERE id = ?
                        """,
                        (now, memory_id),
                    )
                self.conn.execute(
                    """
                    INSERT INTO memory_access_signal (
                        id, memory_id, trace_id, action, accessed_at, usefulness,
                        surface_action, metadata_json
                    ) VALUES (?, ?, ?, 'recalled', ?, NULL, ?, ?)
                    """,
                    (
                        new_id("access"),
                        memory_id,
                        trace_id,
                        now,
                        surface_action,
                        json_dumps(
                            {
                                "score": item.get("score"),
                                "matched_backends": item.get("matched_backends", []),
                                "surface_action_v2": surface_action_v2,
                                "surface_instruction": item.get("surface_instruction"),
                            }
                        ),
                    ),
                )
            self.conn.commit()

    def _trace_items(
        self,
        groups: list[list[RetrievalHit]],
        fused: list[FusedMemoryHit],
        reranked: list[FusedMemoryHit],
        governed: list[FusedMemoryHit],
        filtered: list[tuple[FusedMemoryHit, str]],
    ) -> list[JsonDict]:
        backend_hits: dict[str, RetrievalHit] = {}
        for group in groups:
            for hit in group:
                current = backend_hits.get(hit.memory_id)
                if current is None or hit.rank < current.rank:
                    backend_hits[hit.memory_id] = hit
        fused_scores = {hit.memory_id: hit.score for hit in fused}
        rerank_scores = {hit.memory_id: hit.score for hit in reranked}
        selected_ids = {hit.memory_id for hit in governed}
        filter_reasons = {hit.memory_id: reason for hit, reason in filtered}
        rows: list[JsonDict] = []
        for rank, hit in enumerate(reranked[:50], start=1):
            backend_hit = backend_hits.get(hit.memory_id)
            rows.append(
                {
                    "memory_id": hit.memory_id,
                    "rank": rank,
                    "score": hit.score,
                    "selected": hit.memory_id in selected_ids,
                    "backend": backend_hit.backend if backend_hit else "hybrid_rrf",
                    "backend_rank": backend_hit.rank if backend_hit else rank,
                    "backend_score": backend_hit.score if backend_hit else hit.score,
                    "rrf_score": fused_scores.get(hit.memory_id, hit.score),
                    "rerank_score": rerank_scores.get(hit.memory_id, hit.score),
                    "matched_backends": list(hit.matched_backends),
                    "filter_reason": filter_reasons.get(hit.memory_id),
                }
            )
        return rows

    def _memory_from_row(self, row: Any) -> JsonDict:
        evidence_rows = self.conn.execute(
            "SELECT evidence_id FROM memory_evidence WHERE memory_id = ? ORDER BY evidence_id",
            (row["id"],),
        ).fetchall()
        surface_feedback = self._surface_feedback_for_memory(row["id"])
        surface_consent = self._surface_consent_for_memory(row["id"])
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "summary": row["summary"],
            "content": row["summary"],
            "tags": _loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "sensitivity": row["sensitivity"],
            "evidence_ids": [item["evidence_id"] for item in evidence_rows],
            "supersedes_id": row["supersedes_id"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "retention_policy": row["retention_policy"],
            "retrieval_permission": row["retrieval_permission"],
            "use_permission": row["use_permission"],
            "disclosure_permission": row["disclosure_permission"],
            "surface_permission": row_value(row, "surface_permission", "auto") or "auto",
            "surface_cooldown_until": row_value(row, "surface_cooldown_until"),
            "relationship_impact": row_value(row, "relationship_impact", 0.0),
            "schema_version": row["schema_version"],
            "index_watermark": row["index_watermark"],
            "access_count": row["access_count"],
            "last_accessed_at": row["last_accessed_at"],
            "surface_count": row_value(row, "surface_count", 0),
            "last_surfaced_at": row_value(row, "last_surfaced_at"),
            "surface_feedback": surface_feedback,
            "surface_consent": surface_consent,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _memory_dict_for_id(self, memory_id: str) -> JsonDict:
        row = self.conn.execute("SELECT * FROM memory_record WHERE id = ?", (memory_id,)).fetchone()
        return self._memory_from_row(row)

    def _surface_feedback_for_memory(self, memory_id: str) -> JsonDict:
        rows = self.conn.execute(
            """
            SELECT * FROM memory_surface_feedback
            WHERE memory_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (memory_id,),
        ).fetchall()
        if not rows:
            return {"score": 0.0, "positive_count": 0, "negative_count": 0, "last_feedback_at": None}
        score = 0.0
        positive = 0
        negative = 0
        for row in rows:
            value = row_value(row, "value")
            if value is None:
                value = _feedback_default_value(str(row["feedback_type"]))
            value = float(value)
            score += value
            if value > 0:
                positive += 1
            elif value < 0:
                negative += 1
        normalized = max(-1.0, min(1.0, score / max(len(rows), 1)))
        return {
            "score": round(normalized, 4),
            "positive_count": positive,
            "negative_count": negative,
            "last_feedback_at": rows[0]["created_at"],
        }

    def _surface_consent_for_memory(self, memory_id: str) -> JsonDict:
        rows = self.conn.execute(
            """
            SELECT * FROM memory_surface_consent
            WHERE memory_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (memory_id, utc_now()),
        ).fetchall()
        granted = next((row for row in rows if row["status"] == "granted"), None)
        denied = next((row for row in rows if row["status"] in {"denied", "revoked"}), None)
        return {
            "granted": granted is not None and denied is None,
            "granted_at": row_value(granted, "granted_at") if granted is not None else None,
            "denied": denied is not None and granted is None,
            "denied_at": row_value(denied, "created_at") if denied is not None else None,
        }

    def _why_recalled(self, hit: FusedMemoryHit, plan: RecallPlan) -> str:
        backends = ", ".join(hit.matched_backends)
        if plan.explicit_recall:
            return f"explicit recall request matched via {backends}"
        return f"current turn matched via {backends}"

    def _plan_json(self, plan: RecallPlan) -> JsonDict:
        payload = plan_to_json(plan)
        payload["active_vector_backend"] = self.vector_index.backend_name
        return payload


def _weights_for_plan(plan: RecallPlan) -> dict[str, float]:
    if plan.query_intent == "relationship_context":
        return {"fts": 0.7, "vector": 1.1, "vector_local": 1.1, "temporal": 0.8, "graph": 1.0}
    if plan.query_intent in {"domain_continuity", "project_continuity"}:
        return {"fts": 1.1, "vector": 1.0, "vector_local": 1.0, "temporal": 0.85, "graph": 0.65}
    if plan.query_intent == "write_related_memory":
        return {"fts": 1.0, "vector": 1.15, "vector_local": 1.15, "temporal": 0.75, "graph": 0.85}
    query = plan.query_text.lower()
    if any(term in query for term in ("last time", "recent", "together", "episode")):
        return {"fts": 0.7, "vector": 0.9, "vector_local": 0.9, "temporal": 1.3, "graph": 0.5}
    if any(term in query for term in ("who", "where", "which", "name", "relationship")):
        return {"fts": 1.0, "vector": 1.0, "vector_local": 1.0, "temporal": 0.5, "graph": 1.1}
    if plan.explicit_recall:
        return {"fts": 1.1, "vector": 1.0, "vector_local": 1.0, "temporal": 0.6, "graph": 0.5}
    return {"fts": 1.0, "vector": 1.05, "vector_local": 1.05, "temporal": 0.55, "graph": 0.4}


def _plan_uses_vector(plan: RecallPlan) -> bool:
    return any(backend == "vector" or backend.startswith("vector") for backend in plan.backends)


def _backend_error(backend: str, exc: Exception) -> JsonDict:
    return {"backend": backend, "error": str(exc), "error_type": exc.__class__.__name__}


def _plan_for_subquery(plan: RecallPlan, subquery: RecallSubquery) -> RecallPlan:
    return replace(
        plan,
        query_text=subquery.text,
        top_k=subquery.top_k or plan.top_k,
        tokens=subquery.tokens or search_tokens(subquery.text),
        salient_terms=subquery.salient_terms or plan.salient_terms,
        backends=subquery.backends or plan.backends,
    )


def _public_rerank_metadata(metadata: JsonDict) -> JsonDict:
    return {
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "degraded": bool(metadata.get("degraded")),
        "fallback_provider": metadata.get("fallback_provider"),
        "usage": metadata.get("usage", {}),
        "error": metadata.get("error"),
    }


def _rerank_errors(metadata: JsonDict) -> list[JsonDict]:
    if not metadata.get("error"):
        return []
    return [
        {
            "backend": "reranker",
            "provider": metadata.get("provider"),
            "error": metadata.get("error"),
        }
    ]


def _plan_allows_archived(
    plan: RecallPlan,
    context_use_policy: JsonDict | None = None,
) -> bool:
    if context_use_policy and bool(context_use_policy.get("include_archived")):
        return True
    query = str(plan.query_text or "").lower()
    deep_terms = (
        "deep recall",
        "archived",
        "archive",
        "归档",
        "深度回忆",
        "很久以前",
        "更早以前",
        "历史记忆",
    )
    return bool(plan.explicit_recall and any(term in query for term in deep_terms))


def _feedback_default_value(feedback_type: str) -> float:
    normalized = feedback_type.lower().strip()
    if normalized in {"helpful", "useful", "use_more", "good_surface", "relevant"}:
        return 1.0
    if normalized in {"too_much", "not_relevant", "bad_surface", "annoying", "wrong_context"}:
        return -1.0
    return 0.0


def _loads(value: str, default: Any) -> Any:
    import json

    try:
        return json.loads(value)
    except Exception:
        return default
