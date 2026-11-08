from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.model_provider import (
    EndpointPolicy,
    EndpointPolicyError,
    InMemorySecretStore,
    ModelCapabilities,
    ModelProviderSystem,
    ModelTarget,
    OwnerScope,
    ProfileStatus,
    ProviderKind,
    ProviderProtocol,
    ProviderRegistry,
    SecretMaterial,
    Workload,
)
from sora_sandbox.model_provider.admin import (
    CreateProfileRequest,
    CredentialInput,
    DeleteProfileRequest,
    ModelProviderAdminService,
)
from sora_sandbox.model_provider.contracts import EndpointMode, ProviderEndpoint
from sora_sandbox.model_provider.adapters import GeminiNativeAdapter
from sora_sandbox.model_provider.errors import ModelProviderError, ProviderErrorCode
from sora_sandbox.memory_extractor_worker import AsyncMemoryExtractorWorker
from sora_sandbox.memory_runtime import MemoryRuntime


OWNER = OwnerScope(tenant_id="local", user_id="alice", agent_id="sora")
CHAT_CAPABILITIES = ModelCapabilities(
    chat=True,
    streaming=True,
    structured_output=True,
    thinking_control=True,
    cancellation=True,
    usage_reporting=True,
)


class ModelProviderSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.systems: list[ModelProviderSystem] = []

    def tearDown(self) -> None:
        for system in reversed(self.systems):
            system.close()
        self.temp_dir.cleanup()

    def _system(self, handler=None) -> ModelProviderSystem:
        client = None
        if handler is not None:
            client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        system = ModelProviderSystem(
            registry=ProviderRegistry(self.root / f"provider-{len(self.systems)}.db"),
            secret_store=InMemorySecretStore(),
            owner=OWNER,
            endpoint_policy=EndpointPolicy(resolver=lambda _host, _port: ("93.184.216.34",)),
            broker_client=client,
        )
        self.systems.append(system)
        return system

    def _configured_chat_system(self, handler) -> tuple[ModelProviderSystem, str]:
        system = self._system(handler)
        profile = system.create_profile(
            profile_id="pp_main",
            display_name="Primary",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://provider.example/v1",
            custom_endpoint=True,
        )
        system.bind_credential(profile.profile_id, SecretMaterial(api_key="sk-super-secret"))
        system.register_model(profile.profile_id, "fast-model", CHAT_CAPABILITIES)
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
        system.bind_workload(
            Workload.TURN_INTERPRETATION,
            ModelTarget(provider_profile_id=profile.profile_id, model_id="fast-model"),
        )
        return system, profile.profile_id

    def test_registry_and_exports_never_persist_secret_material(self) -> None:
        system, _ = self._configured_chat_system(lambda _request: httpx.Response(500))
        self.assertNotIn(b"sk-super-secret", system.registry.path.read_bytes())
        exported = json.dumps(system.export_redacted(), sort_keys=True)
        self.assertNotIn("sk-super-secret", exported)
        self.assertIn('"credential_ref": "bound"', exported)
        self.assertFalse(system.export_redacted()["secrets_included"])

    def test_secret_reference_is_bound_to_exact_profile(self) -> None:
        store = InMemorySecretStore()
        reference = store.put("pp_one", SecretMaterial(api_key="secret"))
        self.assertTrue(store.exists(reference, "pp_one"))
        self.assertFalse(store.exists(reference, "pp_two"))
        with self.assertRaises(ModelProviderError) as caught:
            store.use(reference, "pp_two", lambda material: material.api_key)
        self.assertEqual(caught.exception.code, ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            SecretMaterial(headers=(("Content-Type", "text/plain"),))

    def test_endpoint_policy(self) -> None:
        policy = EndpointPolicy(resolver=lambda _host, _port: ("93.184.216.34",))
        blocked = [
            ProviderEndpoint(base_url="http://api.example", mode=EndpointMode.REMOTE),
            ProviderEndpoint(base_url="https://127.0.0.1", mode=EndpointMode.REMOTE),
            ProviderEndpoint(base_url="http://10.0.0.5", mode=EndpointMode.LOCAL),
            ProviderEndpoint(base_url="https://user:pass@api.example", mode=EndpointMode.REMOTE),
        ]
        for endpoint in blocked:
            with self.subTest(endpoint=endpoint.base_url):
                with self.assertRaises(EndpointPolicyError):
                    policy.validate(endpoint)
        remote = policy.validate(
            ProviderEndpoint(base_url="https://api.example/v1", mode=EndpointMode.REMOTE)
        )
        local = policy.validate(
            ProviderEndpoint(base_url="http://127.0.0.1:11434/v1", mode=EndpointMode.LOCAL)
        )
        self.assertEqual(remote.resolved_addresses, ("93.184.216.34",))
        self.assertEqual(local.resolved_addresses, ("127.0.0.1",))

    def test_official_provider_host_requires_explicit_custom_endpoint(self) -> None:
        system = self._system()
        with self.assertRaisesRegex(ValueError, "canonical API host"):
            system.create_profile(
                profile_id="pp_disguised_deepseek",
                display_name="Disguised DeepSeek",
                provider_kind=ProviderKind.DEEPSEEK,
                protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                base_url="https://proxy.example/v1",
            )
        profile = system.create_profile(
            profile_id="pp_custom_deepseek",
            display_name="Custom DeepSeek gateway",
            provider_kind=ProviderKind.DEEPSEEK,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://proxy.example/v1",
            custom_endpoint=True,
        )
        self.assertTrue(profile.endpoint.custom)

    def test_relationship_tasks_route_without_changing_legacy_provider_source(self) -> None:
        system = self._system()
        for task in (
            "extract_relationship_evidence",
            "adjudicate_relationship_candidate_entailment_and_coverage",
            "adjudicate_explicit_romantic_intent",
            "repair_relationship_json",
        ):
            with self.subTest(task=task):
                self.assertEqual(
                    system.llm._workload(
                        None, json.dumps({"task": task}, separators=(",", ":"))
                    ),
                    Workload.RELATIONSHIP_EVIDENCE,
                )

    def test_binding_requires_capabilities_and_explicit_cross_provider_fallback(self) -> None:
        system = self._system()
        targets = []
        for profile_id in ("pp_primary", "pp_fallback"):
            profile = system.create_profile(
                profile_id=profile_id,
                display_name=profile_id,
                provider_kind=ProviderKind.OPENAI_COMPATIBLE,
                protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                base_url=f"https://{profile_id}.example/v1",
                custom_endpoint=True,
                auth_scheme="none",
            )
            system.register_model(profile.profile_id, "model", CHAT_CAPABILITIES)
            system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
            targets.append(ModelTarget(provider_profile_id=profile.profile_id, model_id="model"))
        with self.assertRaisesRegex(ValueError, "cross-provider"):
            system.bind_workload(
                Workload.RESPONSE_GENERATION, targets[0], fallbacks=(targets[1],)
            )
        binding = system.bind_workload(
            Workload.RESPONSE_GENERATION,
            targets[0],
            fallbacks=(targets[1],),
            cross_provider_fallback=True,
        )
        self.assertTrue(binding.cross_provider_fallback)

        weak = system.create_profile(
            profile_id="pp_weak",
            display_name="Weak",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://weak.example/v1",
            custom_endpoint=True,
            auth_scheme="none",
        )
        system.register_model(weak.profile_id, "weak", ModelCapabilities(chat=True))
        system.registry.set_profile_status(weak.profile_id, ProfileStatus.VERIFIED)
        with self.assertRaises(ModelProviderError) as caught:
            system.bind_workload(
                Workload.TURN_INTERPRETATION,
                ModelTarget(provider_profile_id=weak.profile_id, model_id="weak"),
            )
        self.assertEqual(caught.exception.code, ProviderErrorCode.CAPABILITY_MISMATCH)

    def test_compatible_setup_preserves_routes_and_never_binds_unsupported_workloads(self) -> None:
        system = self._system()
        profile = system.create_profile(
            profile_id="pp_auto_setup",
            display_name="Auto Setup",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://auto-setup.example/v1",
            custom_endpoint=True,
            auth_scheme="none",
        )
        system.register_model(profile.profile_id, "chat-model", CHAT_CAPABILITIES)
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
        system.bind_workload(
            Workload.DOMAIN_BACKGROUND,
            ModelTarget(provider_profile_id=profile.profile_id, model_id="chat-model"),
        )

        result = system.configure_compatible_workloads(profile.profile_id, "chat-model")

        self.assertTrue(result.ready_for_conversation)
        self.assertEqual(result.missing_conversation_workloads, ())
        self.assertIn(Workload.DOMAIN_BACKGROUND, result.preserved_workloads)
        self.assertNotIn(Workload.DOMAIN_BACKGROUND, result.bound_workloads)
        unsupported = {item.workload for item in result.unsupported_workloads}
        self.assertEqual(
            unsupported,
            {
                Workload.EMBEDDING_DOCUMENTS,
                Workload.EMBEDDING_QUERY,
                Workload.MEMORY_RERANK,
            },
        )
        health = system.health()
        self.assertTrue(health["conversation_ready"])
        self.assertEqual(health["missing_conversation_workloads"], [])

    def test_compatible_setup_reports_missing_conversation_routes(self) -> None:
        system = self._system()
        profile = system.create_profile(
            profile_id="pp_limited_setup",
            display_name="Limited Setup",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://limited-setup.example/v1",
            custom_endpoint=True,
            auth_scheme="none",
        )
        system.register_model(
            profile.profile_id,
            "chat-only",
            ModelCapabilities(chat=True),
        )
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)

        result = system.configure_compatible_workloads(profile.profile_id, "chat-only")

        self.assertFalse(result.ready_for_conversation)
        self.assertIn(Workload.RESPONSE_GENERATION, result.missing_conversation_workloads)
        self.assertEqual(
            set(result.bound_workloads),
            {Workload.MEMORY_CONSOLIDATION, Workload.DOMAIN_BACKGROUND},
        )
        self.assertFalse(system.health()["conversation_ready"])

    def test_turn_snapshot_is_stable_across_binding_change(self) -> None:
        system = self._system()
        profile = system.create_profile(
            profile_id="pp_snapshot",
            display_name="Snapshot",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://snapshot.example/v1",
            custom_endpoint=True,
            auth_scheme="none",
        )
        for model_id in ("model-a", "model-b"):
            system.register_model(profile.profile_id, model_id, CHAT_CAPABILITIES)
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
        first = system.bind_workload(
            Workload.TURN_INTERPRETATION,
            ModelTarget(provider_profile_id=profile.profile_id, model_id="model-a"),
        )
        with system.router.activate_turn("turn-1"):
            before = system.router.resolve(Workload.TURN_INTERPRETATION, turn_id="turn-1")
            system.bind_workload(
                Workload.TURN_INTERPRETATION,
                ModelTarget(provider_profile_id=profile.profile_id, model_id="model-b"),
                expected_revision=first.revision,
            )
            during = system.router.resolve(Workload.TURN_INTERPRETATION, turn_id="turn-1")
        after = system.router.resolve(Workload.TURN_INTERPRETATION)
        self.assertEqual(before.primary.model.model_id, "model-a")
        self.assertEqual(during.primary.model.model_id, "model-a")
        self.assertEqual(after.primary.model.model_id, "model-b")

    def test_chat_uses_brokered_secret_and_redacted_trace(self) -> None:
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["authorization"] = request.headers.get("Authorization", "")
            observed["url"] = str(request.url)
            return httpx.Response(
                200,
                headers={"x-request-id": "req-123"},
                json={
                    "model": "fast-model",
                    "choices": [{"message": {"content": '{"intent":"chat"}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                },
            )

        system, _ = self._configured_chat_system(handler)
        result = system.llm.chat(
            "system",
            '{"task": "interpret_turn"}',
            workload=Workload.TURN_INTERPRETATION,
            thinking={"type": "disabled"},
        )
        self.assertEqual(result["content"], '{"intent":"chat"}')
        self.assertEqual(observed["authorization"], "Bearer sk-super-secret")
        self.assertEqual(observed["url"], "https://provider.example/v1/chat/completions")
        row = system.registry._conn.execute(
            "SELECT * FROM provider_invocation_trace"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["prompt_tokens"], 10)
        self.assertNotIn("sk-super-secret", json.dumps(dict(row)))

    def test_invocation_usage_preserves_unknown_zero_and_aliases(self) -> None:
        for usage, expected in (
            ({}, (None, None)),
            ({"prompt_tokens": 0, "completion_tokens": 0}, (0, 0)),
            ({"input_tokens": 8, "output_tokens": 2}, (8, 2)),
            ({"prompt_tokens": 0, "input_tokens": 8,
              "completion_tokens": 0, "output_tokens": 2}, (0, 0)),
            ({"prompt_tokens": None, "output_tokens": 2}, (None, 2)),
        ):
            with self.subTest(usage=usage):
                system, _ = self._configured_chat_system(lambda _request: httpx.Response(
                    200, json={"model": "fast-model", "usage": usage,
                               "choices": [{"message": {"content": "ok"}}]},
                ))
                system.llm.chat("system", "user", workload=Workload.TURN_INTERPRETATION)
                row = system.registry._conn.execute(
                    "SELECT prompt_tokens, completion_tokens FROM provider_invocation_trace"
                ).fetchone()
                self.assertEqual(tuple(row), expected)

    def test_gemini_usage_does_not_invent_missing_counts(self) -> None:
        self.assertEqual(GeminiNativeAdapter._usage({}), {})
        self.assertEqual(GeminiNativeAdapter._usage({"usageMetadata": {
            "promptTokenCount": 0, "totalTokenCount": 5,
        }}), {"prompt_tokens": 0, "total_tokens": 5})

    def test_redirect_is_rejected_and_never_followed(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(307, headers={"location": "http://127.0.0.1/admin"})

        system, _ = self._configured_chat_system(handler)
        with self.assertRaises(ModelProviderError) as caught:
            system.llm.chat("system", "user", workload=Workload.TURN_INTERPRETATION)
        self.assertEqual(caught.exception.code, ProviderErrorCode.ENDPOINT_BLOCKED)
        self.assertEqual(requests, ["https://provider.example/v1/chat/completions"])

    def test_response_size_limit_is_enforced_while_streaming_body(self) -> None:
        system, _ = self._configured_chat_system(
            lambda _request: httpx.Response(200, content=b"x" * 2_048)
        )
        system.broker.max_response_bytes = 1_024
        with self.assertRaises(ModelProviderError) as caught:
            system.llm.chat(
                "system", "user", workload=Workload.TURN_INTERPRETATION
            )
        self.assertEqual(caught.exception.code, ProviderErrorCode.INVALID_RESPONSE)
        self.assertIn("size limit", str(caught.exception))

    def test_profile_delete_removes_binding_and_secret(self) -> None:
        system, profile_id = self._configured_chat_system(
            lambda _request: httpx.Response(500)
        )
        reference = system.registry.get_profile(profile_id).credential_ref
        system.delete_profile(profile_id)
        self.assertIsNone(
            system.registry.get_binding(
                OWNER, Workload.TURN_INTERPRETATION, required=False
            )
        )
        self.assertFalse(system.secret_store.exists(reference, profile_id))
        self.assertEqual(system.registry.list_profiles(OWNER), [])

    def test_admin_contract_never_echoes_secret_and_requires_delete_confirmation(self) -> None:
        system = self._system(lambda _request: httpx.Response(500))
        admin = ModelProviderAdminService(system)
        created = admin.create_profile(
            CreateProfileRequest(
                display_name="User Provider",
                provider_kind=ProviderKind.OPENAI_COMPATIBLE,
                protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                base_url="https://user-provider.example/v1",
                custom_endpoint=True,
            )
        )
        saved = admin.save_credential(
            created["profile_id"],
            CredentialInput(api_key="sk-user-owned"),
        )
        self.assertEqual(saved["credential_ref"], "bound")
        self.assertNotIn("sk-user-owned", json.dumps(saved))
        with self.assertRaises(ValueError):
            admin.delete_profile(
                created["profile_id"], DeleteProfileRequest(confirmation="delete")
            )
        deleted = admin.delete_profile(
            created["profile_id"],
            DeleteProfileRequest(confirmation=f"delete:{created['profile_id']}"),
        )
        self.assertEqual(deleted["status"], "deleted")

    def test_failed_credential_rotation_restores_previous_secret_and_status(self) -> None:
        system, profile_id = self._configured_chat_system(
            lambda _request: httpx.Response(401, json={"error": "invalid key"})
        )
        previous = system.registry.get_profile(profile_id)
        previous_ref = previous.credential_ref
        self.assertIsNotNone(previous_ref)

        with self.assertRaises(ModelProviderError):
            system.rotate_credential(
                profile_id,
                SecretMaterial(api_key="sk-invalid-rotated"),
                model_id="fast-model",
            )

        restored = system.registry.get_profile(profile_id)
        self.assertEqual(restored.credential_ref, previous_ref)
        self.assertEqual(restored.status, previous.status)
        self.assertEqual(
            system.secret_store.use(
                str(previous_ref), profile_id, lambda material: material.api_key
            ),
            "sk-super-secret",
        )
        self.assertEqual(len(system.secret_store._values), 1)

    def test_embedding_probe_derives_and_pins_dimension(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["input"], ["Project Sora capability probe"])
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
            )

        system = self._system(handler)
        profile = system.create_profile(
            profile_id="pp_embedding",
            display_name="Embedding",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_EMBEDDINGS,
            base_url="https://embedding.example/v1",
            custom_endpoint=True,
        )
        system.bind_credential(profile.profile_id, SecretMaterial(api_key="sk-embedding"))
        descriptor = system.probe.verify_embedding_model(profile.profile_id, "embed-model")
        self.assertTrue(descriptor.capabilities.embedding)
        self.assertEqual(descriptor.capabilities.embedding_dimension, 3)
        self.assertEqual(
            system.registry.get_profile(profile.profile_id).status,
            ProfileStatus.VERIFIED,
        )

    def test_memory_runtime_selects_provider_routes_for_configured_workloads(self) -> None:
        system, profile_id = self._configured_chat_system(
            lambda _request: httpx.Response(500)
        )
        target = ModelTarget(provider_profile_id=profile_id, model_id="fast-model")
        for workload in (
            Workload.MEMORY_EXTRACTION,
            Workload.MEMORY_RECALL_PLANNING,
            Workload.MEMORY_SURFACE,
        ):
            system.bind_workload(workload, target)
        runtime = MemoryRuntime(
            self.root / "memory.db",
            model_provider_system=system,
        )
        try:
            self.assertEqual(
                runtime.extractor_provider.__class__.__name__,
                "RoutedMemoryExtractorProvider",
            )
            self.assertEqual(
                runtime.recall_planner.llm_provider.__class__.__name__,
                "RoutedRecallPlannerProvider",
            )
            self.assertEqual(
                runtime.surface_policy.provider.__class__.__name__,
                "RoutedSurfacePolicyProvider",
            )
            worker = AsyncMemoryExtractorWorker(runtime)
            self.assertIs(worker.provider, runtime.extractor_provider)
        finally:
            runtime.close()

    def test_quality_eval_validates_routes_credentials_and_redaction(self) -> None:
        system, _ = self._configured_chat_system(lambda _request: httpx.Response(500))
        result = system.quality_eval()
        self.assertEqual(result["status"], "passed", result["cases"])
        self.assertEqual(
            {item["case_name"] for item in result["cases"]},
            {
                "registry_integrity",
                "credential_reference_integrity",
                "workload_binding_integrity",
                "redacted_export",
            },
        )

    def test_unconfigured_draft_profile_does_not_degrade_active_runtime(self) -> None:
        system = self._system()
        profile = system.create_profile(
            profile_id="pp_draft",
            display_name="Draft provider",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://draft.example/v1",
            custom_endpoint=True,
        )
        health = system.health()
        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["credential_health"][profile.profile_id])
        self.assertEqual(health["active_credential_failures"], [])
        self.assertEqual(system.quality_eval()["status"], "passed")

    def test_native_provider_adapters_use_correct_contracts_and_secret_headers(self) -> None:
        observations: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            observations.append(
                {
                    "host": request.url.host,
                    "path": request.url.path,
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            if request.url.host == "api.openai.com":
                return httpx.Response(
                    200,
                    json={
                        "model": "gpt-test",
                        "output_text": "openai-ok",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                )
            if request.url.host == "api.anthropic.com":
                return httpx.Response(
                    200,
                    json={
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "anthropic-ok"}],
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "gemini-ok"}]}}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 2,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 3,
                    },
                },
            )

        cases = [
            (
                ProviderKind.OPENAI,
                ProviderProtocol.OPENAI_RESPONSES,
                "https://api.openai.com/v1",
                "bearer",
                "gpt-test",
                "openai-ok",
            ),
            (
                ProviderKind.ANTHROPIC,
                ProviderProtocol.ANTHROPIC_MESSAGES,
                "https://api.anthropic.com",
                "anthropic_api_key",
                "claude-test",
                "anthropic-ok",
            ),
            (
                ProviderKind.GEMINI,
                ProviderProtocol.GEMINI_NATIVE,
                "https://generativelanguage.googleapis.com/v1beta",
                "google_api_key",
                "gemini-2.5-flash",
                "gemini-ok",
            ),
        ]
        for index, (kind, protocol, base_url, auth_scheme, model_id, expected) in enumerate(cases):
            with self.subTest(protocol=protocol.value):
                system = self._system(handler)
                profile = system.create_profile(
                    profile_id=f"pp_native_{index}",
                    display_name=protocol.value,
                    provider_kind=kind,
                    protocol=protocol,
                    base_url=base_url,
                    auth_scheme=auth_scheme,
                )
                system.bind_credential(
                    profile.profile_id, SecretMaterial(api_key=f"secret-{index}")
                )
                system.register_model(profile.profile_id, model_id, CHAT_CAPABILITIES)
                system.registry.set_profile_status(
                    profile.profile_id, ProfileStatus.VERIFIED
                )
                system.bind_workload(
                    Workload.DOMAIN_BACKGROUND,
                    ModelTarget(
                        provider_profile_id=profile.profile_id,
                        model_id=model_id,
                    ),
                )
                result = system.llm.chat(
                    "system", "user", workload=Workload.DOMAIN_BACKGROUND
                )
                self.assertEqual(result["content"], expected)

        openai, anthropic, gemini = observations
        self.assertEqual(openai["path"], "/v1/responses")
        self.assertEqual(openai["headers"]["authorization"], "Bearer secret-0")
        self.assertEqual(anthropic["path"], "/v1/messages")
        self.assertEqual(anthropic["headers"]["x-api-key"], "secret-1")
        self.assertEqual(anthropic["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(
            gemini["path"], "/v1beta/models/gemini-2.5-flash:generateContent"
        )
        self.assertEqual(gemini["headers"]["x-goog-api-key"], "secret-2")

    def test_provider_protocol_mismatch_is_rejected_before_persistence(self) -> None:
        system = self._system()
        with self.assertRaisesRegex(ValueError, "does not support protocol"):
            system.create_profile(
                profile_id="pp_wrong",
                display_name="Wrong",
                provider_kind=ProviderKind.ANTHROPIC,
                protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                base_url="https://api.anthropic.com",
                auth_scheme="anthropic_api_key",
            )

    def test_configured_client_requires_complete_runtime_routes(self) -> None:
        system = self._system()
        self.assertIsNone(system.configured_client())
        client = system.bootstrap_legacy_deepseek(
            api_key="sk-legacy",
            base_url="https://api.deepseek.com",
            model="deepseek-test",
        )
        self.assertIs(system.configured_client(), client)
        revision = system.registry.revision
        system.bootstrap_legacy_deepseek(
            api_key="sk-legacy",
            base_url="https://api.deepseek.com",
            model="deepseek-test",
        )
        self.assertEqual(system.registry.revision, revision)

    def test_legacy_migration_reuses_an_unconfigured_official_deepseek_profile(self) -> None:
        system = self._system()
        draft = system.create_profile(
            display_name="DeepSeek",
            provider_kind=ProviderKind.DEEPSEEK,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://api.deepseek.com",
        )

        system.bootstrap_legacy_deepseek(
            api_key="sk-legacy",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
        )

        profiles = system.registry.list_profiles(OWNER)
        self.assertEqual([profile.profile_id for profile in profiles], [draft.profile_id])
        migrated = profiles[0]
        self.assertEqual(migrated.status, ProfileStatus.VERIFIED)
        self.assertIsNotNone(migrated.credential_ref)
        self.assertEqual(
            system.registry.get_model(draft.profile_id, "deepseek-v4-flash").model_id,
            "deepseek-v4-flash",
        )

    def test_native_provider_streams_are_normalized(self) -> None:
        cases = [
            (
                ProviderKind.OPENAI,
                ProviderProtocol.OPENAI_RESPONSES,
                "https://api.openai.com/v1",
                "bearer",
                "gpt-stream",
                'data: {"type":"response.output_text.delta","delta":"openai"}\n\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
                "data: [DONE]\n\n",
                "openai",
            ),
            (
                ProviderKind.ANTHROPIC,
                ProviderProtocol.ANTHROPIC_MESSAGES,
                "https://api.anthropic.com",
                "anthropic_api_key",
                "claude-stream",
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"anthropic"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
                "data: [DONE]\n\n",
                "anthropic",
            ),
            (
                ProviderKind.GEMINI,
                ProviderProtocol.GEMINI_NATIVE,
                "https://generativelanguage.googleapis.com/v1beta",
                "google_api_key",
                "gemini-stream",
                'data: {"candidates":[{"content":{"parts":[{"text":"gemini"}]},"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":2}}\n\n'
                "data: [DONE]\n\n",
                "gemini",
            ),
        ]
        for index, (kind, protocol, base_url, auth_scheme, model_id, stream_body, expected) in enumerate(cases):
            with self.subTest(protocol=protocol.value):
                system = self._system(
                    lambda _request, body=stream_body: httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        text=body,
                    )
                )
                profile = system.create_profile(
                    profile_id=f"pp_stream_{index}",
                    display_name=protocol.value,
                    provider_kind=kind,
                    protocol=protocol,
                    base_url=base_url,
                    auth_scheme=auth_scheme,
                )
                system.bind_credential(
                    profile.profile_id, SecretMaterial(api_key=f"stream-secret-{index}")
                )
                system.register_model(profile.profile_id, model_id, CHAT_CAPABILITIES)
                system.registry.set_profile_status(
                    profile.profile_id, ProfileStatus.VERIFIED
                )
                system.bind_workload(
                    Workload.RESPONSE_GENERATION,
                    ModelTarget(
                        provider_profile_id=profile.profile_id,
                        model_id=model_id,
                    ),
                )
                events = list(
                    system.llm.stream_chat(
                        "system", "user", workload=Workload.RESPONSE_GENERATION
                    )
                )
                self.assertEqual("".join(str(item["delta"]) for item in events), expected)

    def test_stream_does_not_fallback_after_content_was_emitted(self) -> None:
        requested_models: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = str(json.loads(request.content)["model"])
            requested_models.append(model)
            if model == "model-a":
                body = (
                    'data: {"model":"model-a","choices":[{"delta":{"content":"partial"}}]}\n\n'
                    "data: not-json\n\n"
                )
                return httpx.Response(200, text=body)
            return httpx.Response(
                200,
                text='data: {"model":"model-b","choices":[{"delta":{"content":"fallback"}}]}\n\ndata: [DONE]\n\n',
            )

        system = self._system(handler)
        profile = system.create_profile(
            profile_id="pp_stream_fallback",
            display_name="Stream fallback",
            provider_kind=ProviderKind.OPENAI_COMPATIBLE,
            protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            base_url="https://stream.example/v1",
            custom_endpoint=True,
            auth_scheme="none",
        )
        for model_id in ("model-a", "model-b"):
            system.register_model(profile.profile_id, model_id, CHAT_CAPABILITIES)
        system.registry.set_profile_status(profile.profile_id, ProfileStatus.VERIFIED)
        system.bind_workload(
            Workload.RESPONSE_GENERATION,
            ModelTarget(provider_profile_id=profile.profile_id, model_id="model-a"),
            fallbacks=(
                ModelTarget(provider_profile_id=profile.profile_id, model_id="model-b"),
            ),
        )
        with self.assertRaises(ModelProviderError) as caught:
            list(
                system.llm.stream_chat(
                    "system", "user", workload=Workload.RESPONSE_GENERATION
                )
            )
        self.assertEqual(caught.exception.code, ProviderErrorCode.INVALID_RESPONSE)
        self.assertEqual(requested_models, ["model-a"])


if __name__ == "__main__":
    unittest.main()
