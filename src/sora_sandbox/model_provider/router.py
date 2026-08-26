from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .contracts import (
    Capability,
    OwnerScope,
    ProfileStatus,
    ResolvedTarget,
    RoutingSnapshot,
    WORKLOAD_REQUIREMENTS,
    Workload,
    WorkloadBinding,
)
from .errors import ModelProviderError, ProviderErrorCode
from .registry import ProviderRegistry


@dataclass(frozen=True)
class _TurnRoutes:
    turn_id: str
    registry_revision: int
    snapshots: dict[Workload, RoutingSnapshot]
    errors: dict[Workload, ModelProviderError]


_ACTIVE_ROUTES: contextvars.ContextVar[_TurnRoutes | None] = contextvars.ContextVar(
    "sora_model_provider_routes", default=None
)


class WorkloadRouter:
    def __init__(
        self,
        registry: ProviderRegistry,
        owner: OwnerScope,
        *,
        allow_unverified: bool = False,
    ) -> None:
        self.registry = registry
        self.owner = owner
        self.allow_unverified = allow_unverified

    def has_binding(self, workload: Workload | str) -> bool:
        normalized = Workload(workload)
        return self.registry.get_binding(self.owner, normalized, required=False) is not None

    @contextmanager
    def activate_turn(self, turn_id: str) -> Iterator[dict[Workload, RoutingSnapshot]]:
        existing = _ACTIVE_ROUTES.get()
        if existing is not None and existing.turn_id == turn_id:
            yield existing.snapshots
            return
        revision = self.registry.revision
        snapshots: dict[Workload, RoutingSnapshot] = {}
        errors: dict[Workload, ModelProviderError] = {}
        for binding in self.registry.list_bindings(self.owner):
            try:
                snapshots[binding.workload] = self.validate_binding(
                    binding,
                    registry_revision=revision,
                    turn_id=turn_id,
                )
            except ModelProviderError as exc:
                errors[binding.workload] = exc
        token = _ACTIVE_ROUTES.set(
            _TurnRoutes(
                turn_id=turn_id,
                registry_revision=revision,
                snapshots=snapshots,
                errors=errors,
            )
        )
        try:
            yield snapshots
        finally:
            _ACTIVE_ROUTES.reset(token)

    def resolve(self, workload: Workload | str, *, turn_id: str | None = None) -> RoutingSnapshot:
        normalized = Workload(workload)
        active = _ACTIVE_ROUTES.get()
        if active is not None and (turn_id is None or active.turn_id == turn_id):
            if normalized in active.errors:
                raise active.errors[normalized]
            snapshot = active.snapshots.get(normalized)
            if snapshot is None:
                raise ModelProviderError(
                    ProviderErrorCode.BINDING_NOT_FOUND,
                    f"no provider binding exists for workload: {normalized.value}",
                )
            return snapshot
        binding = self.registry.get_binding(self.owner, normalized)
        assert binding is not None
        return self.validate_binding(
            binding,
            registry_revision=self.registry.revision,
            turn_id=turn_id,
        )

    def validate_binding(
        self,
        binding: WorkloadBinding,
        *,
        registry_revision: int | None = None,
        turn_id: str | None = None,
    ) -> RoutingSnapshot:
        if binding.owner != self.owner:
            raise ModelProviderError(
                ProviderErrorCode.PROFILE_NOT_FOUND,
                "workload binding belongs to a different owner scope",
            )
        requirements = tuple(dict.fromkeys((*WORKLOAD_REQUIREMENTS[binding.workload], *binding.requirements)))
        primary = self._resolve_target(binding.primary.provider_profile_id, binding.primary.model_id, requirements)
        fallbacks = tuple(
            self._resolve_target(target.provider_profile_id, target.model_id, requirements)
            for target in binding.fallbacks
        )
        if not binding.cross_provider_fallback and any(
            item.profile.profile_id != primary.profile.profile_id for item in fallbacks
        ):
            raise ModelProviderError(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "cross-provider fallback is not authorized",
            )
        return RoutingSnapshot(
            snapshot_id=f"route_{uuid.uuid4().hex}",
            turn_id=turn_id,
            owner=self.owner,
            workload=binding.workload,
            registry_revision=(self.registry.revision if registry_revision is None else registry_revision),
            binding_revision=binding.revision,
            primary=primary,
            fallbacks=fallbacks,
            cross_provider_fallback=binding.cross_provider_fallback,
        )

    def _resolve_target(
        self,
        profile_id: str,
        model_id: str,
        requirements: tuple[Capability, ...],
    ) -> ResolvedTarget:
        profile = self.registry.get_profile(profile_id)
        if profile.owner != self.owner:
            raise ModelProviderError(
                ProviderErrorCode.PROFILE_NOT_FOUND,
                "provider profile belongs to a different owner scope",
            )
        ready = {ProfileStatus.VERIFIED}
        if self.allow_unverified:
            ready.update({ProfileStatus.CREDENTIAL_BOUND, ProfileStatus.DEGRADED})
        if profile.status not in ready:
            raise ModelProviderError(
                ProviderErrorCode.PROFILE_NOT_READY,
                f"provider profile is not ready: {profile.status.value}",
            )
        if profile.auth_scheme != "none" and not profile.credential_ref:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "provider profile has no credential binding",
            )
        model = self.registry.get_model(profile.profile_id, model_id)
        missing = [item.value for item in requirements if not model.capabilities.supports(item)]
        if missing:
            raise ModelProviderError(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                f"model {model_id} lacks required capabilities: {', '.join(missing)}",
            )
        return ResolvedTarget(profile=profile, model=model)
