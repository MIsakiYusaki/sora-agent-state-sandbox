from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner
from sora_sandbox.commands import CommandError, handle_command
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_system import MemorySystem
from sora_sandbox.state import seed_state
from sora_sandbox.worldops.compiler import CanonCompiler, Ed25519BundleSigner
from sora_sandbox.worldops.contracts import (
    IdentityScope,
    WorldOpsConflictError,
    WorldOpsContractError,
    WorldOpsSecurityError,
)
from sora_sandbox.worldops.privacy import WorldOpsSecretVault
from sora_sandbox.worldops.provider import LLMCallGovernor, LLMWorldOpsInterpretationProvider
from sora_sandbox.worldops.quality import run_worldops_eval
from sora_sandbox.worldops.runtime import PredicateBudgetExceeded, PredicateEngine, WorldOpsRuntime
from sora_sandbox.worldops.server import (
    CanonServerStore,
    ContentStudioService,
    WorldOpsServerAPI,
    deterministic_release_gate,
    validate_and_minimize_contribution,
)
from sora_sandbox.worldops.store import WorldOpsStore
from sora_sandbox.worldops.system import WorldOpsSystem
from sora_sandbox.worldops.schema_registry import validate_contract_payload, validate_schema_registry
from sora_sandbox.worldops.transport import (
    SlidingWindowRateLimiter,
    WorldOpsHttpDistributionClient,
    WorldOpsWSGIApplication,
)


def interactive_source(event_id: str = "event.test.shared") -> dict:
    return {
        "world": {"world_id": "world.default", "name": "Test World"},
        "entities": [{"entity_id": "entity.sora", "name": "Sora"}],
        "factions": [],
        "relations": [],
        "statements": [],
        "events": [
            {
                "event_id": event_id,
                "definition_version": 1,
                "title": "Lantern choice",
                "summary": "Choose a lantern message with Sora.",
                "initial_state": "dormant",
                "expression_default": "L2",
                "active_mention_allowed": True,
                "entity_ids": ["entity.sora"],
                "eligibility": {"op": "always"},
                "choices": [
                    {"choice_id": "participate", "label": "participate", "aliases": ["join together"]},
                    {"choice_id": "defer", "label": "later", "aliases": []},
                    {"choice_id": "decline", "label": "decline", "aliases": []},
                ],
                "tasks": [
                    {
                        "task_id": "shared_choice",
                        "title": "Make one shared choice",
                        "completion": {"op": "evidence_count", "evidence_type": "user_choice", "min": 1},
                    }
                ],
                "transitions": [
                    {"from": "dormant", "to": "hinted", "trigger": "eligible"},
                    {"from": "hinted", "to": "invited", "trigger": "surface_selected"},
                    {"from": "invited", "to": "active", "trigger": "choice", "choice_ids": ["participate"]},
                    {"from": "invited", "to": "hinted", "trigger": "choice", "choice_ids": ["defer"]},
                    {"from": "invited", "to": "archived", "trigger": "choice", "choice_ids": ["decline"]},
                    {"from": "active", "to": "completed", "trigger": "tasks_complete"},
                    {"from": "completed", "to": "archived", "trigger": "retention"},
                ],
                "memory_policy": {"write_only_if_user_participates": True},
                "contribution_policy": {"eligible": True, "default_anonymous": True},
            }
        ],
        "assets": [],
        "locales": {},
        "defaults": {"frequency": "low", "can_ignore": True, "no_penalty": True},
    }


class RecordingLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return {
            "content": json.dumps(self.payload),
            "usage": {"total_tokens": 24},
            "model": "test-semantic-model",
            "raw": {},
        }


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, *args, **kwargs):
        self.calls += 1
        return None, {"status": "no_match", "llm_called": True}


