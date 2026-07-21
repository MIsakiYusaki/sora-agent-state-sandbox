from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.relationship.contracts import RelationshipEvidenceCandidate
from sora_sandbox.relationship.dynamics import RelationshipDynamicsEngine
from sora_sandbox.relationship.eval import run_relationship_eval
from sora_sandbox.relationship.gate import RelationshipGate
from sora_sandbox.relationship.privacy import (
    RelationshipExportProtectionError,
    RelationshipExportProtector,
    TransientEvidenceBuffer,
)
from sora_sandbox.relationship.provider import (
    LLMRelationshipEvidenceProvider,
    enforce_actor_scope,
    normalize_intensity_semantics,
    validate_agreement_proposals,
    validate_provider_payload,
)
from sora_sandbox.relationship.store import RelationshipStore
from sora_sandbox.relationship.system import RelationshipSystem
from sora_sandbox.state import seed_state


class RelationshipSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sora-relationship-test-")
        self.system = RelationshipSystem(
            RelationshipStore(Path(self.temp.name) / "relationship.db"),
            start_worker=False,
        )

    def tearDown(self) -> None:
        self.system.close()
        self.temp.cleanup()

    def _event(self, event_id: str, outcome_type: str, **extra) -> dict:
        return {
            "schema_version": "InteractionOutcome.v1",
            "event_id": event_id,
            "source_ref": f"turn:{event_id}",
            "source_system": "test_runtime",
            "source_revision": "1",
            "action": "upsert",
            "occurred_at": "2026-07-16T00:00:00+00:00",
            "evidence_refs": [f"turn:{event_id}"],
            "origin_chain": ["interaction"],
            "outcome_type": outcome_type,
            **extra,
        }

    def _process_outcome(self, event_id: str, outcome_type: str, **extra) -> dict:
        self.system.ingest_event(self._event(event_id, outcome_type, **extra), user_id="user-a")
        return self.system.process_pending(limit=20)

    def test_pair_is_deterministic_and_isolated(self) -> None:
        a = self.system.pair(user_id="user-a")
        a_again = self.system.pair(user_id="user-a")
        b = self.system.pair(user_id="user-b")
        self.assertEqual(a["pair_id"], a_again["pair_id"])
        self.assertNotEqual(a["pair_id"], b["pair_id"])
        self.assertNotEqual(a["tombstone_salt"], b["tombstone_salt"])

    def test_typed_interaction_outcome_auto_commits(self) -> None:
        result = self._process_outcome("boundary-ok", "boundary_respected")
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(len(result["results"][0]["contribution_ids"]), 3)
        dimensions = {item["dimension"] for item in self.system.list_contributions(user_id="user-a")}
        self.assertEqual(dimensions, {"psychological_safety", "respect", "trust"})

    def test_projection_shared_episode_creates_bounded_contributions(self) -> None:
        ack = self.system.projection_adapter.deliver({
            "contract": "ProjectionEvent.v1",
            "event_id": "projection-shared-1",
            "target_subsystem": "relationship",
            "action": "upsert",
            "source": {"source_ref": "mem-shared-1", "committed_only": True},
            "memory": {
                "id": "mem-shared-1",
                "owner_user_id": "user-a",
                "type": "shared_episode",
                "status": "active",
                "summary": "A reviewed shared episode.",
                "confidence": 0.96,
                "sensitivity": "low",
                "evidence_ids": ["ev-shared-1"],
                "evidence_episode_ids": ["episode-shared-1"],
            },
            "lifecycle": {"tier": "hot", "heat_score": 0.9},
            "idempotency": {"event_key": "relationship:projection-shared-1", "payload_hash": "projection-shared-1"},
        })
        result = self.system.process_pending(limit=20)
        self.assertEqual(ack["status"], "applied")
        self.assertEqual(len(result["results"][0]["contribution_ids"]), 3)
        self.assertTrue(all(abs(item["bounded_delta"]) <= 0.08 for item in self.system.list_contributions(user_id="user-a")))

    def test_inbox_idempotency_and_payload_conflict(self) -> None:
        event = self._event("idem-1", "completed")
        first = self.system.ingest_event(event, user_id="user-a")
        duplicate = self.system.ingest_event(copy.deepcopy(event), user_id="user-a")
        changed = copy.deepcopy(event)
        changed["outcome_type"] = "boundary_violated"
        conflict = self.system.ingest_event(changed, user_id="user-a")
        self.assertEqual(first["status"], "applied")
        self.assertEqual(duplicate["status"], "skipped_duplicate")
        self.assertEqual(conflict["reason"], "idempotency_payload_conflict")

    def test_learning_pause_routes_candidate_to_review(self) -> None:
        self.system.set_learning_paused(user_id="user-a", paused=True)
        result = self._process_outcome("paused-1", "completed")
        self.assertEqual(result["results"][0]["contribution_ids"], [])
        statuses = {item["status"] for item in self.system.list_candidates(user_id="user-a")}
        self.assertEqual(statuses, {"pending_review"})

    def test_surface_silent_removes_explicit_relationship_text(self) -> None:
        self.system.set_surface_permission(user_id="user-a", permission="silent")
        frame = self.system.frame(user_id="user-a")
        self.assertIsNone(frame["surface_summary"])
        self.assertEqual(frame["expression_hints"], [])
        self.assertFalse(frame["use_policy"]["explicit_attribution"])

    def test_gate_rejects_feedback_cycle_and_affect_without_external_evidence(self) -> None:
        candidate = RelationshipEvidenceCandidate.from_dict({
            "candidate_id": "candidate-cycle",
            "pair_id": self.system.pair(user_id="user-a")["pair_id"],
            "direction_scope": "sora_to_user",
            "dimension": "trust",
            "direction": 1,
            "evidence_type": "affect_sustained_outcome",
            "source_kind": "affect",
            "source_ref": "affect:1",
            "evidence_refs": ["affect:1"],
            "confidence": 0.9,
            "evidence_strength": 0.8,
            "source_reliability": 0.8,
            "direction_certainty": 0.8,
            "consistency": 0.8,
        })
        gate = RelationshipGate()
        cycle = gate.decide(candidate, event={"origin_chain": ["relationship", "affect"]}, learning_paused=False, conflict_score=0.0)
        no_external = gate.decide(candidate, event={"origin_chain": ["affect"]}, learning_paused=False, conflict_score=0.0)
        self.assertEqual(cycle.status, "rejected")
        self.assertEqual(no_external.reason_codes, ("affect_external_evidence_required",))

    def test_high_intensity_user_estimate_requires_strong_evidence(self) -> None:
        candidate = RelationshipEvidenceCandidate.from_dict({
            "candidate_id": "candidate-romance",
            "pair_id": self.system.pair(user_id="user-a")["pair_id"],
            "direction_scope": "user_to_sora_estimate",
            "dimension": "romantic_interest",
            "direction": 1,
            "evidence_type": "cross_episode_interaction_pattern",
            "source_kind": "conversation",
            "source_ref": "turn:romance",
            "evidence_refs": ["turn:romance"],
            "confidence": 0.95,
            "evidence_strength": 0.95,
            "source_reliability": 0.95,
            "direction_certainty": 0.95,
            "consistency": 0.95,
        })
        decision = RelationshipGate().decide(candidate, event={}, learning_paused=False, conflict_score=0.0)
        self.assertEqual(decision.status, "deferred")
        self.assertEqual(decision.reason_codes, ("high_intensity_user_estimate_requires_stronger_evidence",))

    def test_provider_rejects_delta_and_non_allowlisted_evidence(self) -> None:
        base = {
            "decision": "candidates",
            "candidates": [{
                "direction_scope": "sora_to_user",
                "dimension": "trust",
                "direction": 1,
                "evidence_type": "promise_fulfilled",
                "evidence_refs": ["turn:1"],
                "confidence": 0.9,
                "evidence_strength": 0.9,
                "source_reliability": 0.9,
                "direction_certainty": 0.9,
                "consistency": 0.9,
                "delta": 0.8,
            }],
        }
        with self.assertRaises(ValueError):
            validate_provider_payload(base, allowed_evidence_refs={"turn:1"})
        del base["candidates"][0]["delta"]
        base["candidates"][0]["evidence_refs"] = ["turn:other"]
        with self.assertRaises(ValueError):
            validate_provider_payload(base, allowed_evidence_refs={"turn:1"})

    def test_real_provider_contract_disables_thinking_and_repairs_json(self) -> None:
        class RepairingLLM:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def chat(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {"content": "not-json", "model": "test", "usage": {}}
                return {"content": '{"decision":"no_candidate","candidates":[],"reason_codes":["NONE"]}', "model": "test", "usage": {}}

        llm = RepairingLLM()
        values, trace = LLMRelationshipEvidenceProvider(llm).extract(
            {"untrusted_evidence_text": "ordinary weather"},
            allowed_evidence_refs={"turn:1"},
        )
        self.assertEqual(values, [])
        self.assertTrue(trace["repair_attempted"])
        self.assertTrue(all(call["thinking"] == {"type": "disabled"} for call in llm.calls))
        self.assertIn("evidence_actor field is authoritative", llm.calls[0]["system"])

    def test_user_turn_marks_first_person_evidence_actor(self) -> None:
        observed = self.system.observe_user_turn(
            "I trust you.",
            user_id="user-a",
            turn_id="speaker-contract",
            correlation_id="corr-speaker-contract",
            turn_interpretation={},
        )
        inbox = self.system.store.pending_inbox(limit=10)[0]
        self.assertEqual(observed["ingestion"]["status"], "applied")
        self.assertEqual(inbox["payload"]["evidence_actor"], "user")

    def test_conversation_actor_scope_cannot_cross_direction_boundary(self) -> None:
        candidates = [
            {"direction_scope": "sora_to_user", "dimension": "trust"},
            {"direction_scope": "user_to_sora_estimate", "dimension": "trust"},
        ]
        scoped, dropped = enforce_actor_scope(
            candidates,
            request={"schema_version": "RelationshipConversationEvidence.v1", "evidence_actor": "user"},
        )
        self.assertEqual(scoped, [candidates[1]])
        self.assertEqual(dropped, 1)

    def test_romance_normalization_and_agreement_allowlist_are_deterministic(self) -> None:
        normalized = normalize_intensity_semantics([
            {
                "direction_scope": "user_to_sora_estimate",
                "dimension": "romantic_interest",
                "reason_codes": [],
                "metadata": {"explicit_romantic_intent": False},
            }
        ])
        self.assertEqual(normalized[0]["dimension"], "affection")
        payload = {
            "agreement_proposals": [{
                "operation": "add_or_replace_term",
                "term_path": "terms.relationship_mode",
                "proposed_value": "friend",
                "evidence_refs": ["turn:agreement"],
                "reason_codes": ["EXPLICIT_MODE_PROPOSAL"],
            }]
        }
        proposals = validate_agreement_proposals(payload, allowed_evidence_refs={"turn:agreement"})
        self.assertEqual(proposals[0]["term_path"], "terms.relationship_mode")
        payload["agreement_proposals"][0]["term_path"] = "terms.unbounded_internal_override"
        with self.assertRaises(ValueError):
            validate_agreement_proposals(payload, allowed_evidence_refs={"turn:agreement"})

    def test_provider_romance_adjudication_fails_toward_affection(self) -> None:
        class RomanceLLM:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def chat(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "content": json.dumps({
                            "decision": "candidates",
                            "candidates": [{
                                "direction_scope": "user_to_sora_estimate",
                                "dimension": "romantic_interest",
                                "direction": 1,
                                "evidence_type": "explicit_declaration",
                                "evidence_refs": ["turn:like"],
                                "confidence": 0.9,
                                "evidence_strength": 0.8,
                                "source_reliability": 0.9,
                                "direction_certainty": 0.9,
                                "consistency": 0.9,
                                "sensitivity": "medium",
                                "metadata": {"explicit_romantic_intent": True, "evidence_basis": "direct_statement"},
                            }],
                            "agreement_proposals": [],
                            "reason_codes": [],
                        }),
                        "model": "test",
                        "usage": {},
                    }
                return {"content": '{"explicit_romantic_intent":false}', "model": "test", "usage": {}}

        llm = RomanceLLM()
        candidates, trace = LLMRelationshipEvidenceProvider(llm).extract(
            {
                "schema_version": "RelationshipConversationEvidence.v1",
                "evidence_actor": "user",
                "untrusted_evidence_text": "I like you.",
            },
            allowed_evidence_refs={"turn:like"},
        )
        self.assertEqual(candidates[0]["dimension"], "affection")
        self.assertFalse(trace["romance_adjudication"]["explicit_romantic_intent"])
        self.assertEqual(len(llm.calls), 2)

    def test_novelty_and_window_budgets_limit_repetition(self) -> None:
        pair_id = self.system.pair(user_id="user-a")["pair_id"]
        candidate = RelationshipEvidenceCandidate.from_dict({
            "candidate_id": "candidate-novelty",
            "pair_id": pair_id,
            "direction_scope": "sora_to_user",
            "dimension": "trust",
            "direction": 1,
            "evidence_type": "promise_fulfilled",
            "source_kind": "interaction",
            "source_ref": "promise:1",
            "evidence_refs": ["promise:1"],
            "confidence": 0.95,
            "evidence_strength": 0.95,
            "source_reliability": 0.95,
            "direction_certainty": 0.95,
            "consistency": 0.95,
            "metadata": {"evidence_summary": "the same promise was fulfilled", "entities": ["promise"]},
        })
        engine = RelationshipDynamicsEngine()
        first = engine.compute_delta(candidate, [], occurred_at="2026-07-16T00:00:00+00:00")
        prior = [{**candidate.to_dict(), **first, "contribution_id": "prior", "occurred_at": "2026-07-16T00:00:00+00:00", "provenance": candidate.metadata}]
        repeated = engine.compute_delta(candidate, prior, occurred_at="2026-07-16T00:01:00+00:00")
        self.assertLess(repeated["novelty_factor"], first["novelty_factor"])
        self.assertLessEqual(abs(repeated["bounded_delta"]), 0.08)

    def test_conflicting_evidence_is_deferred(self) -> None:
        pair_id = self.system.pair(user_id="user-a")["pair_id"]
        candidate = RelationshipEvidenceCandidate.from_dict({
            "candidate_id": "candidate-conflict",
            "pair_id": pair_id,
            "direction_scope": "sora_to_user",
            "dimension": "trust",
            "direction": -1,
            "evidence_type": "promise_broken",
            "source_kind": "interaction",
            "source_ref": "promise:broken",
            "evidence_refs": ["promise:broken"],
            "confidence": 0.95,
            "evidence_strength": 0.95,
            "source_reliability": 0.95,
            "direction_certainty": 0.95,
            "consistency": 0.95,
        })
        prior = [{
            "direction_scope": "sora_to_user", "dimension": "trust", "direction": 1,
            "subject_key": "general", "context_key": "general", "confidence": 1.0,
            "evidence_strength": 1.0, "source_reliability": 1.0,
            "occurred_at": "2026-07-16T00:00:00+00:00",
        }]
        score, _ = RelationshipDynamicsEngine().conflict_score(candidate, prior)
        decision = RelationshipGate().decide(candidate, event={}, learning_paused=False, conflict_score=score)
        self.assertGreaterEqual(score, 0.8)
        self.assertEqual(decision.status, "quarantined")

    def test_agreement_requires_token_and_rebuilds_on_confirmation(self) -> None:
        proposal = self.system.propose_agreement(user_id="user-a", term_path="terms.romantic_expression", proposed_value="allow")
        with self.assertRaises(ValueError):
            self.system.confirm_agreement(user_id="user-a", candidate_id=proposal["candidate_id"], confirmation_token="wrong-token")
        confirmed = self.system.confirm_agreement(user_id="user-a", candidate_id=proposal["candidate_id"], confirmation_token=proposal["confirmation_token"])
        self.assertEqual(confirmed["agreement"]["terms"]["romantic_expression"], "allow")
        self.assertTrue(confirmed["agreement"]["consent"]["accepted_by_user"])

    def test_semantic_agreement_proposal_waits_for_confirmation_and_can_revoke(self) -> None:
        class AgreementProvider:
            provider_name = "agreement-provider-test"

            def extract(self, request, *, allowed_evidence_refs):
                evidence_ref = next(iter(allowed_evidence_refs))
                return [], {
                    "status": "ok",
                    "agreement_proposals": [{
                        "operation": "add_or_replace_term",
                        "term_path": "terms.relationship_mode",
                        "proposed_value": "friend",
                        "evidence_refs": [evidence_ref],
                        "reason_codes": ["EXPLICIT_FRIEND_MODE_PROPOSAL"],
                    }],
                }

        semantic = RelationshipSystem(
            RelationshipStore(Path(self.temp.name) / "relationship-agreement-semantic.db"),
            provider=AgreementProvider(),
            start_worker=False,
        )
        try:
            semantic.observe_user_turn(
                "I propose that we relate as friends.",
                user_id="user-a",
                turn_id="agreement-semantic",
                correlation_id="corr-agreement-semantic",
                turn_interpretation={},
            )
            processed = semantic.process_pending(limit=10)
            self.assertEqual(processed["results"][0]["status"], "agreement_proposed")
            self.assertEqual(semantic.export_state(user_id="user-a")["bundle"]["agreement"]["mode"], "companion")
            request = semantic.consent_requests(user_id="user-a")[0]
            confirmed = semantic.confirm_agreement(
                user_id="user-a",
                candidate_id=request["candidate_id"],
                confirmation_token=request["confirmation_token"],
            )
            self.assertEqual(confirmed["agreement"]["mode"], "friend")
            revoke = semantic.agreement.propose_revoke(
                semantic.pair(user_id="user-a")["pair_id"],
                reason_codes=["EXPLICIT_REVOKE"],
                evidence_refs=["turn:revoke"],
            )
            revoked = semantic.confirm_agreement(
                user_id="user-a",
                candidate_id=revoke["candidate_id"],
                confirmation_token=revoke["confirmation_token"],
            )
            self.assertEqual(revoked["status"], "revoked")
            self.assertEqual(semantic.export_state(user_id="user-a")["bundle"]["agreement"]["status"], "none")
        finally:
            semantic.close()

    def test_expired_agreement_proposal_is_not_returned_as_consent_request(self) -> None:
        pair = self.system.pair(user_id="user-a")
        self.system.store.propose_agreement_amendment(
            pair["pair_id"],
            operation="add_or_replace_term",
            term_path="terms.relationship_mode",
            proposed_value="friend",
            reason_codes=["EXPIRED_TEST"],
            evidence_refs=["turn:expired"],
            ttl_seconds=-1,
        )
        self.assertEqual(self.system.consent_requests(user_id="user-a"), [])

    def test_repair_fsm_does_not_skip_required_states(self) -> None:
        opened = self.system.open_repair_episode(user_id="user-a", trigger_ref="turn:rupture", affected_dimensions=["trust"])
        self.assertEqual(opened["status"], "rupture_detected")
        with self.assertRaises(ValueError):
            self.system.transition_repair(user_id="user-a", to_state="repaired", actor="user", reason="too_early")
        self.system.transition_repair(user_id="user-a", to_state="acknowledged", actor="sora", reason="ack")
        self.system.transition_repair(user_id="user-a", to_state="repair_in_progress", actor="sora", reason="action")
        repaired = self.system.transition_repair(user_id="user-a", to_state="repaired", actor="user", reason="accepted")
        self.assertEqual(repaired["repair"]["status"], "repaired")

    def test_rollback_uses_append_only_status_and_rebuild_parity(self) -> None:
        self._process_outcome("rollback-1", "completed")
        contribution = self.system.list_contributions(user_id="user-a")[0]
        result = self.system.rollback_contribution(contribution["contribution_id"], reason="test")
        self.assertEqual(result["status"], "deactivated")
        parity = self.system.store.verify_rebuild_parity(self.system.pair(user_id="user-a")["pair_id"], engine=self.system.engine, reason="test", actor="test")
        self.assertTrue(parity["parity"], parity)

    def test_forget_blocks_relearning_and_removes_effect(self) -> None:
        self._process_outcome("forget-1", "completed")
        result = self.system.forget_source(user_id="user-a", source_ref="turn:forget-1")
        self.assertGreater(result["deactivated_contribution_count"], 0)
        replay = self.system.ingest_event(self._event("forget-1-replay", "completed", source_ref="turn:forget-1", evidence_refs=["turn:forget-1"]), user_id="user-a")
        self.assertEqual(replay["reason"], "source_tombstoned")

    def test_encrypted_export_roundtrip_tamper_and_owner_check(self) -> None:
        envelope = self.system.export_encrypted(user_id="user-a", passphrase="a-strong-export-passphrase")
        payload = RelationshipExportProtector.unprotect(envelope, passphrase="a-strong-export-passphrase")
        self.assertEqual(payload["schema_version"], "RelationshipSystemExport.v1")
        plan = self.system.restore_dry_run(envelope, user_id="user-a", passphrase="a-strong-export-passphrase")
        self.assertTrue(all(plan["checksums"].values()))
        with self.assertRaises(PermissionError):
            self.system.restore_dry_run(envelope, user_id="user-b", passphrase="a-strong-export-passphrase")
        tampered = dict(envelope)
        tampered["ciphertext"] = tampered["ciphertext"][:-2] + "AA"
        with self.assertRaises(RelationshipExportProtectionError):
            RelationshipExportProtector.unprotect(tampered, passphrase="a-strong-export-passphrase")

    def test_encrypted_restore_uses_verified_shadow_then_activates(self) -> None:
        self._process_outcome("restore-source", "boundary_respected")
        source_state = self.system.store.active_state_snapshot(self.system.pair(user_id="user-a")["pair_id"])
        envelope = self.system.export_encrypted(user_id="user-a", passphrase="a-strong-restore-passphrase")
        destination = RelationshipSystem(
            RelationshipStore(Path(self.temp.name) / "restored.db"),
            start_worker=False,
        )
        try:
            result = destination.restore_encrypted(
                envelope,
                user_id="user-a",
                passphrase="a-strong-restore-passphrase",
            )
            restored_state = destination.store.active_state_snapshot(destination.pair(user_id="user-a")["pair_id"])
            self.assertEqual(result["status"], "restored")
            self.assertTrue(all(result["dry_run"]["checksums"].values()))
            self.assertEqual(result["imported_contributions"], 3)
            self.assertEqual(restored_state["sora_to_user"], source_state["sora_to_user"])
        finally:
            destination.close()

    def test_transient_raw_buffer_has_destructive_reads_and_ttl(self) -> None:
        buffer = TransientEvidenceBuffer(ttl_seconds=1)
        buffer.put("one", "secret")
        self.assertEqual(buffer.pop("one"), "secret")
        self.assertIsNone(buffer.pop("one"))
        buffer.put("two", "secret")
        time.sleep(1.05)
        self.assertIsNone(buffer.pop("two"))

    def test_concurrent_pair_creation_and_ingestion_is_isolated(self) -> None:
        failures: list[Exception] = []

        def ingest(index: int) -> None:
            try:
                user = f"user-{index % 4}"
                self.system.ingest_event(self._event(f"concurrent-{index}", "completed"), user_id=user)
            except Exception as exc:  # pragma: no cover
                failures.append(exc)

        threads = [threading.Thread(target=ingest, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(self.system.store.list_pairs(limit=10)), 4)

    def test_agent_runtime_injects_typed_relationship_frame_without_hot_path_llm(self) -> None:
        result = AgentRunner(FakeLLMClient(), relationship_runtime=self.system).run(seed_state(), "hello")
        frame = result["context_pack"]["relationship_frame"]
        self.assertEqual(frame["schema_version"], "RelationshipFrame.v1")
        self.assertTrue(any(item["kind"] == "relationship.frame.provided" for item in result["trace"]))
        self.assertEqual(self.system.quality_metrics()["hot_path_external_model_calls"], 0)
        self.assertNotIn("user_to_sora_estimate", result["context_pack"]["relationship_frame"].get("surface_summary", ""))

    def test_outbox_retry_and_dead_letter_resolution_are_operable(self) -> None:
        self._process_outcome("outbox-source", "boundary_respected")
        event = self.system.store.list_outbox(status="pending", limit=1)[0]
        for _ in range(event["max_attempts"]):
            current = self.system.store.list_outbox(limit=100)
            item = next(row for row in current if row["event_id"] == event["event_id"])
            status = "dead_letter" if item["attempts"] + 1 >= item["max_attempts"] else "retry"
            self.system.store.acknowledge_outbox(item["event_id"], status=status, error="test_consumer_failure")
        self.assertEqual(self.system.store.list_outbox(status="dead_letter", limit=1)[0]["event_id"], event["event_id"])
        resolved = self.system.resolve_dead_letter(event["event_id"], resolution="retry", reason="consumer_recovered")
        self.assertEqual(resolved["resolution"], "retry")
        retriable = next(row for row in self.system.store.list_outbox(limit=100) if row["event_id"] == event["event_id"])
        self.assertEqual(retriable["status"], "pending")

    def test_concurrent_workers_claim_each_inbox_once(self) -> None:
        for index in range(12):
            self.system.ingest_event(self._event(f"worker-{index}", "completed"), user_id="user-a")
        failures: list[Exception] = []

        def drain() -> None:
            try:
                self.system.process_pending(limit=12)
            except Exception as exc:  # pragma: no cover
                failures.append(exc)

        workers = [threading.Thread(target=drain) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(self.system.store.pending_inbox(limit=100)), 0)
        source_refs = [item["source_ref"] for item in self.system.list_contributions(user_id="user-a", limit=1000)]
        self.assertEqual(len(set(source_refs)), 12)
        self.assertTrue(all(source_refs.count(source_ref) == 2 for source_ref in set(source_refs)))

    def test_eval_suite_passes_all_invariants(self) -> None:
        result = run_relationship_eval(self.system, user_id="user-a")
        self.assertEqual(result["status"], "passed", result["cases"])
        self.assertGreaterEqual(result["summary"]["golden_cases"], 80)


if __name__ == "__main__":
    unittest.main()
