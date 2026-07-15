from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Mapping

from sora_sandbox.agent import AgentRunner
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.persona.eval import run_persona_eval
from sora_sandbox.persona.contracts import DEFAULT_DEFINITION, DEFAULT_TRAITS
from sora_sandbox.persona.store import PersonaStore
from sora_sandbox.persona.system import PersonaSystem
from sora_sandbox.state import seed_state


JsonDict = dict[str, Any]


class DeterministicProvider:
    provider_name = "test_deterministic_persona_provider"

    def __init__(
        self,
        *,
        scope: str = "agent",
        target: str = "warmth",
        generalizability: float = 0.96,
        fail: bool = False,
    ) -> None:
        self.scope = scope
        self.target = target
        self.generalizability = generalizability
        self.fail = fail

    def evaluate(
        self,
        source_payload: Mapping[str, Any],
        *,
        agent_id: str,
        user_id: str | None,
        trait_definitions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        if self.fail:
            raise RuntimeError("provider unavailable")
        memory = dict(source_payload.get("memory") or {})
        return {
            "decision": "candidate",
            "agent_id": agent_id,
            "user_id": user_id,
            "target_scope": self.scope,
            "target_key": self.target,
            "direction": "increase",
            "reason": "reviewed evidence supports a bounded adjustment",
            "source_kind": "memory_projection",
            "source_ref": memory["id"],
            "evidence_refs": list(memory.get("evidence_ids") or []),
            "confidence": 0.97,
            "evidence_strength": 0.95,
            "consistency": 0.95,
            "generalizability": self.generalizability,
            "source_reliability": 0.97,
            "conflict_score": 0.02,
            "risk_penalty": 0.02,
            "sensitivity": memory.get("sensitivity", "low"),
            "explicit_feedback": True,
            "metadata": {
                "episode_refs": list(memory.get("evidence_episode_ids") or []),
                "source_revision": memory.get("updated_at") or source_payload.get("event_id"),
            },
        }, {"provider": self.provider_name, "status": "ok", "usage": {}}

    def summarize_reflection(
        self,
        snapshot: Mapping[str, Any],
        *,
        contributions: list[JsonDict],
    ) -> tuple[JsonDict | None, JsonDict]:
        refs = list(contributions[0]["evidence_refs"])
        return {
            "summary": "Sora has made one bounded, evidence-backed adjustment.",
            "facts": [{"text": "A reviewed adjustment was accepted.", "evidence_refs": refs}],
        }, {"provider": self.provider_name, "status": "ok"}


def projection_event(
    event_id: str,
    memory_id: str,
    *,
    user_id: str = "user-a",
    action: str = "upsert",
    sensitivity: str = "low",
) -> JsonDict:
    return {
        "contract": "ProjectionEvent.v1",
        "event_id": event_id,
        "target_subsystem": "persona",
        "event_type": {
            "upsert": "memory_upsert",
            "rollback": "memory_rollback",
        }.get(action, "memory_tombstone"),
        "action": action,
        "source": {"source_ref": memory_id, "committed_only": True},
        "memory": {
            "id": memory_id,
            "owner_user_id": user_id,
            "type": "persona_delta",
            "status": "active",
            "summary": "Sora consistently chose a warmer but still bounded response style.",
            "tags": ["persona", "reviewed"],
            "confidence": 0.97,
            "sensitivity": sensitivity,
            "evidence_ids": [f"evidence-{memory_id}-{index}" for index in range(3)],
            "evidence_episode_ids": [f"episode-{memory_id}-a", f"episode-{memory_id}-b"],
            "updated_at": f"revision-{event_id}",
        },
        "lifecycle": {"tier": "hot", "heat_score": 0.9},
        "idempotency": {
            "event_key": f"persona:{memory_id}:{action}",
            "payload_hash": f"hash-{event_id}",
            "projector_version": "memory_projection_gateway.v1",
        },
    }


class PersonaSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "persona.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_system(self, provider: Any | None = None) -> PersonaSystem:
        return PersonaSystem(
            PersonaStore(self.path),
            provider=provider,
            start_worker=False,
        )

    def test_seed_has_versioned_definition_snapshot_and_domain_safe_taxonomy(self) -> None:
        system = self.make_system()
        try:
            snapshot = system.store.active_snapshot()
            self.assertEqual(snapshot["schema_version"], "PersonaSnapshot.v1")
            self.assertEqual(len(snapshot["trait_values"]), 10)
            self.assertIn("affiliation", snapshot["trait_values"])
            self.assertNotIn("attachment", snapshot["trait_values"])
            self.assertEqual(system.store.pragma_health()["journal_mode"], "wal")
        finally:
            system.close()

    def test_projection_is_idempotent_and_async_worker_commits_bounded_delta(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            event = projection_event("event-1", "memory-1")
            first = system.projection_adapter.deliver(event)
            second = system.projection_adapter.deliver(event)
            self.assertEqual(first["status"], "applied")
            self.assertEqual(second["status"], "skipped_duplicate")
            result = system.process_pending()
            self.assertEqual(result["processed_count"], 1)
            contribution = system.store.list_contributions(active=True)[0]
            self.assertLessEqual(abs(contribution["delta"]), 0.004)
            self.assertEqual(system.store.list_candidates()[0]["status"], "committed")
            self.assertGreater(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
        finally:
            system.close()

    def test_per_user_adaptation_never_promotes_to_global_snapshot(self) -> None:
        system = self.make_system(DeterministicProvider(scope="per_user", target="playfulness"))
        try:
            before = system.store.active_snapshot()
            system.ingest_projection(projection_event("event-user", "memory-user", user_id="user-a"))
            system.process_pending()
            after = system.store.active_snapshot()
            self.assertEqual(before["trait_values"], after["trait_values"])
            adaptation = system.store.user_adaptations("user-a")
            self.assertGreater(adaptation["playfulness"]["value"], before["trait_values"]["playfulness"])
            self.assertEqual(system.store.user_adaptations("user-b"), {})
        finally:
            system.close()

    def test_rollback_deactivates_contribution_and_rebuilds_original_value(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            system.ingest_projection(projection_event("event-rb", "memory-rb"))
            system.process_pending()
            contribution = system.store.list_contributions(active=True)[0]
            result = system.rollback_contribution(contribution["contribution_id"], reason="test")
            self.assertTrue(result["snapshot"]["trait_values"]["warmth"] == 0.64)
            self.assertFalse(system.store.list_contributions()[0]["active"])
        finally:
            system.close()

    def test_forget_tombstones_source_and_prevents_relearning(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            system.ingest_projection(projection_event("event-forget", "memory-forget"))
            system.process_pending()
            forgotten = system.forget_source("memory-forget")
            self.assertEqual(forgotten["contributions_deactivated"], 1)
            replay = system.ingest_projection(projection_event("event-forget-replay", "memory-forget"))
            self.assertEqual(replay["status"], "rejected_policy")
            self.assertEqual(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
        finally:
            system.close()

    def test_growth_pause_routes_candidate_to_review_without_changing_initiative(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            control = system.set_growth_paused(True)
            self.assertTrue(control["growth_paused"])
            self.assertNotIn("initiative_permission", control)
            system.ingest_projection(projection_event("event-paused", "memory-paused"))
            system.process_pending()
            self.assertEqual(system.store.list_candidates()[0]["status"], "pending_review")
            self.assertEqual(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
        finally:
            system.close()

    def test_interruptibility_frame_suppresses_narrative_and_self_reference(self) -> None:
        system = self.make_system()
        try:
            frame = system.frame(
                interruptibility_frame={
                    "response_obligation": "required",
                    "response_shape": "brief",
                    "state_surface_permission": {"persona": "silent"},
                }
            )
            self.assertEqual(frame["expression_bias"], {})
            self.assertIsNone(frame["narrative_gist"])
            self.assertIsNone(frame["self_reference_candidate"])
            self.assertTrue(frame["core"])
        finally:
            system.close()

    def test_provider_failure_quarantines_without_state_change(self) -> None:
        system = self.make_system(DeterministicProvider(fail=True))
        try:
            system.ingest_projection(projection_event("event-fail", "memory-fail"))
            result = system.process_pending()
            self.assertEqual(result["results"][0]["status"], "quarantined")
            self.assertEqual(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
        finally:
            system.close()

    def test_concurrent_controls_are_serialized_by_single_writer(self) -> None:
        system = self.make_system()
        failures: list[Exception] = []

        def update(index: int) -> None:
            try:
                system.set_growth_paused(index % 2 == 0, user_id=f"user-{index % 3}")
            except Exception as exc:  # pragma: no cover - assertion records it.
                failures.append(exc)

        try:
            threads = [threading.Thread(target=update, args=(index,)) for index in range(30)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(system.store.pragma_health()["integrity"], "ok")
        finally:
            system.close()

    def test_legacy_migration_excludes_relationship_affect_interest_and_expression(self) -> None:
        system = self.make_system()
        try:
            legacy = seed_state()["persona"]
            result = system.migrate_legacy(legacy)
            self.assertEqual(result["status"], "examined_no_canonical_mutation")
            excluded = result["comparison"]["legacy_fields_not_owned_by_persona"]
            self.assertIn("relationship_state", excluded)
            self.assertIn("interest_graph", excluded)
            self.assertNotIn("Project Sora", str(system.export_state()["definition"]))
        finally:
            system.close()

    def test_agent_uses_canonical_persona_frame_and_disables_legacy_growth_path(self) -> None:
        system = self.make_system()
        try:
            runner = AgentRunner(FakeLLMClient(), persona_runtime=system)
            result = runner.run(seed_state(), "这个语气我很喜欢")
            frame = result["context_pack"]["persona_frame"]
            self.assertEqual(frame["runtime_mode"], "canonical_persona_system")
            self.assertEqual(frame["schema_version"], "PersonaFrame.v1")
            self.assertIsNone(result["response_plan"]["growth_event_candidate"])
        finally:
            system.close()

    def test_eval_suite_passes_all_current_invariants(self) -> None:
        system = self.make_system()
        try:
            result = run_persona_eval(system)
            self.assertEqual(result["status"], "passed")
            self.assertGreaterEqual(result["total_cases"], 10)
        finally:
            system.close()

    def test_module09_delivery_and_module10_registry_use_real_persona_adapter(self) -> None:
        system = self.make_system(DeterministicProvider())
        memory = MemoryRuntime(Path(self.temp.name) / "memory.db")
        try:
            memory.projector_bus.register_adapter("persona", system.projection_adapter)
            system.register_quality_ops(memory)
            created = memory.add_manual(
                "Sora repeatedly chose warm, bounded responses in reviewed interactions.",
                memory_type="persona_delta",
                tags=["persona", "reviewed"],
            )
            projection = memory.list_projector_outbox(limit=20)
            self.assertTrue(any(item["status"] == "processed" for item in projection))
            system.process_pending(limit=20)
            candidate = system.store.list_candidates(limit=1)[0]
            self.assertEqual(candidate["status"], "pending_review")
            system.approve_candidate(candidate["candidate_id"], reason="integration_review")
            self.assertGreater(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
            suites = {item["name"] for item in memory.list_quality_eval_suites()}
            actions = {item["action_name"] for item in memory.list_admin_action_specs()}
            self.assertIn("persona", suites)
            self.assertIn("persona_rebuild", actions)
            frame = memory.get_projection_frame("persona")
            self.assertEqual(frame["adapter"], "persona_projection_adapter.v1")
            self.assertIn(created["id"], frame["source_memory_ids"])
        finally:
            memory.close()
            system.close()

    def test_definition_release_and_rollback_rebuild_snapshots(self) -> None:
        system = self.make_system()
        try:
            definition = {**DEFAULT_DEFINITION, "taxonomy_version": "persona_traits.v2-test"}
            released = system.store.release_definition(
                definition,
                DEFAULT_TRAITS,
                expected_version=1,
                release_ref="test-signed-release",
                actor="test-release-manager",
            )
            self.assertEqual(released["definition_version"], 2)
            self.assertEqual(released["snapshot"]["definition_version"], 2)
            rolled_back = system.store.rollback_definition(
                1,
                reason="test_rollback",
                actor="test-release-manager",
            )
            self.assertEqual(rolled_back["definition_version"], 1)
            self.assertEqual(rolled_back["snapshot"]["definition_version"], 1)
        finally:
            system.close()

    def test_outbox_ack_is_idempotent_and_retention_ready(self) -> None:
        system = self.make_system()
        try:
            event = system.store.list_outbox(status="pending", limit=1)[0]
            first = system.store.acknowledge_outbox(event["event_id"], delivered=True)
            second = system.store.acknowledge_outbox(event["event_id"], delivered=True)
            self.assertEqual(first["status"], "delivered")
            self.assertTrue(second["idempotent"])
        finally:
            system.close()

    def test_low_generalizability_is_context_only_and_cannot_mutate_global_persona(self) -> None:
        system = self.make_system(DeterministicProvider(generalizability=0.40))
        try:
            before = system.store.active_snapshot()["trait_values"]
            system.ingest_projection(projection_event("event-context", "memory-context"))
            result = system.process_pending()
            self.assertIsNotNone(result["results"][0]["contribution"])
            contribution = system.store.list_contributions(active=True)[0]
            self.assertEqual(contribution["target_scope"], "per_user")
            self.assertEqual(before, system.store.active_snapshot()["trait_values"])
            candidate = system.store.list_candidates(limit=1)[0]
            self.assertEqual(candidate["candidate"]["metadata"]["gate_decision"], "context_only")
        finally:
            system.close()

    def test_agent_auto_commit_requires_independent_evidence_and_episode_diversity(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            event = projection_event("event-thin", "memory-thin")
            event["memory"]["evidence_ids"] = ["evidence-thin"]
            event["memory"]["evidence_episode_ids"] = ["episode-thin"]
            system.ingest_projection(event)
            system.process_pending()
            candidate = system.store.list_candidates(limit=1)[0]
            self.assertEqual(candidate["status"], "pending_review")
            self.assertEqual(candidate["gate_reason"], "agent_scope_insufficient_independent_evidence")
            self.assertEqual(system.store.list_contributions(active=True), [])
        finally:
            system.close()

    def test_high_risk_traits_always_require_review(self) -> None:
        system = self.make_system(DeterministicProvider(target="autonomy"))
        try:
            system.ingest_projection(projection_event("event-autonomy", "memory-autonomy"))
            system.process_pending()
            candidate = system.store.list_candidates(limit=1)[0]
            self.assertEqual(candidate["status"], "pending_review")
            self.assertEqual(candidate["gate_reason"], "high_risk_trait_requires_review")
        finally:
            system.close()

    def test_target_aware_idempotency_allows_distinct_traits_for_same_memory_revision(self) -> None:
        provider = DeterministicProvider(target="warmth")
        system = self.make_system(provider)
        try:
            first = projection_event("event-target-warmth", "memory-multi")
            first["memory"]["updated_at"] = "revision-shared"
            system.ingest_projection(first)
            system.process_pending()
            provider.target = "playfulness"
            second = projection_event("event-target-play", "memory-multi")
            second["memory"]["updated_at"] = "revision-shared"
            system.ingest_projection(second)
            system.process_pending()
            contributions = system.store.list_contributions(active=True)
            self.assertEqual({item["target_key"] for item in contributions}, {"warmth", "playfulness"})
            keys = {item["idempotency_key"] for item in system.store.list_candidates()}
            self.assertEqual(len(keys), 2)
        finally:
            system.close()

    def test_memory_rollback_deactivates_without_tombstone_and_accepts_new_revision(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            system.ingest_projection(projection_event("event-source", "memory-source"))
            system.process_pending()
            rollback = system.ingest_projection(
                projection_event("event-source-rollback", "memory-source", action="rollback")
            )
            self.assertEqual(rollback["status"], "rolled_back")
            self.assertFalse(system.store.list_contributions()[0]["active"])
            replay = system.ingest_projection(projection_event("event-source-v2", "memory-source"))
            self.assertEqual(replay["status"], "accepted")
        finally:
            system.close()

    def test_expired_frame_uses_base_definition_before_revalidation(self) -> None:
        system = self.make_system(DeterministicProvider())
        try:
            system.ingest_projection(projection_event("event-frame", "memory-frame"))
            system.process_pending()
            self.assertGreater(system.store.active_snapshot()["trait_values"]["warmth"], 0.64)
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            system.store._write(  # noqa: SLF001 - controlled expiry fault injection.
                lambda conn: conn.execute(
                    "UPDATE persona_frame SET expires_at=?, stale_until=? WHERE status='active'",
                    (past, past),
                ),
                priority=0,
                name="test_expire_frame",
            )
            fallback = system.frame()
            self.assertEqual(fallback["runtime_mode"], "base_definition_fallback")
            self.assertFalse(fallback["snapshot_applied"])
            self.assertEqual(fallback["traits"]["warmth"], 0.64)
            revalidated = system.frame()
            self.assertEqual(revalidated["runtime_mode"], "canonical_persona_system")
            self.assertGreater(revalidated["traits"]["warmth"], 0.64)
        finally:
            system.close()

    def test_interest_and_ephemeral_layers_are_typed_ttl_bound_and_framed(self) -> None:
        system = self.make_system()
        try:
            system.upsert_interest_state(
                "distributed-systems",
                sora_interest=0.82,
                shared_density=0.35,
                reason_refs=["evidence-interest"],
                attitude="engaged",
            )
            system.set_ephemeral_state(
                "focus",
                "careful_review",
                ttl_seconds=300,
                source_ref="turn-1",
            )
            frame = system.frame()
            self.assertEqual(frame["self_interests"][0]["topic_key"], "distributed-systems")
            self.assertEqual(frame["ephemeral_state"]["focus"]["value"], "careful_review")
            self.assertTrue(frame["judgment_directives"])
            self.assertEqual(system.store.instance()["active_definition_version"], 1)
        finally:
            system.close()

    def test_dead_letter_requires_resolution_before_bounded_cleanup(self) -> None:
        system = self.make_system()
        try:
            event = system.store.list_outbox(status="pending", limit=1)[0]
            system.store.acknowledge_outbox(
                event["event_id"],
                delivered=False,
                error="terminal test failure",
                terminal=True,
            )
            old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
            system.store._write(  # noqa: SLF001 - controlled retention fault injection.
                lambda conn: conn.execute(
                    "UPDATE persona_outbox SET created_at=? WHERE id=?",
                    (old, event["event_id"]),
                ),
                priority=0,
                name="test_age_dead_letter",
            )
            before = system.store.cleanup(batch_size=1)
            self.assertEqual(before["unresolved_dead_letters_over_retention"], 1)
            self.assertEqual(len(system.store.list_outbox(status="dead_letter")), 1)
            system.store.resolve_dead_letter(
                event["event_id"],
                resolution="exported",
                export_ref="audit-export-1",
                actor="test-admin",
            )
            after = system.store.cleanup(batch_size=1)
            self.assertEqual(after["resolved_dead_letters_deleted"], 1)
            self.assertEqual(system.store.list_outbox(status="dead_letter"), [])
        finally:
            system.close()

    def test_schema_v1_database_is_upgraded_and_instance_is_backfilled(self) -> None:
        system = self.make_system()
        system.close()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP TABLE persona_instance")
            conn.execute("PRAGMA user_version=1")
            conn.commit()
        finally:
            conn.close()
        upgraded = self.make_system()
        try:
            self.assertEqual(upgraded.store.pragma_health()["schema_version"], 2)
            self.assertEqual(upgraded.store.instance()["status"], "active")
            counts = upgraded.store.table_counts()
            self.assertIn("persona_interest_state", counts)
            self.assertIn("persona_ephemeral_state", counts)
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