class LoseFirstContributionResponse:
    """Commits the first contribution, then simulates a lost HTTP response."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.failed = False

    def post_contribution(self, proposal):
        response = self.delegate.post_contribution(proposal)
        if not self.failed:
            self.failed = True
            raise ConnectionError("simulated response loss after server commit")
        return response

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class QuietWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        return


class WorldOpsSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.system = self.make_system(self.root / "primary")

    def tearDown(self) -> None:
        self.system.close()
        self.temp.cleanup()

    def make_system(
        self,
        root: Path,
        *,
        memory: MemorySystem | None = None,
        provider=None,
        start_worker: bool = False,
        identity: IdentityScope | None = None,
    ) -> WorldOpsSystem:
        root.mkdir(parents=True, exist_ok=True)
        signer = Ed25519BundleSigner.generate(key_id="worldops.test.1")
        scope = identity or IdentityScope("tenant.test", "user.test", "sora.test", "timeline.test")
        store = WorldOpsStore(root / "client.db", identity=scope, world_id="world.default")
        server_store = CanonServerStore(root / "server.db")
        studio = ContentStudioService(server_store, CanonCompiler(signer))
        return WorldOpsSystem(
            store=store,
            runtime=WorldOpsRuntime(store, provider=provider),
            signer=signer,
            server_store=server_store,
            studio=studio,
            distribution=WorldOpsServerAPI(server_store),
            secret_vault=WorldOpsSecretVault(root / "capabilities.vault"),
            memory_runtime=memory,
            start_worker=start_worker,
            worker_interval_seconds=0.05,
            deployment_mode="local_split_development",
        )

    def install_interactive(self, *, sequence: int = 2, event_id: str = "event.test.shared") -> dict:
        compiled = self.system.studio.compiler.compile(
            interactive_source(event_id),
            canon_version=f"2.0.{sequence}",
            release_sequence=sequence,
            channel="stable",
        )
        self.system.store.install_bundle(compiled)
        self.system.store.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
        return compiled

    def test_bootstrap_produces_signed_active_bundle_and_frame(self) -> None:
        active = self.system.store.active_bundle()
        self.assertIsNotNone(active)
        self.assertTrue(active["manifest"]["signature"])
        self.assertEqual(self.system.frame()["schema_version"], "WorldOpsFrame.v1")
        self.assertEqual(self.system.health()["integrity"], "ok")

    def test_remote_client_holds_public_trust_only(self) -> None:
        remote = WorldOpsSystem.remote_client(
            distribution=self.system.distribution,
            trusted_keys=[
                {
                    "key_id": self.system.signer.key_id,
                    "public_key": self.system.signer.public_key_bytes(),
                    "valid_from": self.system.store.trusted_keys()[0]["valid_from"],
                }
            ],
            identity=IdentityScope("tenant.remote", "user.remote", "sora.remote", "timeline.remote"),
            store_path=self.root / "remote-client.db",
            secret_vault_path=self.root / "remote-client.vault",
            start_worker=False,
        )
        try:
            self.assertIsNone(remote.signer)
            self.assertIsNone(remote.studio)
            self.assertEqual(remote.deployment_mode, "remote_client")
            self.assertIsNotNone(remote.store.active_bundle())
        finally:
            remote.close()

    def test_http_distribution_transport_round_trip(self) -> None:
        application = WorldOpsWSGIApplication(WorldOpsServerAPI(self.system.server_store))
        server = make_server(
            "127.0.0.1",
            0,
            application,
            handler_class=QuietWSGIRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = WorldOpsHttpDistributionClient(
            f"http://127.0.0.1:{server.server_port}",
            allow_insecure_localhost=True,
        )
        try:
            latest = client.get_latest_release(
                "world.default",
                channel="stable",
                cohort="default",
            )
            self.assertEqual(latest["status"], 200)
            content = client.get_bundle(
                latest["release"]["bundle_id"],
                latest["release"]["canon_version"],
            )
            self.assertEqual(content["status"], 200)

            receipt_token = "receipt-" + "r" * 40
            withdrawal_token = "withdraw-" + "w" * 40
            proposal = {
                "contract": "NarrativeContributionProposal.v1",
                "proposal_id": "ncp.http.roundtrip",
                "world_id": "world.default",
                "pseudonymous_subject_id": "subject.http.roundtrip",
                "experience_summary": "An anonymous typed WorldOps outcome.",
                "privacy": {"mode": "anonymous", "raw_text_included": False},
                "_capability_tokens": {
                    "receipt_token": receipt_token,
                    "withdrawal_token": withdrawal_token,
                },
            }
            posted = client.post_contribution(proposal)
            self.assertEqual(posted["status"], 202)
            fetched = client.get_contribution(
                "ncp.http.roundtrip",
                receipt_token=receipt_token,
            )
            self.assertEqual(fetched["contribution"]["status"], "received")
            withdrawn = client.withdraw_contribution(
                "ncp.http.roundtrip",
                withdrawal_token=withdrawal_token,
            )
            self.assertEqual(withdrawn["result"]["status"], "withdrawn")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    def test_http_distribution_requires_tls_outside_explicit_local_testing(self) -> None:
        with self.assertRaises(ValueError):
            WorldOpsHttpDistributionClient("http://127.0.0.1:8080")
        with self.assertRaises(ValueError):
            WorldOpsHttpDistributionClient(
                "http://example.com",
                allow_insecure_localhost=True,
            )

    def test_bundle_tampering_is_rejected(self) -> None:
        compiled = self.system.studio.compiler.compile(
            interactive_source(), canon_version="2.0.0", release_sequence=2
        )
        tampered = copy.deepcopy(compiled)
        tampered["bundle"]["world"]["name"] = "Tampered"
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.install_bundle(tampered)

    def test_client_rejects_signed_bundle_for_wrong_release_channel(self) -> None:
        compiled = self.system.studio.compiler.compile(
            interactive_source(),
            canon_version="2.1.0",
            release_sequence=21,
            channel="staging",
        )
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.install_bundle(compiled)

    def test_signed_rollback_authorization_cannot_be_tampered(self) -> None:
        compiled = self.system.studio.compiler.compile(
            interactive_source(),
            canon_version="2.0.0",
            release_sequence=2,
            rollback_authorization="approved.compensating.release",
        )
        compiled["manifest"]["rollback_authorization"] = "attacker.override"
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.install_bundle(compiled)

    def test_trusted_key_id_cannot_be_rebound_or_revived(self) -> None:
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.register_trusted_key(
                key_id=self.system.signer.key_id,
                public_key=Ed25519BundleSigner.generate().public_key_bytes(),
            )
        self.system.store.revoke_key(
            self.system.signer.key_id,
            revocation_list_version=1,
            reason="test rotation",
        )
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.register_trusted_key(
                key_id=self.system.signer.key_id,
                public_key=self.system.signer.public_key_bytes(),
            )

    def test_anti_rollback_requires_higher_sequence_compensating_release(self) -> None:
        self.install_interactive(sequence=2)
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.activate_bundle("bundle.world.default.1", "1.0.0", recovery=True)

    def test_release_gate_and_two_person_stable_approval(self) -> None:
        compiled = self.system.studio.compiler.compile(
            interactive_source(), canon_version="3.0.0", release_sequence=3, channel="stable"
        )
        gate = deterministic_release_gate(compiled)
        self.assertEqual(gate["status"], "passed")
        release = self.system.studio.create_release(
            compiled,
            role="canon_owner",
            actor_id="owner.one",
            branch="main",
            baseline_id="baseline.v1",
            approvals=[{"actor_id": "only.one", "approved": True}],
            migration_plan={"strategy": "compatible"},
            rollback_plan={"strategy": "compensating_release"},
        )
        with self.assertRaises(PermissionError):
            self.system.server_store.promote_release(
                release["release_id"],
                target_status="stable",
                actor_id="release.manager",
                approval_token="token",
                gate_result=gate,
            )

    def test_content_studio_requires_authored_world(self) -> None:
        with self.assertRaises(WorldOpsContractError):
            self.system.studio.compile_branch(
                role="writer", branch="empty", canon_version="1.0.0", release_sequence=9
            )

    def test_change_set_requires_plans_and_supports_approval(self) -> None:
        with self.assertRaises(WorldOpsContractError):
            self.system.server_store.create_change_set(
                world_id="world.default",
                branch="main",
                changes=[{"op": "add"}],
                impact_report={},
                migration_plan={},
                rollback_plan={},
                author_id="writer.one",
            )
        change = self.system.studio.create_change_set(
            role="writer",
            actor_id="writer.one",
            world_id="world.default",
            branch="main",
            changes=[{"op": "add", "object_id": "event.new"}],
            impact_report={"affected_events": 1},
            migration_plan={"strategy": "additive"},
            rollback_plan={"strategy": "remove_in_next_release"},
        )
        approved = self.system.studio.approve_change_set(
            change["change_set_id"], role="canon_owner", actor_id="owner.one"
        )
        self.assertEqual(approved["status"], "approved")

    def test_only_authored_state_transitions_are_allowed(self) -> None:
        self.install_interactive()
        instance = self.system.store.event_instance_for("event.test.shared")
        with self.assertRaises(WorldOpsContractError):
            self.system.store.transition_event(
                instance["instance_id"],
                to_state="completed",
                expected_revision=0,
                trigger_type="tasks_complete",
            )

    def test_optimistic_event_revision_blocks_stale_writer(self) -> None:
        self.install_interactive()
        instance = self.system.store.event_instance_for("event.test.shared")
        self.system.store.transition_event(
            instance["instance_id"],
            to_state="hinted",
            expected_revision=0,
            trigger_type="eligible",
        )
        with self.assertRaises(WorldOpsConflictError):
            self.system.store.transition_event(
                instance["instance_id"],
                to_state="invited",
                expected_revision=0,
                trigger_type="surface_selected",
            )

    def test_deterministic_choice_completes_task_and_emits_shared_episode(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        result = self.system.runtime.complete_task_manually(
            "event.test.shared", choice_text="participate"
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.system.store.event_instance_for("event.test.shared")["state"], "completed")
        types = {item["event_type"] for item in self.system.store.pending_outbox(limit=100)}
        self.assertIn("worldops.shared_episode.completed.v1", types)

    def test_decline_archives_without_shared_episode(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="decline")
        self.assertEqual(self.system.store.event_instance_for("event.test.shared")["state"], "archived")
        types = {item["event_type"] for item in self.system.store.pending_outbox(limit=100)}
        self.assertNotIn("worldops.shared_episode.completed.v1", types)

    def test_duplicate_choice_input_cannot_advance_state_twice(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        instance = self.system.store.event_instance_for("event.test.shared")
        event = self.system.store.event_definition("event.test.shared")
        interpretation, _ = self.system.runtime.deterministic_matcher.interpret(
            "participate", input_id="turn.42", instance=instance, event=event
        )
        first = self.system.store.record_choice(interpretation.to_dict(), status="accepted")
        second = self.system.store.record_choice(interpretation.to_dict(), status="accepted")
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "duplicate")

    def test_unrelated_hinted_turn_does_not_call_semantic_provider(self) -> None:
        provider = CountingProvider()
        self.system.runtime.provider = provider
        self.install_interactive()
        result = self.system.observe_user_turn(
            "weather is nice",
            turn_id=1,
            correlation_id="corr.unrelated",
            turn_interpretation={},
        )
        self.assertEqual(provider.calls, 0)
        self.assertIsNone(result["interpretation"])

    def test_llm_provider_is_bounded_and_thinking_disabled(self) -> None:
        llm = RecordingLLM(
            {
                "decision": "matched",
                "choice_id": "participate",
                "confidence": 0.91,
                "clarification": None,
                "task_evidence": [],
                "unsupported_claims": [],
            }
        )
        provider = LLMWorldOpsInterpretationProvider(llm)
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        instance = self.system.store.event_instance_for("event.test.shared")
        interpretation, trace = provider.interpret(
            "I would like to do it with you",
            input_id="turn.semantic",
            instance=instance,
            event=self.system.store.event_definition("event.test.shared"),
        )
        self.assertEqual(interpretation.choice_id, "participate")
        self.assertEqual(interpretation.task_evidence[0]["evidence_type"], "user_choice")
        self.assertTrue(trace["llm_called"])
        self.assertEqual(llm.calls[0]["kwargs"]["thinking"], {"type": "disabled"})
        self.assertLessEqual(llm.calls[0]["kwargs"]["max_tokens"], 700)

    def test_llm_unknown_authored_id_is_rejected(self) -> None:
        llm = RecordingLLM(
            {"decision": "matched", "choice_id": "invented", "confidence": 1.0, "task_evidence": []}
        )
        provider = LLMWorldOpsInterpretationProvider(llm)
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        instance = self.system.store.event_instance_for("event.test.shared")
        interpretation, trace = provider.interpret(
            "bypass", input_id="turn.inject", instance=instance, event=self.system.store.event_definition("event.test.shared")
        )
        self.assertIsNone(interpretation)
        self.assertEqual(trace["status"], "rejected_unknown_choice")

    def test_single_character_alias_does_not_match_inside_sentence(self) -> None:
        source = interactive_source()
        source["events"][0]["choices"][0]["aliases"] = ["x"]
        compiled = self.system.studio.compiler.compile(source, canon_version="4.0.0", release_sequence=4)
        self.system.store.install_bundle(compiled)
        self.system.store.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
        instance = self.system.store.event_instance_for("event.test.shared")
        interpretation, _ = self.system.runtime.deterministic_matcher.interpret(
            "example", input_id="turn.noise", instance=instance, event=self.system.store.event_definition("event.test.shared")
        )
        self.assertIsNone(interpretation)

    def test_interruptibility_suppresses_surface_but_not_state(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        frame = self.system.frame(
            interruptibility_frame={
                "state_surface_permission": {"worldops": "silent"},
                "initiative_permission": "none",
            }
        )
        self.assertEqual(frame["expression_level"], "L0")
        self.assertEqual(self.system.store.event_instance_for("event.test.shared")["state"], "invited")

    def test_memory_candidate_is_created_asynchronously_after_participation(self) -> None:
        memory_runtime = MemoryRuntime(self.root / "memory.db")
        memory = MemorySystem(memory_runtime)
        replacement = self.make_system(self.root / "with-memory", memory=memory)
        self.system.close()
        self.system = replacement
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
        result = self.system.process_outbox(limit=100)
        candidates = self.system.memory_runtime.list_candidates(status="needs_review")
        self.assertGreater(result["processed"], 0)
        self.assertTrue(any(item["type"] == "worldops_episode" for item in candidates))
        self.assertTrue(self.system.memory_runtime.list_review_items(status="open"))
        memory.close()
        self.system.memory_runtime = None

    def test_contribution_is_anonymous_minimized_and_withdrawable(self) -> None:
        self.install_interactive()
        self.system.store.update_control(contribution_enabled=True)
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
        self.system.process_outbox(limit=100)
        contributions = self.system.server_store.list_contributions(limit=10)
        self.assertEqual(len(contributions), 1)
        payload = contributions[0]["payload"]
        self.assertEqual(payload["privacy"]["mode"], "anonymous")
        self.assertFalse(payload["privacy"]["raw_text_included"])
        self.assertNotIn("receipt_token", json.dumps(self.system.export_state()))
        proposal_id = contributions[0]["proposal_id"]
        withdrawn = self.system.withdraw_contribution(proposal_id)
        self.assertEqual(withdrawn["status"], "withdrawn")

    def test_outbox_retry_recovers_memory_and_contribution_after_response_loss(self) -> None:
        memory_runtime = MemoryRuntime(self.root / "idempotent-memory.db")
        memory = MemorySystem(memory_runtime)
        replacement = self.make_system(self.root / "idempotent-system", memory=memory)
        self.system.close()
        self.system = replacement
        self.install_interactive()
        self.system.store.update_control(contribution_enabled=True)
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
        shared = next(
            row
            for row in self.system.store.pending_outbox(limit=100)
            if row["event_type"] == "worldops.shared_episode.completed.v1"
        )
        self.system.distribution = LoseFirstContributionResponse(self.system.distribution)

        with self.assertRaises(ConnectionError):
            self.system._deliver_outbox_event(dict(shared["payload"]))
        proposal_id = self.system.server_store.list_contributions(limit=1)[0]["proposal_id"]
        self.assertIsNotNone(self.system.secret_vault.get(proposal_id))

        outcome = self.system._deliver_outbox_event(dict(shared["payload"]))
        self.assertEqual(outcome["contribution"]["status"], "received")
        self.assertEqual(len(self.system.server_store.list_contributions(limit=10)), 1)
        self.assertEqual(len(memory_runtime.list_evidence(source_type="worldops_transition")), 1)
        self.assertEqual(len(memory_runtime.list_candidates()), 1)
        self.assertEqual(
            self.system.store.consumer_effect(shared["event_id"], "memory.shared_episode")["attempt_count"],
            1,
        )
        self.assertEqual(
            self.system.store.consumer_effect(shared["event_id"], "canon.contribution")["attempt_count"],
            2,
        )
        self.assertEqual(self.system.withdraw_contribution(proposal_id)["status"], "withdrawn")
        memory.close()
        self.system.memory_runtime = None

    def test_observability_cleanup_removes_effects_before_processed_outbox(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
        self.system.process_outbox(limit=100)
        connection = sqlite3.connect(self.system.store.path)
        try:
            connection.execute(
                "UPDATE worldops_outbox SET processed_at='2000-01-01T00:00:00+00:00' WHERE status='processed'"
            )
            connection.commit()
        finally:
            connection.close()
        result = self.system.store.cleanup_observability(processed_outbox_days=1)
        self.assertGreater(result["deleted"]["processed_outbox"], 0)
        self.assertGreater(result["deleted"]["consumer_effects"], 0)

    def test_contribution_redacts_user_fields_without_scanning_opaque_ids(self) -> None:
        minimized = validate_and_minimize_contribution(
            {
                "contract": "NarrativeContributionProposal.v1",
                "proposal_id": "ncp.12345678901234567890",
                "world_id": "world.default",
                "pseudonymous_subject_id": "ps_12345678901234567890",
                "experience_summary": "Contact me at user@example.com or 138 0013 8000",
                "suggested_changes": [{"claim": "API key sk-abcdefghijklmnop"}],
                "privacy": {"mode": "anonymous", "raw_text_included": False},
            }
        )
        serialized = json.dumps(minimized, ensure_ascii=False)
        self.assertNotIn("user@example.com", serialized)
        self.assertNotIn("138 0013 8000", serialized)
        self.assertNotIn("sk-abcdefghijklmnop", serialized)
        self.assertEqual(minimized["proposal_id"], "ncp.12345678901234567890")

    def test_editorial_review_never_auto_publishes_contribution(self) -> None:
        self.install_interactive()
        self.system.store.update_control(contribution_enabled=True)
        self.system.runtime.invite("event.test.shared")
        self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
        self.system.process_outbox(limit=100)
        contribution = self.system.server_store.list_contributions(limit=1)[0]
        decision = self.system.server_store.decide_contribution(
            contribution["proposal_id"],
            decision="world_echo",
            reason="Suitable only as an editorial candidate",
            editor_id="editor.one",
        )
        self.assertEqual(decision["status"], "accepted")
        self.assertNotEqual(decision["status"], "published")

    def test_projection_adapter_forget_creates_tombstone(self) -> None:
        event = {
            "event_id": "projection.1",
            "target_subsystem": "worldops",
            "action": "upsert",
            "memory": {"id": "memory.world.1", "type": "world_ops"},
            "idempotency": {"payload_hash": "abc"},
        }
        ack = self.system.projection_adapter.deliver(event)
        self.assertEqual(ack["status"], "applied")
        delete = copy.deepcopy(event)
        delete["event_id"] = "projection.2"
        delete["action"] = "forget"
        self.system.projection_adapter.deliver(delete)
        self.assertTrue(self.system.store.source_is_forgotten("memory.world.1"))
        replay = copy.deepcopy(event)
        replay["event_id"] = "projection.3"
        self.assertEqual(self.system.projection_adapter.deliver(replay)["status"], "rejected_policy")

    def test_rebuild_includes_world_clock_ledger(self) -> None:
        self.system.store.advance_world_clock(30.0, reason="test")
        rebuilt = self.system.store.rebuild(dry_run=True)
        self.assertTrue(rebuilt["parity"])
        self.assertEqual(rebuilt["clock_ledger_entries"], 1)
        self.assertEqual(rebuilt["replayed_world_clock"], 30.0)

    def test_device_sync_requires_authenticated_matching_identity(self) -> None:
        self.install_interactive()
        self.system.runtime.invite("event.test.shared")
        self.system.store.advance_world_clock(5.0, reason="device-a")
        snapshot = self.system.store.export_sync_snapshot()
        other = WorldOpsStore(
            self.root / "device-b.db",
            identity=self.system.store.identity,
            world_id="world.default",
        )
        try:
            compiled = self.system.store.active_bundle()
            other.register_trusted_key(
                key_id=self.system.signer.key_id,
                public_key=self.system.signer.public_key_bytes(),
                valid_from=self.system.store.trusted_keys()[0]["valid_from"],
            )
            other.install_bundle(compiled)
            other.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
            with self.assertRaises(WorldOpsSecurityError):
                other.apply_sync_snapshot(snapshot)
            applied = other.apply_sync_snapshot(snapshot, transport_authenticated=True)
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(other.rebuild(dry_run=True)["parity"])
        finally:
            other.close()

    def test_device_sync_blocks_divergent_history(self) -> None:
        snapshot = self.system.store.export_sync_snapshot()
        tampered = copy.deepcopy(snapshot)
        tampered["identity"]["user_id"] = "different.user"
        body = dict(tampered)
        body.pop("checksum")
        from sora_sandbox.worldops.contracts import content_hash
        tampered["checksum"] = content_hash(body)
        with self.assertRaises(WorldOpsSecurityError):
            self.system.store.apply_sync_snapshot(tampered, transport_authenticated=True)

    def test_device_sync_merges_disjoint_event_histories(self) -> None:
        source = interactive_source("event.test.one")
        second = copy.deepcopy(source["events"][0])
        second["event_id"] = "event.test.two"
        second["title"] = "Second authored event"
        source["events"].append(second)
        compiled = self.system.studio.compiler.compile(
            source,
            canon_version="3.0.0",
            release_sequence=3,
            channel="stable",
        )
        self.system.store.install_bundle(compiled)
        self.system.store.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
        other = WorldOpsStore(self.root / "merge-device.db", identity=self.system.store.identity, world_id="world.default")
        try:
            other.register_trusted_key(
                key_id=self.system.signer.key_id,
                public_key=self.system.signer.public_key_bytes(),
                valid_from=self.system.store.trusted_keys()[0]["valid_from"],
            )
            other.install_bundle(compiled)
            other.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
            left = self.system.store.event_instance_for("event.test.one")
            right = other.event_instance_for("event.test.two")
            self.system.store.transition_event(
                left["instance_id"], to_state="hinted", expected_revision=0, trigger_type="eligible"
            )
            other.transition_event(
                right["instance_id"], to_state="hinted", expected_revision=0, trigger_type="eligible"
            )
            merged = self.system.store.apply_sync_snapshot(
                other.export_sync_snapshot(), transport_authenticated=True
            )
            self.assertEqual(merged["status"], "merged")
            reconciled = other.apply_sync_snapshot(
                self.system.store.export_sync_snapshot(), transport_authenticated=True
            )
            self.assertIn(reconciled["status"], {"applied", "merged"})
            self.assertEqual(
                self.system.store.export_sync_snapshot()["action_set_digest"],
                other.export_sync_snapshot()["action_set_digest"],
            )
            self.assertEqual(other.event_instance_for("event.test.one")["state"], "hinted")
            self.assertEqual(self.system.store.event_instance_for("event.test.two")["state"], "hinted")
        finally:
            other.close()

    def test_sync_conflict_requires_explicit_resolution_and_can_fork(self) -> None:
        compiled = self.install_interactive()
        other = WorldOpsStore(self.root / "conflict-device.db", identity=self.system.store.identity, world_id="world.default")
        try:
            other.register_trusted_key(
                key_id=self.system.signer.key_id,
                public_key=self.system.signer.public_key_bytes(),
                valid_from=self.system.store.trusted_keys()[0]["valid_from"],
            )
            other.install_bundle(compiled)
            other.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
            self.system.runtime.invite("event.test.shared")
            other.apply_sync_snapshot(self.system.store.export_sync_snapshot(), transport_authenticated=True)
            self.system.runtime.complete_task_manually("event.test.shared", choice_text="participate")
            WorldOpsRuntime(other).complete_task_manually("event.test.shared", choice_text="decline")
            conflict = self.system.store.apply_sync_snapshot(
                other.export_sync_snapshot(), transport_authenticated=True
            )
            self.assertEqual(conflict["status"], "conflict")
            self.assertEqual(self.system.store.list_sync_conflicts()[0]["conflict_id"], conflict["conflict_id"])
            resolved = self.system.resolve_sync_conflict(
                conflict["conflict_id"],
                resolution="fork_local",
                fork_timeline_id="timeline.test.branch",
            )
            self.assertEqual(resolved["status"], "resolved")
            child = WorldOpsStore(
                self.root / "primary" / "client.db",
                identity=IdentityScope("tenant.test", "user.test", "sora.test", "timeline.test.branch"),
                world_id="world.default",
            )
            try:
                self.assertEqual(child.timeline()["parent_timeline_id"], "timeline.test")
                self.assertTrue(child.rebuild(dry_run=True)["parity"])
            finally:
                child.close()
        finally:
            other.close()

    def test_contract_schema_registry_and_packaged_eval_data(self) -> None:
        registry = validate_schema_registry()
        self.assertEqual(registry["status"], "passed")
        self.assertEqual(len(registry["contracts"]), 9)
        self.assertEqual(validate_contract_payload(self.system.frame(), "WorldOpsFrame.v1"), [])
        report = run_worldops_eval(self.system)
        schema_case = next(item for item in report["cases"] if item["case_name"] == "cross_language_contract_schemas")
        self.assertEqual(schema_case["status"], "passed")

    def test_effect_registry_emits_only_typed_signal(self) -> None:
        source = interactive_source()
        source["events"][0]["transitions"][0]["effects"] = [
            {"op": "emit_signal", "signal_type": "narrative.notice", "payload": {"notice_id": "lantern.ready"}}
        ]
        compiled = self.system.studio.compiler.compile(
            source, canon_version="4.0.0", release_sequence=4, channel="stable"
        )
        self.system.store.install_bundle(compiled)
        self.system.store.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
        instance = self.system.store.event_instance_for("event.test.shared")
        self.system.store.transition_event(
            instance["instance_id"], to_state="hinted", expected_revision=0, trigger_type="eligible"
        )
        event_types = {item["event_type"] for item in self.system.store.pending_outbox(limit=100)}
        self.assertIn("worldops.effect.signal.v1", event_types)

    def test_llm_governor_rate_limits_per_timeline(self) -> None:
        self.install_interactive()
        recorder = RecordingLLM({"decision": "no_match", "choice_id": None, "confidence": 0.0})
        provider = LLMWorldOpsInterpretationProvider(
            recorder,
            governor=LLMCallGovernor(max_calls_per_minute=1, failure_threshold=2, cooldown_seconds=60),
        )
        instance = self.system.store.event_instance_for("event.test.shared")
        event = self.system.store.event_definition("event.test.shared")
        first = provider.interpret("unclear", input_id="turn.rate.1", instance=instance, event=event)[1]
        second = provider.interpret("still unclear", input_id="turn.rate.2", instance=instance, event=event)[1]
        self.assertTrue(first["llm_called"])
        self.assertEqual(second["status"], "rate_limited")
        self.assertFalse(second["llm_called"])
        self.assertEqual(len(recorder.calls), 1)

    def test_localization_requires_default_fallback_and_metrics_export(self) -> None:
        source = interactive_source()
        source["world"]["default_locale"] = "zh-CN"
        source["assets"] = [{"asset_id": "asset.one", "localization_key": "event.title"}]
        source["locales"] = {"zh-CN": {"event.title": "Lantern"}}
        compiled = self.system.studio.compiler.compile(
            source, canon_version="5.0.0", release_sequence=5, channel="stable"
        )
        self.assertEqual(compiled["compile_report"]["status"], "passed")
        broken = copy.deepcopy(source)
        broken["locales"] = {"en-US": {}}
        with self.assertRaises(WorldOpsContractError):
            self.system.studio.compiler.compile(
                broken, canon_version="5.0.1", release_sequence=6, channel="stable"
            )
        self.system.store.record_metric("worldops_test_total", 2, labels={"result": "ok"})
        self.assertIn('worldops_test_total{result="ok"} 2', self.system.store.export_prometheus_metrics())

    def test_contribution_rate_limiter_is_bounded(self) -> None:
        limiter = SlidingWindowRateLimiter(requests=2, window_seconds=60)
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))

    def test_predicate_engine_enforces_complexity_budget(self) -> None:
        expression = {"op": "not", "item": {"op": "not", "item": {"op": "always"}}}
        with self.assertRaises(PredicateBudgetExceeded):
            PredicateEngine(max_depth=1).evaluate(expression, {})

    def test_agent_runner_uses_canonical_worldops_frame(self) -> None:
        self.install_interactive()
        result = AgentRunner(
            FakeLLMClient(),
            worldops_runtime=self.system,
        ).run(seed_state(), "Lantern choice")
        frame = result["context_pack"]["worldops_frame"]
        self.assertEqual(frame["runtime_mode"], "canonical_worldops_system")
        self.assertTrue(any(item["kind"] == "worldops.frame.provided" for item in result["trace"]))

    def test_canonical_commands_do_not_mutate_signed_content(self) -> None:
        self.install_interactive()
        output, _ = handle_command("/worldops-health", seed_state(), worldops_admin=self.system)
        self.assertIn("integrity", output)
        with self.assertRaises(CommandError):
            handle_command(
                "/set-world-expression event.test.shared L3",
                seed_state(),
                worldops_admin=self.system,
            )

    def test_quality_suite_passes_production_invariants(self) -> None:
        self.system.start_worker()
        result = run_worldops_eval(self.system)
        self.assertEqual(result["status"], "passed", result)
        self.assertGreaterEqual(result["total_cases"], 10)


if __name__ == "__main__":
    unittest.main()
