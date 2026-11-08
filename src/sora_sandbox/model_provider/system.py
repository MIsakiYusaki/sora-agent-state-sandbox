from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from ..paths import user_config_dir
from .adapters import AdapterRegistry
from .broker import CredentialedHTTPBroker
from .client import ProviderLLMClient
from .contracts import (
    Capability,
    CONVERSATION_REQUIRED_WORKLOADS,
    EndpointMode,
    ModelCapabilities,
    ModelDescriptor,
    ModelTarget,
    OwnerScope,
    ProfileStatus,
    ProviderRoutingSetupResult,
    ProviderEndpoint,
    ProviderKind,
    ProviderProfile,
    ProviderProtocol,
    Workload,
    WorkloadBinding,
    WorkloadCompatibility,
    WORKLOAD_REQUIREMENTS,
)
from .endpoint_policy import EndpointPolicy
from .probe import CapabilityProbe
from .registry import ProviderRegistry
from .router import WorkloadRouter
from .secrets import OSKeyringSecretStore, SecretMaterial, SecretStore


LEGACY_CHAT_WORKLOADS = CONVERSATION_REQUIRED_WORKLOADS


class ModelProviderSystem:
    def __init__(
        self,
        registry: ProviderRegistry,
        secret_store: SecretStore,
        owner: OwnerScope,
        *,
        endpoint_policy: EndpointPolicy | None = None,
        adapters: AdapterRegistry | None = None,
        allow_unverified_routes: bool = False,
        broker_client=None,
    ) -> None:
        self.registry = registry
        self.secret_store = secret_store
        self.owner = owner
        self.endpoint_policy = endpoint_policy or EndpointPolicy()
        self.adapters = adapters or AdapterRegistry()
        self.broker = CredentialedHTTPBroker(
            secret_store,
            endpoint_policy=self.endpoint_policy,
            client=broker_client,
        )
        self.router = WorkloadRouter(
            registry,
            owner,
            allow_unverified=allow_unverified_routes,
        )
        self.probe = CapabilityProbe(
            registry,
            self.broker,
            adapters=self.adapters,
        )
        self.llm = ProviderLLMClient(
            registry,
            self.router,
            self.broker,
            adapters=self.adapters,
        )
        self.llm.provider_system = self
        self._configuration_lock = threading.RLock()

    @classmethod
    def default(
        cls,
        owner: OwnerScope,
        *,
        registry_path: Path | None = None,
        secret_store: SecretStore | None = None,
        allow_unverified_routes: bool = False,
    ) -> "ModelProviderSystem":
        root = user_config_dir() / "model-provider"
        return cls(
            ProviderRegistry(registry_path or root / "provider.db"),
            secret_store or OSKeyringSecretStore(),
            owner,
            allow_unverified_routes=allow_unverified_routes,
        )

    def close(self) -> None:
        try:
            self.broker.close()
        finally:
            self.registry.close()

    def configured_client(
        self,
        *,
        required_workloads: Iterable[Workload] = LEGACY_CHAT_WORKLOADS,
    ) -> ProviderLLMClient | None:
        try:
            for workload in required_workloads:
                snapshot = self.router.resolve(workload)
                targets = (snapshot.primary, *snapshot.fallbacks)
                for target in targets:
                    profile = target.profile
                    if profile.auth_scheme == "none":
                        continue
                    if not profile.credential_ref or not self.secret_store.exists(
                        profile.credential_ref, profile.profile_id
                    ):
                        return None
        except Exception:
            return None
        return self.llm

    def create_profile(
        self,
        *,
        display_name: str,
        provider_kind: ProviderKind,
        protocol: ProviderProtocol,
        base_url: str,
        endpoint_mode: EndpointMode = EndpointMode.REMOTE,
        custom_endpoint: bool = False,
        auth_scheme: str = "bearer",
        profile_id: str | None = None,
    ) -> ProviderProfile:
        self._validate_provider_protocol(provider_kind, protocol)
        self._validate_provider_endpoint_declaration(
            provider_kind,
            base_url,
            endpoint_mode=endpoint_mode,
            custom_endpoint=custom_endpoint,
        )
        endpoint = ProviderEndpoint(
            base_url=base_url,
            mode=endpoint_mode,
            custom=custom_endpoint,
        )
        self.endpoint_policy.validate(endpoint, resolve_dns=False)
        return self.registry.create_profile(
            ProviderProfile(
                profile_id=profile_id or f"pp_{uuid.uuid4().hex}",
                owner=self.owner,
                display_name=display_name,
                provider_kind=provider_kind,
                protocol=protocol,
                endpoint=endpoint,
                auth_scheme=auth_scheme,
            ),
            actor="model_provider_system",
        )

    def bind_credential(
        self,
        profile_id: str,
        material: SecretMaterial,
        *,
        expected_policy_revision: int | None = None,
    ) -> ProviderProfile:
        profile = self.registry.get_profile(profile_id)
        if profile.credential_ref:
            raise ValueError("provider profile already has a credential; use credential rotation")
        secret_ref = self.secret_store.put(profile_id, material)
        try:
            return self.registry.bind_credential(
                profile_id,
                secret_ref,
                expected_policy_revision=expected_policy_revision,
                actor="user",
            )
        except Exception:
            self.secret_store.delete(secret_ref, profile_id)
            raise

    def rotate_credential(
        self,
        profile_id: str,
        material: SecretMaterial,
        *,
        model_id: str,
        thinking_control: bool = False,
        capability_kind: str = "chat",
        embedding_dimension: int | None = None,
    ) -> ProviderProfile:
        previous = self.registry.get_profile(profile_id)
        previous_ref = previous.credential_ref
        if not previous_ref:
            raise ValueError("provider profile has no credential to rotate; save a credential first")
        new_ref = self.secret_store.put(profile_id, material)
        try:
            self.registry.bind_credential(profile_id, new_ref, actor="credential_rotation")
            if capability_kind == "embedding":
                self.probe.verify_embedding_model(
                    profile_id,
                    model_id,
                    expected_dimension=embedding_dimension,
                )
            elif capability_kind == "rerank":
                self.probe.verify_rerank_model(profile_id, model_id)
            elif capability_kind == "chat":
                self.probe.verify_chat_model(
                    profile_id,
                    model_id,
                    thinking_control=thinking_control,
                )
            else:
                raise ValueError(f"unsupported capability kind: {capability_kind}")
        except Exception:
            if previous_ref:
                self.registry.bind_credential(profile_id, previous_ref, actor="credential_rotation_rollback")
                self.registry.set_profile_status(
                    profile_id,
                    previous.status,
                    actor="credential_rotation_rollback",
                )
            self.secret_store.delete(new_ref, profile_id)
            raise
        if previous_ref:
            self.secret_store.delete(previous_ref, profile_id)
        return self.registry.get_profile(profile_id)

    def register_model(
        self,
        profile_id: str,
        model_id: str,
        capabilities: ModelCapabilities,
        *,
        source: str = "catalog",
    ) -> ModelDescriptor:
        return self.registry.upsert_model(
            ModelDescriptor(
                provider_profile_id=profile_id,
                model_id=model_id,
                capabilities=capabilities,
                source=source,
            ),
            actor=source,
        )

    def bind_workload(
        self,
        workload: Workload,
        primary: ModelTarget,
        *,
        fallbacks: Iterable[ModelTarget] = (),
        requirements: Iterable[Capability] = (),
        cross_provider_fallback: bool = False,
        expected_revision: int | None = None,
    ) -> WorkloadBinding:
        with self._configuration_lock:
            existing = self.registry.get_binding(self.owner, workload, required=False)
            binding = WorkloadBinding(
                binding_id=existing.binding_id if existing else f"wb_{uuid.uuid4().hex}",
                owner=self.owner,
                workload=workload,
                primary=primary,
                fallbacks=tuple(fallbacks),
                requirements=tuple(requirements),
                cross_provider_fallback=cross_provider_fallback,
            )
            self.router.validate_binding(binding)
            return self.registry.put_binding(
                binding,
                expected_revision=expected_revision,
                actor="user",
            )

    def configure_compatible_workloads(
        self,
        profile_id: str,
        model_id: str,
    ) -> ProviderRoutingSetupResult:
        """Bind only routes the verified model can satisfy, preserving user choices."""

        with self._configuration_lock:
            model = self.registry.get_model(profile_id, model_id)
            existing = {
                binding.workload: binding
                for binding in self.registry.list_bindings(self.owner)
            }
            bound: list[Workload] = []
            preserved: list[Workload] = []
            unsupported: list[WorkloadCompatibility] = []
            target = ModelTarget(provider_profile_id=profile_id, model_id=model_id)
            for workload in Workload:
                requirements = WORKLOAD_REQUIREMENTS[workload]
                if workload in existing:
                    preserved.append(workload)
                    continue
                missing = tuple(
                    requirement
                    for requirement in requirements
                    if not model.capabilities.supports(requirement)
                )
                if missing:
                    unsupported.append(
                        WorkloadCompatibility(
                            workload=workload,
                            requirements=requirements,
                            missing_capabilities=missing,
                        )
                    )
                    continue
                self.bind_workload(
                    workload,
                    target,
                    requirements=requirements,
                )
                bound.append(workload)

            configured = {
                binding.workload
                for binding in self.registry.list_bindings(self.owner)
            }
            missing_conversation = tuple(
                workload
                for workload in CONVERSATION_REQUIRED_WORKLOADS
                if workload not in configured
            )
            return ProviderRoutingSetupResult(
                profile_id=profile_id,
                model=model,
                bound_workloads=tuple(bound),
                preserved_workloads=tuple(preserved),
                unsupported_workloads=tuple(unsupported),
                missing_conversation_workloads=missing_conversation,
                ready_for_conversation=not missing_conversation,
            )

    def delete_profile(self, profile_id: str) -> None:
        profile = self.registry.get_profile(profile_id)
        for binding in self.registry.list_bindings(self.owner):
            targets = (binding.primary, *binding.fallbacks)
            if any(target.provider_profile_id == profile_id for target in targets):
                self.registry.remove_binding(self.owner, binding.workload, actor="profile_delete")
        if profile.credential_ref:
            self.secret_store.delete(profile.credential_ref, profile_id)
        self.registry.delete_profile(profile_id, actor="user")

    def erase_owner_data(self) -> dict[str, int]:
        """Called after the owner's runtime has stopped, never during model work."""
        with self._configuration_lock:
            profiles = self.registry.list_profiles(self.owner, include_deleted=True)
            for profile in profiles:
                if profile.credential_ref:
                    erase = getattr(self.secret_store, "delete_if_present", None)
                    if not callable(erase):
                        raise ValueError("credential backend does not support resumable erasure")
                    erase(profile.credential_ref, profile.profile_id)
            return self.registry.purge_owner(self.owner)

    def bootstrap_legacy_deepseek(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        workloads: Iterable[Workload] = LEGACY_CHAT_WORKLOADS,
    ) -> ProviderLLMClient:
        identity = "\x1f".join(
            (
                self.owner.tenant_id,
                self.owner.user_id,
                self.owner.agent_id,
                base_url.rstrip("/"),
                model,
            )
        )
        profile_id = "pp_legacy_deepseek_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        profile = next(
            (
                candidate
                for candidate in self.registry.list_profiles(self.owner)
                if candidate.provider_kind == ProviderKind.DEEPSEEK
                and candidate.protocol == ProviderProtocol.OPENAI_CHAT_COMPLETIONS
                and candidate.endpoint.base_url.rstrip("/") == base_url.rstrip("/")
                and candidate.status == ProfileStatus.DRAFT
                and candidate.credential_ref is None
            ),
            None,
        )
        if profile is None:
            try:
                profile = self.registry.get_profile(profile_id)
            except Exception:
                profile = None
        if profile is None:
            profile = self.create_profile(
                profile_id=profile_id,
                display_name="DeepSeek（从旧版配置导入）",
                provider_kind=ProviderKind.DEEPSEEK,
                protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                base_url=base_url,
            )
        profile_id = profile.profile_id
        secret_ref = profile.credential_ref or f"cred_{hashlib.sha256((profile_id + ':api-key').encode()).hexdigest()[:32]}"
        self.secret_store.put(
            profile_id,
            SecretMaterial(api_key=api_key),
            secret_ref=secret_ref,
        )
        if profile.credential_ref != secret_ref:
            profile = self.registry.bind_credential(
                profile_id, secret_ref, actor="legacy_migration"
            )
        capabilities = ModelCapabilities(
            chat=True,
            streaming=True,
            structured_output=True,
            thinking_control=True,
            cancellation=True,
            usage_reporting=True,
            context_window=131072,
            max_output_tokens=8192,
        )
        try:
            descriptor = self.registry.get_model(profile_id, model)
        except Exception:
            descriptor = None
        if descriptor is None or descriptor.capabilities != capabilities:
            self.register_model(
                profile_id,
                model,
                capabilities,
                source="legacy_catalog",
            )
        if profile.status != ProfileStatus.VERIFIED:
            self.registry.set_profile_status(
                profile_id,
                ProfileStatus.VERIFIED,
                actor="legacy_migration",
            )
        target = ModelTarget(provider_profile_id=profile_id, model_id=model)
        for workload in workloads:
            existing = self.registry.get_binding(self.owner, workload, required=False)
            if existing is None:
                self.bind_workload(workload, target)
        return self.llm

    def export_redacted(self) -> dict:
        return self.registry.export_redacted(self.owner)

    def quality_eval(self) -> dict:
        cases: list[dict] = []
        health = self.registry.health()
        cases.append(
            {
                "case_name": "registry_integrity",
                "status": "passed" if health.get("foreign_keys") and health.get("journal_mode") == "wal" else "failed",
                "details": health,
            }
        )
        profiles = self.registry.list_profiles(self.owner)
        active_profile_ids = {
            target.provider_profile_id
            for binding in self.registry.list_bindings(self.owner)
            for target in (binding.primary, *binding.fallbacks)
        }
        credential_failures = [
            profile.profile_id
            for profile in profiles
            if profile.auth_scheme != "none"
            and (
                (
                    profile.credential_ref is not None
                    and not self.secret_store.exists(
                        profile.credential_ref, profile.profile_id
                    )
                )
                or (
                    profile.profile_id in active_profile_ids
                    and profile.credential_ref is None
                )
            )
        ]
        cases.append(
            {
                "case_name": "credential_reference_integrity",
                "status": "failed" if credential_failures else "passed",
                "details": {"failed_profile_ids": credential_failures},
            }
        )
        route_failures: list[dict] = []
        for binding in self.registry.list_bindings(self.owner):
            try:
                self.router.validate_binding(binding)
            except Exception as exc:
                route_failures.append(
                    {"workload": binding.workload.value, "error_code": getattr(getattr(exc, "code", None), "value", type(exc).__name__)}
                )
        cases.append(
            {
                "case_name": "workload_binding_integrity",
                "status": "failed" if route_failures else "passed",
                "details": {"failures": route_failures},
            }
        )
        exported = self.export_redacted()
        serialized = str(exported).lower()
        forbidden = [name for name in ("api_key", "authorization", "secret_material") if name in serialized]
        cases.append(
            {
                "case_name": "redacted_export",
                "status": "failed" if forbidden or exported.get("secrets_included") is not False else "passed",
                "details": {"forbidden_fields": forbidden, "secrets_included": exported.get("secrets_included")},
            }
        )
        return {
            "schema_version": "ModelProviderQualityEval.v1",
            "status": "passed" if all(item["status"] == "passed" for item in cases) else "failed",
            "cases": cases,
        }

    def register_quality_ops(self, memory_runtime) -> None:
        memory_runtime.register_quality_eval_suite(
            "model_provider",
            self.quality_eval,
            source_module="model_provider_system",
        )
        memory_runtime.register_quality_export_section(
            "model_provider",
            self.export_quality_report,
        )

    def export_quality_report(self) -> dict:
        return {
            "schema_version": "ModelProviderQualityReport.v1",
            "health": self.health(),
            "configuration": self.export_redacted(),
            "quality_eval": self.quality_eval(),
        }

    def maintain(self) -> dict[str, int]:
        return self.registry.maintain()

    def health(self) -> dict:
        profiles = self.registry.list_profiles(self.owner)
        bindings = self.registry.list_bindings(self.owner)
        active_profile_ids = {
            target.provider_profile_id
            for binding in bindings
            for target in (binding.primary, *binding.fallbacks)
        }
        credential_health = {
            profile.profile_id: (
                self.secret_store.exists(profile.credential_ref, profile.profile_id)
                if profile.credential_ref
                else profile.auth_scheme == "none"
            )
            for profile in profiles
        }
        active_credential_failures = sorted(
            profile_id
            for profile_id in active_profile_ids
            if not credential_health.get(profile_id, False)
        )
        registry_health = self.registry.health()
        configured_workloads = {binding.workload for binding in bindings}
        missing_conversation_workloads = [
            workload.value
            for workload in CONVERSATION_REQUIRED_WORKLOADS
            if workload not in configured_workloads
        ]
        healthy = (
            registry_health.get("status") == "ok"
            and not active_credential_failures
        )
        return {
            "status": "ok" if healthy else "degraded",
            "registry": registry_health,
            "credential_health": credential_health,
            "active_credential_failures": active_credential_failures,
            "binding_count": len(bindings),
            "missing_conversation_workloads": missing_conversation_workloads,
            "conversation_ready": healthy and not missing_conversation_workloads,
        }

    @staticmethod
    def _validate_provider_endpoint_declaration(
        provider_kind: ProviderKind,
        base_url: str,
        *,
        endpoint_mode: EndpointMode,
        custom_endpoint: bool,
    ) -> None:
        host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
        if endpoint_mode == EndpointMode.LOCAL:
            if provider_kind not in {ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}:
                raise ValueError("local endpoints require ollama or openai_compatible provider kind")
            return
        if provider_kind == ProviderKind.OLLAMA:
            raise ValueError("ollama provider profiles must use a local loopback endpoint")
        official_hosts = {
            ProviderKind.DEEPSEEK: {"api.deepseek.com"},
            ProviderKind.OPENAI: {"api.openai.com"},
            ProviderKind.ANTHROPIC: {"api.anthropic.com"},
            ProviderKind.GEMINI: {"generativelanguage.googleapis.com"},
        }
        expected = official_hosts.get(provider_kind)
        if expected is not None and not custom_endpoint and host not in expected:
            raise ValueError("official provider profile must use its canonical API host or be marked custom")
        if provider_kind in {ProviderKind.OPENAI_COMPATIBLE, ProviderKind.HTTP_RERANK} and not custom_endpoint:
            raise ValueError("remote compatible provider endpoints must be explicitly marked custom")

    @staticmethod
    def _validate_provider_protocol(
        provider_kind: ProviderKind,
        protocol: ProviderProtocol,
    ) -> None:
        allowed = {
            ProviderKind.DEEPSEEK: {ProviderProtocol.OPENAI_CHAT_COMPLETIONS},
            ProviderKind.OPENAI: {
                ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                ProviderProtocol.OPENAI_RESPONSES,
                ProviderProtocol.OPENAI_EMBEDDINGS,
            },
            ProviderKind.ANTHROPIC: {ProviderProtocol.ANTHROPIC_MESSAGES},
            ProviderKind.GEMINI: {ProviderProtocol.GEMINI_NATIVE},
            ProviderKind.OPENAI_COMPATIBLE: {
                ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                ProviderProtocol.OPENAI_EMBEDDINGS,
            },
            ProviderKind.OLLAMA: {
                ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
                ProviderProtocol.OPENAI_EMBEDDINGS,
            },
            ProviderKind.HTTP_RERANK: {ProviderProtocol.HTTP_RERANK},
        }
        supported = allowed.get(provider_kind)
        if supported is not None and protocol not in supported:
            raise ValueError(
                f"provider kind {provider_kind.value} does not support protocol {protocol.value}"
            )
