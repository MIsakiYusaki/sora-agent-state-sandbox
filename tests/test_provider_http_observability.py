"""Fault-injection transport fixtures, never normal runtime providers."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sora_sandbox.conversation_runtime.contracts import TurnCancelled
from sora_sandbox.llm import DeepSeekClient
from sora_sandbox.memory_embedding import OpenAICompatibleEmbeddingProvider
from sora_sandbox.memory_extractor_provider import OpenAICompatibleExtractorProvider, noop_result
from sora_sandbox.memory_extractor_worker import AsyncMemoryExtractorWorker
from sora_sandbox.memory_job_observability import MemoryJobObserver
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_recall_planner import OpenAICompatibleRecallPlannerProvider
from sora_sandbox.memory_reranker import HTTPCrossEncoderReranker, OpenAICompatibleLLMReranker
from sora_sandbox.memory_surface_policy import OpenAICompatibleSurfacePolicyProvider
from sora_sandbox.model_provider import (
    EndpointPolicy, InMemorySecretStore, ModelCapabilities, ModelProviderSystem,
    ModelTarget, OwnerScope, ProfileStatus, ProviderKind, ProviderProtocol, ProviderRegistry,
    SecretMaterial, Workload,
)
from sora_sandbox.model_provider.errors import ModelProviderError
from sora_sandbox.turn_observability import TurnEvidence, capture_context, observe_call, observe_http


class ProviderHTTPObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def system(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addCleanup(client.close)
        system = ModelProviderSystem(
            ProviderRegistry(Path(self.temporary.name) / "providers.db"), InMemorySecretStore(),
            OwnerScope(tenant_id="fixture", user_id="fixture", agent_id="fixture"),
            endpoint_policy=EndpointPolicy(resolver=lambda *_: ("93.184.216.34",)),
            broker_client=client,
        )
        self.addCleanup(system.close)
        profile = system.create_profile(
            profile_id="fixture", display_name="Fixture", provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS, base_url="https://provider.example/v1",
            custom_endpoint=True,
        )
        system.bind_credential(profile.profile_id, SecretMaterial(api_key="private-secret-canary"))
        capabilities = ModelCapabilities(chat=True, streaming=True, structured_output=True, thinking_control=True)
        for model in ("primary", "fallback"):
            system.register_model(profile.profile_id, model, capabilities)
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
        system.bind_workload(Workload.TURN_INTERPRETATION,
                             ModelTarget(provider_profile_id=profile.profile_id, model_id="primary"),
                             fallbacks=(ModelTarget(provider_profile_id=profile.profile_id, model_id="fallback"),))
        return system, system.registry.get_profile(profile.profile_id)

    def assert_private(self, evidence):
        serialized = json.dumps(evidence.snapshot())
        for forbidden in ("private-", "provider.example", "Authorization", "api_key"):
            self.assertNotIn(forbidden, serialized)

    def test_fallback_has_two_actual_requests_with_same_logical_parent(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content)["model"])
            if len(requests) == 1:
                return httpx.Response(503, text="private-provider-error")
            return httpx.Response(200, json={"choices": [{"message": {"content": "private-result"}}]})
        system, _ = self.system(handler)
        evidence = TurnEvidence()
        with evidence.activate(), evidence.span("model.turn_interpretation"):
            result = system.llm.chat("private-system", "private-user", workload=Workload.TURN_INTERPRETATION)
        self.assertEqual(result["content"], "private-result")
        self.assertEqual(requests, ["primary", "fallback"])
        spans = evidence.snapshot()["spans"]
        self.assertEqual([s["name"] for s in spans[1:]], ["provider.http.json"] * 2)
        self.assertEqual([s["status"] for s in spans[1:]], ["failed", "completed"])
        self.assertTrue(all(s["parent_id"] == spans[0]["span_id"] and s["duration_ms"] >= 0 for s in spans[1:]))
        self.assert_private(evidence)

    def test_stream_yield_does_not_reparent_consumer_and_can_close_in_other_context(self):
        system, profile = self.system(lambda _: httpx.Response(200, text='data: {"private-key":"private-content"}\n\ndata: [DONE]\n\n'))
        evidence, other = TurnEvidence(), TurnEvidence()
        with evidence.activate(), evidence.span("model.response_generation"):
            original = capture_context()
            stream = system.broker.stream_sse(profile, "/chat/completions", {}, timeout_seconds=1)
            next(stream)
            self.assertEqual(capture_context(), original)
            observe_call("consumer.check", lambda: None)
            active = evidence.snapshot()["spans"][1]
            self.assertEqual(active["status"], "running")
            self.assertIsNone(active["duration_ms"])
            with other.activate():
                stream.close()
            self.assertEqual(capture_context(), original)
        spans = evidence.snapshot()["spans"]
        self.assertEqual(spans[1]["status"], "interrupted")
        self.assertEqual(spans[2]["parent_id"], spans[0]["span_id"])
        self.assertEqual(other.snapshot()["spans"], [])
        self.assert_private(evidence)

    def test_completed_and_broken_sse_have_truthful_status(self):
        system, profile = self.system(lambda _: httpx.Response(200, text='data: {"value":1}\n\ndata: [DONE]\n\n'))
        evidence = TurnEvidence()
        with evidence.activate():
            self.assertEqual(len(list(system.broker.stream_sse(profile, "/chat/completions", {}, timeout_seconds=1))), 1)
        self.assertEqual(evidence.snapshot()["spans"][0]["status"], "completed")
        with patch.object(system.broker.client, "stream", side_effect=httpx.ReadTimeout("private-timeout")):
            with evidence.activate(), self.assertRaises(ModelProviderError):
                list(system.broker.stream_sse(profile, "/chat/completions", {}, timeout_seconds=1))
        self.assertEqual(evidence.snapshot()["spans"][1]["status"], "failed")
        self.assert_private(evidence)

    def test_policy_rejection_is_not_counted_as_an_http_request(self):
        def forbidden(_):
            self.fail("request must not leave the broker")
        system, profile = self.system(forbidden)
        evidence = TurnEvidence()
        with evidence.activate(), self.assertRaises(ValueError):
            system.broker.post_json(profile, "https://private-bad-path", {}, timeout_seconds=1)
        self.assertEqual(evidence.snapshot()["spans"], [])

    def test_embedding_and_rerank_are_separate_request_types(self):
        system, profile = self.system(lambda _: httpx.Response(200, json={}))
        evidence = TurnEvidence()
        with evidence.activate():
            for path in ("/embeddings", "/rerank"):
                system.broker.post_json(profile, path, {"input": "private-text"}, timeout_seconds=1)
        self.assertEqual([s["name"] for s in evidence.snapshot()["spans"]],
                         ["provider.http.embeddings", "provider.http.rerank"])
        self.assert_private(evidence)

    def test_all_compatibility_http_exits_record_actual_reads(self):
        chat = {"choices": [{"message": {"content": "{}"}}]}
        cases = [
            ("compat_chat", lambda: DeepSeekClient("private-key").chat("private-system", "private-user"), chat),
            ("compat_extraction", lambda: OpenAICompatibleExtractorProvider("private-key")._post_chat_completion({}), chat),
            ("compat_recall_plan", lambda: OpenAICompatibleRecallPlannerProvider(api_key="private-key")._post_chat_completion({}), chat),
            ("compat_surface", lambda: OpenAICompatibleSurfacePolicyProvider("private-key")._post_chat_completion({}), chat),
            ("compat_embeddings", lambda: OpenAICompatibleEmbeddingProvider("private-key", "https://provider.example", "fixture").embed_documents(["private-input"]), {"data": [{"embedding": [1.0]}]}),
            ("compat_rerank", lambda: HTTPCrossEncoderReranker(endpoint="https://provider.example")._post_json({}), {}),
            ("compat_llm_rerank", lambda: OpenAICompatibleLLMReranker("private-key", "https://provider.example", "fixture")._chat_json({}), chat),
        ]
        for suffix, function, response in cases:
            with self.subTest(suffix=suffix):
                evidence = TurnEvidence()
                with evidence.activate(), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as opener:
                    function()
                opener.assert_called_once()
                spans = evidence.snapshot()["spans"]
                self.assertEqual(len(spans), 1)
                self.assertEqual(spans[0]["name"], "provider.http." + suffix)
                self.assertEqual(spans[0]["status"], "completed")
                self.assert_private(evidence)

    def test_compat_stream_close_and_transport_failure(self):
        evidence = TurnEvidence()
        response = io.BytesIO(b'data: {"choices":[{"delta":{"content":"private-result"}}]}\n\ndata: [DONE]\n\n')
        with evidence.activate(), patch("urllib.request.urlopen", return_value=response):
            stream = DeepSeekClient("private-key").stream_chat("private-system", "private-user")
            next(stream)
            stream.close()
        self.assertTrue(response.closed)
        self.assertEqual(evidence.snapshot()["spans"][0]["status"], "interrupted")
        with evidence.activate(), patch("urllib.request.urlopen", side_effect=urllib.error.URLError("private-error")):
            with self.assertRaises(RuntimeError):
                DeepSeekClient("private-key").chat("private-system", "private-user")
        self.assertEqual(evidence.snapshot()["spans"][1]["status"], "failed")
        self.assert_private(evidence)

    def test_repair_call_is_nested_and_no_request_is_invented(self):
        package = {"job": {"job_id": "fixture"}, "episode": {"episode_id": "fixture"}, "events": []}
        evidence = TurnEvidence(lane="memory_worker")
        responses = [io.BytesIO(json.dumps({"choices": [{"message": {"content": "invalid"}}]}).encode()),
                     io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())]
        with evidence.activate(), evidence.span("memory.extractor.provider"), patch("urllib.request.urlopen", side_effect=responses) as opener:
            OpenAICompatibleExtractorProvider("private-key").extract(package)
        self.assertEqual(opener.call_count, 2)
        spans = evidence.snapshot()["spans"]
        self.assertEqual([s["name"] for s in spans], ["memory.extractor.provider", "provider.http.compat_extraction", "memory.extractor.json_repair", "provider.http.compat_extraction"])
        self.assertEqual(spans[3]["parent_id"], spans[2]["span_id"])
        self.assert_private(evidence)

    def test_cancellation_and_disabled_or_bounded_observation(self):
        with observe_http("provider.http.json"):
            pass
        evidence = TurnEvidence(limit=1)
        with evidence.activate():
            with self.assertRaises(TurnCancelled), observe_http("provider.http.json"):
                raise TurnCancelled("private-cancel")
            with observe_http("provider.http.json"):
                pass
        self.assertEqual(evidence.snapshot()["spans"][0]["status"], "cancelled")
        self.assertEqual(evidence.snapshot()["dropped_count"], 1)
        self.assert_private(evidence)

    def test_http_spans_survive_memory_job_persistence_and_debug_bridge(self):
        from types import SimpleNamespace
        from sora_sandbox.conversation_gateway.debug_snapshot import _attach_memory_jobs
        runtime = MemoryRuntime(Path(self.temporary.name) / "memory.db", owner_user_id="owner", agent_id="agent")
        self.addCleanup(runtime.close)
        receipt = runtime.observe_committed_turn({
            "event_id": "event-http", "turn_id": "turn-http", "event_type": "conversation.turn_committed",
            "payload": {"runtime_turn_id": "turn-http", "conversation_id": "conversation-http",
                        "user_id": "owner", "agent_id": "agent", "correlation_id": "corr-http",
                        "user_input": "I prefer quiet weekend walks by the river. They help me relax after a busy week and I plan to keep this routine.",
                        "reply": "private-reply"},
        })
        package = runtime.build_extraction_input(receipt["job_id"])
        payload = {"choices": [{"message": {"content": json.dumps(noop_result(package, reason="fixture"))}}]}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            result = AsyncMemoryExtractorWorker(runtime, provider=OpenAICompatibleExtractorProvider("private-key")).run_once()
        self.assertEqual(result["status"], "completed")
        # Fresh observer proves the spans are read from durable attempts, not live state.
        persisted = MemoryJobObserver(runtime).snapshot("conversation-http", ["turn-http"])
        attempt = persisted["jobs"][0]["attempts"][0]
        self.assertIn("provider.http.compat_extraction", [s["name"] for s in attempt["execution"]["spans"]])
        timeline = {"turns": [{"turn_id": "turn-http", "correlation_id": "corr-http"}]}
        _attach_memory_jobs(timeline, SimpleNamespace(runtime=runtime), "conversation-http")
        self.assertEqual(timeline["turns"][0]["memory_jobs"]["jobs"][0]["attempts"][0], attempt)
        self.assertNotIn("private-", json.dumps(timeline))


if __name__ == "__main__":
    unittest.main()
