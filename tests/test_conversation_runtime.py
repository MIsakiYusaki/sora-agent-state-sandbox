from __future__ import annotations

import copy
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.conversation_runtime.context import (
    GENERATION_CONTEXT_MARKER,
    ContextBudget,
    ConversationContextPacker,
)
from sora_sandbox.conversation_runtime.contracts import (
    CancellationToken,
    TurnCancelled,
    TurnKind,
    TurnStatus,
    UnifiedDisclosureLevel,
)
from sora_sandbox.conversation_runtime.eval import (
    ConversationReleaseGate,
    run_golden_set,
    summarize_eval_results,
    validate_golden_set,
)
from sora_sandbox.conversation_runtime.journal import (
    ConversationJournal,
    InvalidTurnTransition,
    JournalConflict,
)
from sora_sandbox.conversation_runtime.post_turn import (
    AffectPostTurnConsumer,
    FunctionPostTurnConsumer,
    PostTurnDispatcher,
    RelationshipPostTurnConsumer,
    WorldOpsPostTurnConsumer,
)
from sora_sandbox.conversation_runtime.routing import (
    ModelBudgetExceeded,
    ModelRouteCoordinator,
    RoutedLLMClient,
)
from sora_sandbox.conversation_runtime.runtime import (
    ConversationRuntime,
    ConversationRuntimeConfig,
)
from sora_sandbox.conversation_runtime.security import (
    AtRestCipher,
    KeyMismatchError,
    StaticKeyProvider,
)
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.memory_store import init_memory_schema
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.state import seed_state


class ConversationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def journal(self) -> ConversationJournal:
        return ConversationJournal(
            self.root / "conversation.db",
            AtRestCipher(StaticKeyProvider(b"k" * 32)),
        )

    def runtime(self, llm=None) -> ConversationRuntime:
        journal = self.journal()
        routes = ModelRouteCoordinator(max_hot_path_calls=3)
        routed = RoutedLLMClient(llm or FakeLLMClient(), routes, journal=journal)
        runner = AgentRunner(
            routed,
            subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
            interruptibility=InterruptibilityService(),
        )
        return ConversationRuntime(
            runner,
            journal=journal,
            model_routes=routes,
            post_turn=PostTurnDispatcher(journal),
            config=ConversationRuntimeConfig(start_post_turn_worker=False),
        )

    def test_turn_input_is_encrypted_at_rest(self) -> None:
        journal = self.journal()
        try:
            context, created = journal.create_turn(
                conversation_id="c1",
                request_id="r1",
                user_input="highly-sensitive-plain-text",
            )
            self.assertTrue(created)
            self.assertEqual(journal.read_input(context.turn_id), "highly-sensitive-plain-text")
            connection = sqlite3.connect(journal.path)
            try:
                raw = str(
                    connection.execute("SELECT input_cipher FROM runtime_turn").fetchone()[0]
                )
            finally:
                connection.close()
            self.assertNotIn("highly-sensitive-plain-text", raw)
            self.assertTrue(raw.startswith("aes256gcm-v1:"))
        finally:
            journal.close()

    def test_request_idempotency_and_conflict_detection(self) -> None:
        journal = self.journal()
        try:
            first, created = journal.create_turn(
                conversation_id="c1", request_id="same", user_input="hello"
            )
            second, duplicate = journal.create_turn(
                conversation_id="c1", request_id="same", user_input="hello"
            )
            self.assertTrue(created)
            self.assertFalse(duplicate)
            self.assertEqual(first.turn_id, second.turn_id)
            with self.assertRaises(JournalConflict):
                journal.create_turn(
                    conversation_id="c1", request_id="same", user_input="different"
                )
        finally:
            journal.close()

    def test_state_machine_rejects_illegal_transition(self) -> None:
        journal = self.journal()
        try:
            context, _ = journal.create_turn(
                conversation_id="c1", request_id="r1", user_input="hello"
            )
            with self.assertRaises(InvalidTurnTransition):
                journal.transition(
                    context.turn_id, TurnStatus.COMMITTED, stage="skip_everything"
                )
        finally:
            journal.close()

    def test_atomic_commit_contains_history_result_and_outbox(self) -> None:
        journal = self.journal()
        try:
            context, _ = journal.create_turn(
                conversation_id="c1", request_id="r1", user_input="hello"
            )
            journal.transition(context.turn_id, TurnStatus.PREPARING, stage="prepare")
            journal.transition(context.turn_id, TurnStatus.GENERATING, stage="generate")
            journal.transition(context.turn_id, TurnStatus.COMMITTING, stage="commit")
            committed = journal.commit_turn(
                context.turn_id,
                user_input="hello",
                reply="hi",
                result={"reply": "hi"},
                outbox_events=(("conversation.turn_committed", {"reply": "hi"}),),
            )
            self.assertEqual(committed.status, TurnStatus.COMMITTED)
            self.assertEqual(journal.read_result(context.turn_id), {"reply": "hi"})
            self.assertEqual(
                [(item["role"], item["content"]) for item in journal.recent_turns("c1")],
                [("user", "hello"), ("sora", "hi")],
            )
            claimed = journal.claim_outbox(worker_id="test")
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["payload"], {"reply": "hi"})
        finally:
            journal.close()

    def test_outbox_consumer_is_idempotent_under_replay(self) -> None:
        journal = self.journal()
        calls: list[str] = []
        dispatcher = PostTurnDispatcher(journal)
        dispatcher.register(
            FunctionPostTurnConsumer(
                "counter", lambda event: calls.append(str(event["event_id"])) or {"ok": True}
            )
        )
        try:
            context, _ = journal.create_turn(
                conversation_id="c1", request_id="r1", user_input="hello"
            )
            journal.transition(context.turn_id, TurnStatus.PREPARING, stage="prepare")
            journal.transition(context.turn_id, TurnStatus.GENERATING, stage="generate")
            journal.transition(context.turn_id, TurnStatus.COMMITTING, stage="commit")
            journal.commit_turn(
                context.turn_id,
                user_input="hello",
                reply="hi",
                result={"reply": "hi"},
                outbox_events=(("conversation.turn_committed", {"reply": "hi"}),),
            )
            self.assertEqual(dispatcher.process_once()["delivered"], 1)
            with journal.transaction() as connection:
                connection.execute(
                    "UPDATE runtime_outbox SET status='pending', delivered_at_ms=NULL"
                )
            self.assertEqual(dispatcher.process_once()["delivered"], 1)
            self.assertEqual(len(calls), 1)
        finally:
            journal.close()

    def test_execution_lease_prevents_parallel_turns_in_same_conversation(self) -> None:
        journal = self.journal()
        try:
            first, _ = journal.create_turn(
                conversation_id="c1", request_id="r1", user_input="one"
            )
            second, _ = journal.create_turn(
                conversation_id="c1", request_id="r2", user_input="two"
            )
            self.assertTrue(journal.acquire_lease("c1", first.turn_id, "owner-a", ttl_ms=5000))
            self.assertFalse(journal.acquire_lease("c1", second.turn_id, "owner-b", ttl_ms=5000))
            self.assertTrue(journal.release_lease("c1", first.turn_id, "owner-a"))
            self.assertTrue(journal.acquire_lease("c1", second.turn_id, "owner-b", ttl_ms=5000))
        finally:
            journal.close()

    def test_legacy_plaintext_history_is_migrated_once_into_encrypted_store(self) -> None:
        journal = self.journal()
        turns = [
            {"role": "user", "content": "old secret", "at": 1},
            {"role": "sora", "content": "old reply", "at": 2},
        ]
        try:
            self.assertEqual(journal.import_legacy_history("c1", turns), 2)
            self.assertEqual(journal.import_legacy_history("c1", turns), 0)
            self.assertEqual(
                [(item["role"], item["content"]) for item in journal.recent_turns("c1")],
                [("user", "old secret"), ("sora", "old reply")],
            )
        finally:
            journal.close()

    def test_model_budget_reserves_response_slot_and_disables_fast_thinking(self) -> None:
        class CaptureLLM:
            def __init__(self):
                self.calls = []

            def chat(self, system, user, **kwargs):
                self.calls.append(dict(kwargs))
                return {"content": "{}", "usage": {}, "model": "capture"}

        delegate = CaptureLLM()
        routes = ModelRouteCoordinator(max_hot_path_calls=3)
        client = RoutedLLMClient(delegate, routes)
        with routes.activate("turn-1", CancellationToken()):
            client.chat("s", '{"task": "interpret_turn"}')
            client.chat("s", '{"task": "score_candidate_signals"}')
            with self.assertRaises(ModelBudgetExceeded):
                client.chat("s", '{"task": "resolve_interruptibility"}')
            client.chat("s", '{"context_pack": {}, "response": true}')
        self.assertEqual(delegate.calls[0]["thinking"], {"type": "disabled"})
        self.assertEqual(delegate.calls[1]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", delegate.calls[2])

    def test_runtime_e2e_and_request_replay(self) -> None:
        runtime = self.runtime()
        state = seed_state()
        try:
            result = runtime.run(state, "hello", request_id="stable-request")
            replay = runtime.run(state, "hello", request_id="stable-request")
            self.assertEqual(result["reply"], replay["reply"])
            self.assertTrue(replay["runtime"]["idempotent_replay"])
            self.assertEqual(result["correlation_id"], result["runtime"]["correlation_id"])
            self.assertLessEqual(len(result["runtime"]["model_calls"]), 3)
            self.assertEqual(
                result["runtime"]["model_calls"][-1]["task"], "response_generation"
            )
            event_types = [
                event.event_type
                for event in runtime.reconnect(result["runtime"]["turn_id"])
            ]
            self.assertEqual(event_types[0], "turn.accepted")
            self.assertIn("response.delta", event_types)
            self.assertEqual(event_types[-1], "response.completed")
            self.assertEqual(state["session"]["recent_turns"], [])
            self.assertNotIn("last_reply_summary", state["session"]["working_memory"])
            self.assertEqual(len(runtime.journal.recent_turns("local-sandbox")), 2)
        finally:
            runtime.close()

    def test_stream_turn_events_can_be_resumed(self) -> None:
        runtime = self.runtime()
        state = seed_state()
        try:
            events = list(
                runtime.stream_turn(state, "hello", request_id="stream-request")
            )
            resumed = runtime.reconnect(
                events[0].turn_id, after_sequence=events[1].sequence
            )
            self.assertTrue(events)
            self.assertTrue(all(
                event.sequence > events[1].sequence for event in resumed
            ))
            self.assertEqual(resumed[-1].event_type, "response.completed")
        finally:
            runtime.close()

    def test_explicit_quiet_commits_without_visible_response_delta(self) -> None:
        runtime = self.runtime()
        state = seed_state()
        try:
            result = runtime.run(state, "先别回复", request_id="quiet-request")
            events = runtime.reconnect(result["runtime"]["turn_id"])
            self.assertFalse(result["emitted"])
            self.assertEqual(result["reply"], "")
            self.assertNotIn("response.delta", [event.event_type for event in events])
            self.assertEqual(
                runtime.journal.get_turn(result["runtime"]["turn_id"]).status,
                TurnStatus.COMMITTED,
            )
        finally:
            runtime.close()

    def test_cancellation_reaches_streaming_provider_and_discards_state(self) -> None:
        class SlowLLM:
            def chat(self, system, user, **kwargs):
                return {"content": "{}", "usage": {}, "model": "slow"}

            def stream_chat(self, system, user, **kwargs):
                token = kwargs["cancellation_token"]
                for _ in range(100):
                    token.raise_if_cancelled()
                    time.sleep(0.01)
                    yield {"delta": "x", "usage": {}, "model": "slow"}

        runtime = self.runtime(SlowLLM())
        state = seed_state()
        before = copy.deepcopy(state)
        stream = runtime.stream_turn(state, "hello", request_id="cancel-request")
        first = next(stream)
        self.assertEqual(first.event_type, "turn.accepted")
        self.assertTrue(runtime.cancel("local-sandbox", reason="test_cancel"))
        with self.assertRaises(TurnCancelled):
            list(stream)
        try:
            self.assertEqual(state, before)
            self.assertEqual(
                runtime.journal.get_turn(first.turn_id).status, TurnStatus.CANCELLED
            )
        finally:
            runtime.close()

    def test_failed_runner_does_not_mutate_caller_state(self) -> None:
        class FaultRunner:
            llm = FakeLLMClient()
            subsystem_registry = None
            defer_domain_observation = False
            defer_post_turn_writes = False
            context_pack_transformer = None

            def run(self, state, user_input, *, correlation_id=None):
                state["session"]["turn_id"] = 999
                raise RuntimeError("injected")

        journal = self.journal()
        runtime = ConversationRuntime(
            FaultRunner(),
            journal=journal,
            model_routes=ModelRouteCoordinator(),
            post_turn=PostTurnDispatcher(journal),
            config=ConversationRuntimeConfig(start_post_turn_worker=False),
        )
        state = seed_state()
        before = copy.deepcopy(state)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                runtime.run(state, "hello", request_id="fault")
            self.assertEqual(state, before)
        finally:
            runtime.close()

    def test_context_packer_enforces_most_restrictive_disclosure(self) -> None:
        packed = ConversationContextPacker().pack(
            {
                "interruptibility_frame": {
                    "state_surface_permission": {
                        "memory": "silent",
                        "persona": "implicit",
                    }
                },
                "recollection_frame": {
                    "surface_action": "explicit_recall",
                    "retrieved_ids": ["m1"],
                    "memory_summaries": [{"id": "m1", "summary": "private"}],
                },
                "persona_frame": {"narrative_gist": "warm"},
                "session_context": {"recent_turns": []},
            }
        )
        self.assertEqual(packed.disclosure["memory"], UnifiedDisclosureLevel.SILENT.value)
        self.assertEqual(packed.context["recollection_frame"]["retrieved_ids"], [])
        self.assertEqual(packed.context["recollection_frame"]["memory_summaries"], [])

    def test_context_packer_excludes_relationship_optimization_risk_features(self) -> None:
        packed = ConversationContextPacker().pack(
            {
                "relationship_frame": {
                    "bond_stage": "close",
                    "stable_bands": {"trust": "high"},
                    "bond_bands": {
                        "dependency": "high",
                        "possessiveness": "medium",
                        "loyalty": "high",
                    },
                    "use_policy": {"surface": "implicit"},
                },
                "session_context": {"recent_turns": []},
                "recollection_frame": {},
            }
        )
        relationship = packed.context["relationship_frame"]
        self.assertEqual(relationship["bond_stage"], "close")
        self.assertNotIn("bond_bands", relationship)

    def test_generation_context_gate_runs_before_response_and_reserves_reply_tokens(self) -> None:
        class CaptureLLM(FakeLLMClient):
            def __init__(self):
                self.response_kwargs = None

            def stream_chat(self, system, user, **kwargs):
                if "context_pack" in user:
                    self.response_kwargs = dict(kwargs)
                yield from super().stream_chat(system, user, **kwargs)

        delegate = CaptureLLM()
        runtime = self.runtime(delegate)
        state = seed_state()
        try:
            result = runtime.run(state, "hello", request_id="gate-proof")
            gate = result["runtime"]["packed_context"]["generation_gate"]
            self.assertEqual(gate["schema_version"], "GenerationContextGate.v1")
            self.assertEqual(gate["reply_budget_tokens"], 1200)
            self.assertEqual(delegate.response_kwargs["max_tokens"], 1200)
            self.assertNotIn(GENERATION_CONTEXT_MARKER, result["context_pack"])
        finally:
            runtime.close()

    def test_context_packer_bounds_oversized_domain_frames(self) -> None:
        packer = ConversationContextPacker(
            ContextBudget(
                total_tokens=5000,
                response_reserve=800,
                system_reserve=800,
                recent_turns=500,
                memory=500,
                domain_frames=700,
                control_frames=1200,
            )
        )
        packed = packer.pack(
            {
                "session_context": {"recent_turns": []},
                "recollection_frame": {},
                "worldops_frame": {
                    "schema_version": "WorldOpsFrame.v1",
                    "allowed_facts": ["fact-" + ("x" * 200) for _ in range(80)],
                    "action_options": ["choice-" + ("y" * 200) for _ in range(80)],
                    "forbidden_behaviors": ["do-not-invent"],
                },
            }
        )
        self.assertLessEqual(packed.pools["domain_frames"], 700)
        self.assertLessEqual(packed.token_estimate, packer.budget.injectable_tokens)
        self.assertTrue(packed.dropped)

    def test_cipher_selects_key_by_ciphertext_key_id(self) -> None:
        first = AtRestCipher(StaticKeyProvider(b"a" * 32, key_id="key-v1"))
        ciphertext = first.encrypt_text("secret", associated_data="row:1")
        second = AtRestCipher(StaticKeyProvider(b"b" * 32, key_id="key-v2"))
        with self.assertRaisesRegex(KeyMismatchError, "key-v1"):
            second.decrypt_text(ciphertext, associated_data="row:1")

    def test_proactive_turn_entry_is_reserved(self) -> None:
        runtime = self.runtime()
        try:
            with self.assertRaisesRegex(NotImplementedError, "reserved"):
                runtime.run_proactive(seed_state(), {"type": "idle"})
        finally:
            runtime.close()

    def test_stream_cancellation_closes_iterator_and_records_audit_partial(self) -> None:
        journal = self.journal()
        token = CancellationToken()

        class ClosingStream:
            def __init__(self):
                self.index = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                self.index += 1
                if self.index == 1:
                    return {"delta": "partial", "usage": {}, "model": "capture"}
                token.cancel("stop")
                return {"delta": "ignored", "usage": {}, "model": "capture"}

            def close(self):
                self.closed = True

        stream = ClosingStream()

        class StreamingProvider:
            def chat(self, system, user, **kwargs):
                raise AssertionError("response path should stream")

            def stream_chat(self, system, user, **kwargs):
                return stream

        context, _ = journal.create_turn(
            conversation_id="c1", request_id="cancel-stream", user_input="hello"
        )
        client = RoutedLLMClient(
            StreamingProvider(), ModelRouteCoordinator(), journal=journal
        )
        try:
            routes = client.coordinator
            with routes.activate(context.turn_id, token):
                with self.assertRaises(TurnCancelled):
                    client.chat("system", '{"context_pack": {}, "response": true}')
            self.assertTrue(stream.closed)
            artifact = journal.artifacts(context.turn_id, "aborted_response")[0]
            self.assertEqual(artifact["payload"]["text"], "partial")
            self.assertFalse(artifact["payload"]["copy_allowed"])
        finally:
            journal.close()

    def test_outbox_continues_other_consumers_when_one_fails(self) -> None:
        journal = self.journal()
        successful_calls: list[str] = []
        failing_calls: list[str] = []
        dispatcher = PostTurnDispatcher(journal)

        def fail(event):
            failing_calls.append(str(event["event_id"]))
            raise RuntimeError("boom")

        dispatcher.register(
            FunctionPostTurnConsumer("failing", fail)
        )
        dispatcher.register(
            FunctionPostTurnConsumer(
                "successful",
                lambda event: successful_calls.append(str(event["event_id"])) or {"ok": True},
            )
        )
        try:
            context, _ = journal.create_turn(
                conversation_id="c1", request_id="outbox-failure", user_input="hello"
            )
            journal.transition(context.turn_id, TurnStatus.PREPARING, stage="prepare")
            journal.transition(context.turn_id, TurnStatus.GENERATING, stage="generate")
            journal.transition(context.turn_id, TurnStatus.COMMITTING, stage="commit")
            journal.commit_turn(
                context.turn_id,
                user_input="hello",
                reply="hi",
                result={"reply": "hi"},
                outbox_events=(("conversation.turn_committed", {"reply": "hi"}),),
            )
            self.assertEqual(dispatcher.process_once()["failed"], 1)
            self.assertEqual(len(successful_calls), 1)
            receipt = journal.consumer_receipt(
                "successful", successful_calls[0]
            )
            self.assertEqual(receipt["status"], "delivered")
            with journal.transaction() as connection:
                connection.execute(
                    "UPDATE runtime_outbox SET available_at_ms=0 WHERE status='pending'"
                )
            self.assertEqual(dispatcher.process_once()["failed"], 1)
            self.assertEqual(len(failing_calls), 2)
            self.assertEqual(len(successful_calls), 1)
        finally:
            journal.close()

    def test_eval_percentile_uses_nearest_rank(self) -> None:
        rows = [
            {
                "status": "passed",
                "latency_ms": latency,
                "request_idempotent": True,
                "control_compliant": True,
                "disclosure_compliant": True,
                "commit_atomic": True,
            }
            for latency in range(1, 21)
        ]
        self.assertEqual(summarize_eval_results(rows)["p95_latency_ms"], 19.0)

    def test_golden_harness_executes_real_runtime_trajectories(self) -> None:
        result = run_golden_set(
            lambda _case: self.runtime(),
            lambda _case: seed_state(),
            ROOT
            / "src"
            / "sora_sandbox"
            / "conversation_runtime"
            / "eval_data"
            / "golden_v1.jsonl",
            expectation_evaluator=lambda _case, observation: {
                "trajectory_executed": bool(observation.get("turn_ids"))
            },
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["case_count"], 9)
        self.assertTrue(all(row.get("observation") for row in result["results"]))

    def test_maintenance_purges_expired_dead_letters(self) -> None:
        journal = self.journal()
        try:
            context, _ = journal.create_turn(
                conversation_id="c1", request_id="dead-letter", user_input="hello"
            )
            journal.transition(context.turn_id, TurnStatus.PREPARING, stage="prepare")
            journal.transition(context.turn_id, TurnStatus.GENERATING, stage="generate")
            journal.transition(context.turn_id, TurnStatus.COMMITTING, stage="commit")
            journal.commit_turn(
                context.turn_id,
                user_input="hello",
                reply="hi",
                result={"reply": "hi"},
                outbox_events=(("conversation.turn_committed", {"reply": "hi"}),),
            )
            with journal.transaction() as connection:
                connection.execute(
                    "UPDATE runtime_outbox SET status='dead_letter', created_at_ms=1"
                )
            purged = journal.purge_expired_content(
                raw_content_before_ms=0,
                client_events_before_ms=0,
                dead_letter_before_ms=2,
            )
            self.assertEqual(purged["dead_letters_deleted"], 1)
            self.assertEqual(journal.health()["dead_letters"], 0)
        finally:
            journal.close()

    def test_release_decision_is_signed_and_tamper_evident(self) -> None:
        gate = ConversationReleaseGate(b"release-signing-key" * 2)
        metrics = {
            "e2e_success_rate": 1.0,
            "request_idempotency_rate": 1.0,
            "control_compliance_rate": 1.0,
            "context_disclosure_violation_rate": 0.0,
            "commit_atomicity_rate": 1.0,
            "p95_latency_ms": 100.0,
        }
        decision = gate.evaluate(
            metrics, dataset_version="golden_v1", policy_profile="default"
        )
        self.assertEqual(decision["status"], "passed")
        self.assertTrue(gate.verify(decision))
        decision["status"] = "blocked"
        self.assertFalse(gate.verify(decision))

    def test_golden_set_has_all_release_categories(self) -> None:
        result = validate_golden_set(
            ROOT
            / "src"
            / "sora_sandbox"
            / "conversation_runtime"
            / "eval_data"
            / "golden_v1.jsonl"
        )
        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["case_count"], 9)

    def test_legacy_memory_schema_adds_candidate_hash_before_index(self) -> None:
        connection = sqlite3.connect(self.root / "legacy-memory.db")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE memory_candidate (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                sensitivity TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                supersedes_id TEXT,
                explicit_user_request INTEGER NOT NULL,
                local_only INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                committed_memory_id TEXT
            )
            """
        )
        try:
            init_memory_schema(connection)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(memory_candidate)")
            }
            indexes = {
                row["name"]
                for row in connection.execute("PRAGMA index_list(memory_candidate)")
            }
            self.assertIn("candidate_hash", columns)
            self.assertIn("idx_memory_candidate_hash", indexes)
        finally:
            connection.close()

    def test_module10_change_scope_selects_only_conversation_runtime_suite(self) -> None:
        memory = MemoryRuntime(self.root / "quality-memory.db")
        try:
            memory.register_quality_eval_suite(
                "conversation_runtime",
                lambda: {
                    "status": "passed",
                    "total_cases": 1,
                    "passed_cases": 1,
                    "failed_cases": 0,
                    "summary": {},
                },
                source_module="text_conversation_runtime",
            )
            gate = memory.run_release_gate(change_scope=["text_runtime"])
            self.assertEqual(gate["suites"], ["conversation_runtime"])
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
