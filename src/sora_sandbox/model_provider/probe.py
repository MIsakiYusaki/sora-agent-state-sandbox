from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .adapters import AdapterRegistry
from .broker import CredentialedHTTPBroker
from .contracts import (
    ModelCapabilities,
    ModelDescriptor,
    ProfileStatus,
    ResolvedTarget,
    utc_now,
)
from .registry import ProviderRegistry


class CapabilityProbe:
    def __init__(
        self,
        registry: ProviderRegistry,
        broker: CredentialedHTTPBroker,
        *,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self.registry = registry
        self.broker = broker
        self.adapters = adapters or AdapterRegistry()

    def verify_chat_model(
        self,
        profile_id: str,
        model_id: str,
        *,
        thinking_control: bool = False,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        verify_streaming: bool = True,
    ) -> ModelDescriptor:
        probe_id = f"probe_{uuid.uuid4().hex}"
        profile = self.registry.set_profile_status(
            profile_id,
            ProfileStatus.VERIFYING,
            actor="capability_probe",
        )
        provisional = ModelDescriptor(
            provider_profile_id=profile.profile_id,
            model_id=model_id,
            capabilities=ModelCapabilities(
                chat=True,
                streaming=True,
                structured_output=True,
                thinking_control=thinking_control,
                cancellation=True,
                usage_reporting=True,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
            ),
            source="probe_provisional",
        )
        target = ResolvedTarget(profile=profile, model=provisional)
        try:
            adapter = self.adapters.get(profile.protocol)
            chat = getattr(adapter, "chat", None)
            if not callable(chat):
                raise TypeError("selected provider protocol does not implement chat")
            result = chat(
                self.broker,
                target,
                system="Return exactly one JSON object and no other text.",
                user='{"probe":"project_sora","expected":"ok"}',
                timeout_seconds=20.0,
                temperature=0.0,
                max_tokens=40,
                response_format={"type": "json_object"},
                thinking={"type": "disabled"} if thinking_control else None,
            )
            parsed = json.loads(str(result.get("content") or ""))
            structured = isinstance(parsed, dict)
            streaming = False
            if verify_streaming:
                stream = getattr(adapter, "stream_chat", None)
                if callable(stream):
                    for _event in stream(
                        self.broker,
                        target,
                        system="Reply with OK.",
                        user="OK",
                        timeout_seconds=20.0,
                        temperature=0.0,
                        max_tokens=8,
                    ):
                        streaming = True
                        break
            capabilities = provisional.capabilities.model_copy(
                update={
                    "structured_output": structured,
                    "streaming": streaming if verify_streaming else provisional.capabilities.streaming,
                    "usage_reporting": bool(result.get("usage")),
                }
            )
            descriptor = self.registry.upsert_model(
                provisional.model_copy(
                    update={
                        "capabilities": capabilities,
                        "source": "probe",
                        "verified_at": utc_now(),
                        "expires_at": (
                            datetime.now(timezone.utc) + timedelta(days=30)
                        ).isoformat(),
                    }
                ),
                actor="capability_probe",
            )
            self.registry.set_profile_status(
                profile_id,
                ProfileStatus.VERIFIED,
                actor="capability_probe",
            )
            self.registry.record_probe(
                probe_id=probe_id,
                profile_id=profile_id,
                model_id=model_id,
                probe_kind="chat_capabilities",
                status="passed",
                result={
                    "structured_output": capabilities.structured_output,
                    "streaming": capabilities.streaming,
                    "thinking_control": capabilities.thinking_control,
                    "usage_reporting": capabilities.usage_reporting,
                },
            )
            return descriptor
        except Exception as exc:
            self.registry.set_profile_status(
                profile_id,
                ProfileStatus.DEGRADED,
                actor="capability_probe",
                detail={"error_code": getattr(getattr(exc, "code", None), "value", type(exc).__name__)},
            )
            self.registry.record_probe(
                probe_id=probe_id,
                profile_id=profile_id,
                model_id=model_id,
                probe_kind="chat_capabilities",
                status="failed",
                result={
                    "error_code": getattr(getattr(exc, "code", None), "value", type(exc).__name__),
                    "retryable": bool(getattr(exc, "retryable", False)),
                },
            )
            raise

    def verify_embedding_model(
        self,
        profile_id: str,
        model_id: str,
        *,
        expected_dimension: int | None = None,
    ) -> ModelDescriptor:
        probe_id = f"probe_{uuid.uuid4().hex}"
        profile = self.registry.set_profile_status(
            profile_id, ProfileStatus.VERIFYING, actor="capability_probe"
        )
        try:
            adapter = self.adapters.get(profile.protocol)
            embed = getattr(adapter, "embed", None)
            if not callable(embed):
                raise TypeError("selected provider protocol does not implement embeddings")
            provisional_dimension = expected_dimension or 1
            provisional = ModelDescriptor(
                provider_profile_id=profile.profile_id,
                model_id=model_id,
                capabilities=ModelCapabilities(
                    embedding=True,
                    embedding_dimension=provisional_dimension,
                ),
                source="probe_expected" if expected_dimension else "probe_provisional",
            )
            target = ResolvedTarget(profile=profile, model=provisional)
            vectors = embed(
                self.broker,
                target,
                ["Project Sora capability probe"],
                timeout_seconds=20.0,
            )
            dimension = len(vectors[0]) if vectors and vectors[0] else 0
            if dimension < 1 or dimension > 65536:
                raise ValueError("embedding provider returned an invalid dimension")
            if expected_dimension and dimension != expected_dimension:
                raise ValueError("embedding dimension does not match the configured manifest")
            descriptor = self.registry.upsert_model(
                provisional.model_copy(
                    update={
                        "capabilities": ModelCapabilities(
                            embedding=True,
                            embedding_dimension=dimension,
                        ),
                        "source": "probe",
                        "verified_at": utc_now(),
                    }
                ),
                actor="capability_probe",
            )
            self.registry.set_profile_status(
                profile_id, ProfileStatus.VERIFIED, actor="capability_probe"
            )
            self.registry.record_probe(
                probe_id=probe_id,
                profile_id=profile_id,
                model_id=model_id,
                probe_kind="embedding_capabilities",
                status="passed",
                result={"embedding": True, "embedding_dimension": dimension},
            )
            return descriptor
        except Exception as exc:
            self._record_failed_probe(
                probe_id, profile_id, model_id, "embedding_capabilities", exc
            )
            raise

    def verify_rerank_model(self, profile_id: str, model_id: str) -> ModelDescriptor:
        probe_id = f"probe_{uuid.uuid4().hex}"
        profile = self.registry.set_profile_status(
            profile_id, ProfileStatus.VERIFYING, actor="capability_probe"
        )
        try:
            adapter = self.adapters.get(profile.protocol)
            rerank = getattr(adapter, "rerank", None)
            if not callable(rerank):
                raise TypeError("selected provider protocol does not implement reranking")
            provisional = ModelDescriptor(
                provider_profile_id=profile.profile_id,
                model_id=model_id,
                capabilities=ModelCapabilities(rerank=True),
                source="probe_provisional",
            )
            target = ResolvedTarget(profile=profile, model=provisional)
            payload = rerank(
                self.broker,
                target,
                query="Project Sora",
                documents=[{"id": "probe", "text": "Project Sora capability probe"}],
                timeout_seconds=20.0,
            )
            if not isinstance(payload, dict):
                raise ValueError("rerank provider returned an invalid response")
            descriptor = self.registry.upsert_model(
                provisional.model_copy(
                    update={"source": "probe", "verified_at": utc_now()}
                ),
                actor="capability_probe",
            )
            self.registry.set_profile_status(
                profile_id, ProfileStatus.VERIFIED, actor="capability_probe"
            )
            self.registry.record_probe(
                probe_id=probe_id,
                profile_id=profile_id,
                model_id=model_id,
                probe_kind="rerank_capabilities",
                status="passed",
                result={"rerank": True},
            )
            return descriptor
        except Exception as exc:
            self._record_failed_probe(
                probe_id, profile_id, model_id, "rerank_capabilities", exc
            )
            raise

    def _record_failed_probe(
        self,
        probe_id: str,
        profile_id: str,
        model_id: str,
        probe_kind: str,
        exc: Exception,
    ) -> None:
        error_code = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
        self.registry.set_profile_status(
            profile_id,
            ProfileStatus.DEGRADED,
            actor="capability_probe",
            detail={"error_code": error_code},
        )
        self.registry.record_probe(
            probe_id=probe_id,
            profile_id=profile_id,
            model_id=model_id,
            probe_kind=probe_kind,
            status="failed",
            result={
                "error_code": error_code,
                "retryable": bool(getattr(exc, "retryable", False)),
            },
        )
