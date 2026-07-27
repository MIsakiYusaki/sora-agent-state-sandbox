from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..memory_projector import (
    PROJECTION_ACK_CONTRACT,
    PROJECTION_FRAME_CONTRACT,
    PROJECTION_SNAPSHOT_CONTRACT,
)
from ..memory_schema import JsonDict, new_id, utc_now
from ..paths import SEEDS_DIR, workspace_state_dir
from .compiler import CanonCompiler, Ed25519BundleSigner
from .contracts import (
    NARRATIVE_CONTRIBUTION_CONTRACT,
    WORLDOPS_PROJECTION_ADAPTER_VERSION,
    IdentityScope,
    WorldOpsContractError,
    content_hash,
    validate_event_envelope,
)
from .coordinator import WorldOpsWriteCoordinator
from .privacy import WorldOpsSecretVault
from .provider import WorldOpsInterpretationProvider
from .runtime import WorldOpsRuntime
from .server import CanonServerStore, ContentStudioService, WorldOpsServerAPI, deterministic_release_gate
from .store import WorldOpsStore


class WorldOpsDistributionClient(Protocol):
    def get_latest_release(
        self,
        world_id: str,
        *,
        channel: str,
        cohort: str,
        etag: str | None = None,
    ) -> JsonDict:
        ...

    def get_bundle(self, bundle_id: str, version: str) -> JsonDict:
        ...

    def post_contribution(self, proposal: Mapping[str, Any]) -> JsonDict:
        ...

    def get_contribution(self, proposal_id: str, *, receipt_token: str) -> JsonDict:
        ...

    def withdraw_contribution(self, proposal_id: str, *, withdrawal_token: str) -> JsonDict:
        ...


class WorldOpsProjectionAdapter:
    adapter_name = WORLDOPS_PROJECTION_ADAPTER_VERSION

    def __init__(self, system: "WorldOpsSystem") -> None:
        self.system = system

    def deliver(self, event: JsonDict) -> JsonDict:
        return self.system.write_coordinator.run(
            lambda: self._deliver(event),
            priority=1,
            name="worldops_projection_deliver",
        )

    def _deliver(self, event: JsonDict) -> JsonDict:
        if str(event.get("target_subsystem") or "") != "worldops":
            return {
                "contract": PROJECTION_ACK_CONTRACT,
                "event_id": event.get("event_id"),
                "target_subsystem": "worldops",
                "status": "rejected_policy",
                "error_type": "wrong_target_subsystem",
                "processed_at": utc_now(),
            }
        memory = dict(event.get("memory") or {})
        memory_id = str(memory.get("id") or "")
        action = str(event.get("action") or "upsert")
        if not memory_id:
            return {
                "contract": PROJECTION_ACK_CONTRACT,
                "event_id": event.get("event_id"),
                "target_subsystem": "worldops",
                "status": "rejected_policy",
                "error_type": "memory_id_missing",
                "processed_at": utc_now(),
            }
        if action not in {"upsert", "delete", "rollback", "forget", "tombstone"}:
            return {
                "contract": PROJECTION_ACK_CONTRACT,
                "event_id": event.get("event_id"),
                "target_subsystem": "worldops",
                "status": "rejected_policy",
                "error_type": "unsupported_projection_action",
                "processed_at": utc_now(),
            }
        if action == "upsert" and self.system.store.source_is_forgotten(memory_id):
            status = "rejected_policy"
            ingestion = {"status": "tombstone_prevents_relearning", "memory_id": memory_id}
        else:
            ingestion = self.system.store.store_memory_projection(event)
            status = "applied" if ingestion["status"] == "applied" else "applied"
            if action in {"delete", "rollback", "forget", "tombstone"}:
                self.system.store.forget_source(memory_id, reason_code=f"memory_{action}")
        snapshot = self.system.projection_snapshot(source_memory_ids=[memory_id], ingestion=ingestion)
        return {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": event["event_id"],
            "target_subsystem": "worldops",
            "status": status,
            "idempotency": event.get("idempotency", {}),
            "snapshot_ref": snapshot["snapshot_ref"],
            "snapshot_version": snapshot["snapshot_version"],
            "snapshot": snapshot,
            "proposal_requests": [],
            "warnings": [],
            "processed_at": utc_now(),
        }


class WorldOpsSystem:
    """Production boundary for public canon and one isolated personal timeline."""

    def __init__(
        self,
        *,
        store: WorldOpsStore,
        runtime: WorldOpsRuntime,
        signer: Ed25519BundleSigner | None,
        server_store: CanonServerStore | None,
        studio: ContentStudioService | None,
        distribution: WorldOpsDistributionClient,
        secret_vault: WorldOpsSecretVault,
        memory_runtime: Any | None = None,
        start_worker: bool = True,
        worker_interval_seconds: float = 0.5,
        deployment_mode: str = "remote_client",
        write_coordinator: WorldOpsWriteCoordinator | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.signer = signer
        self.server_store = server_store
        self.studio = studio
        self.distribution = distribution
        self.secret_vault = secret_vault
        self.memory_runtime = memory_runtime
        self.worker_interval_seconds = max(0.05, float(worker_interval_seconds))
        self.deployment_mode = deployment_mode
        self.write_coordinator = write_coordinator or WorldOpsWriteCoordinator()
        if deployment_mode == "remote_client" and signer is not None:
            raise WorldOpsContractError("production WorldOps clients must never hold a content signing key")
        if deployment_mode == "local_split_development" and (signer is None or studio is None or server_store is None):
            raise WorldOpsContractError("local split development requires its isolated content service")
        self.projection_adapter = WorldOpsProjectionAdapter(self)
        self._event_consumers: dict[str, Callable[[JsonDict], Any]] = {}
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._outbox_delivery_lock = threading.RLock()
        self._last_maintenance = 0.0
        self.bootstrap_default_content()
        if start_worker:
            self.start_worker()

    @classmethod
    def default(
        cls,
        *,
        provider: WorldOpsInterpretationProvider | None = None,
        memory_runtime: Any | None = None,
        identity: IdentityScope | None = None,
        start_worker: bool = True,
    ) -> "WorldOpsSystem":
        state_dir = workspace_state_dir()
        scope = identity or IdentityScope(
            tenant_id="local",
            user_id="default-user",
            sora_id="sora.default",
            timeline_id="timeline.default",
        )
        signer = Ed25519BundleSigner.load_or_create(
            state_dir / "worldops-signing-key.pem",
            key_id="worldops.local.1",
        )
        store = WorldOpsStore(state_dir / "worldops.db", identity=scope, world_id="world.default")
        server_store = CanonServerStore(state_dir / "worldops-server.db")
        compiler = CanonCompiler(signer)
        studio = ContentStudioService(server_store, compiler, release_gate=deterministic_release_gate)
        return cls(
            store=store,
            runtime=WorldOpsRuntime(store, provider=provider),
            signer=signer,
            server_store=server_store,
            studio=studio,
            distribution=WorldOpsServerAPI(server_store),
            secret_vault=WorldOpsSecretVault(state_dir / "worldops-capabilities.vault"),
            memory_runtime=memory_runtime,
            start_worker=start_worker,
            deployment_mode="local_split_development",
        )

    @classmethod
    def remote_client(
        cls,
        *,
        distribution: WorldOpsDistributionClient,
        trusted_keys: list[Mapping[str, Any]],
        provider: WorldOpsInterpretationProvider | None = None,
        memory_runtime: Any | None = None,
        identity: IdentityScope | None = None,
        store_path: str | Path | None = None,
        secret_vault_path: str | Path | None = None,
        start_worker: bool = True,
    ) -> "WorldOpsSystem":
        state_dir = workspace_state_dir()
        scope = identity or IdentityScope(
            tenant_id="default",
            user_id="default-user",
            sora_id="sora.default",
            timeline_id="timeline.default",
        )
        store = WorldOpsStore(store_path or state_dir / "worldops.db", identity=scope, world_id="world.default")
        for key in trusted_keys:
            if not key.get("valid_from"):
                raise WorldOpsContractError("production trust keys require an explicit valid_from timestamp")
            store.register_trusted_key(
                key_id=str(key["key_id"]),
                public_key=bytes(key["public_key"]),
                algorithm=str(key.get("algorithm") or "Ed25519"),
                valid_from=str(key["valid_from"]),
                valid_to=(str(key["valid_to"]) if key.get("valid_to") else None),
                metadata={"source": "production_trust_bootstrap"},
            )
        return cls(
            store=store,
            runtime=WorldOpsRuntime(store, provider=provider),
            signer=None,
            server_store=None,
            studio=None,
            distribution=distribution,
            secret_vault=WorldOpsSecretVault(secret_vault_path or state_dir / "worldops-capabilities.vault"),
            memory_runtime=memory_runtime,
            start_worker=start_worker,
            deployment_mode="remote_client",
        )

    def bootstrap_default_content(self) -> JsonDict:
        if self.signer is not None:
            self.store.register_trusted_key(
                key_id=self.signer.key_id,
                public_key=self.signer.public_key_bytes(),
                metadata={"source": "local_content_service", "trust_scope": "world.default"},
            )
        active = self.store.active_bundle()
        if active is not None:
            return {"status": "already_active", "manifest": active["manifest"]}
        latest = self.distribution.get_latest_release("world.default", channel="stable", cohort="default")
        if int(latest.get("status", 0)) != 200:
            if self.studio is None or self.server_store is None:
                return {"status": "unavailable", "reason": "no_signed_release_from_content_service"}
            source = json.loads((SEEDS_DIR / "worldops.seed.json").read_text(encoding="utf-8"))
            compiled = self.studio.compiler.compile(
                source,
                canon_version="1.0.0",
                release_sequence=1,
                channel="stable",
                cohort="default",
            )
            release = self.studio.create_release(
                compiled,
                role="canon_owner",
                actor_id="bootstrap.canon_owner",
                branch="bootstrap",
                baseline_id="worldops.bootstrap.v1",
                approvals=[
                    {"actor_id": "bootstrap.editor", "approved": True},
                    {"actor_id": "bootstrap.release_manager", "approved": True},
                ],
                migration_plan={"strategy": "initial_install", "from": None},
                rollback_plan={"strategy": "compensating_release", "retain_previous": True},
            )
            gate = deterministic_release_gate(compiled)
            self.server_store.promote_release(
                release["release_id"],
                target_status="stable",
                actor_id="bootstrap.release_manager",
                approval_token="bootstrap-local-service-token",
                gate_result=gate,
            )
            latest = self.distribution.get_latest_release("world.default", channel="stable", cohort="default")
        return self.refresh_from_server(latest=latest)

    def refresh_from_server(self, *, latest: Mapping[str, Any] | None = None) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self._refresh_from_server(latest=latest),
            priority=1,
            name="worldops_refresh_bundle",
            timeout=30.0,
        )

    def _refresh_from_server(self, *, latest: Mapping[str, Any] | None = None) -> JsonDict:
        response = dict(
            latest
            or self.distribution.get_latest_release("world.default", channel="stable", cohort="default")
        )
        if int(response.get("status", 0)) == 304:
            return {"status": "not_modified"}
        if int(response.get("status", 0)) != 200:
            return {"status": "unavailable", "server_status": response.get("status")}
        release = dict(response["release"])
        identity = self.store.identity_state()
        if int(release["release_sequence"]) <= int(identity["active_release_sequence"]):
            return {"status": "current", "release_sequence": int(identity["active_release_sequence"])}
        bundle_response = self.distribution.get_bundle(
            str(release["bundle_id"]), str(release["canon_version"])
        )
        if int(bundle_response.get("status", 0)) != 200:
            raise WorldOpsContractError("content service did not return the selected bundle")
        compiled = dict(bundle_response["content"])
        installed = self.store.install_bundle(compiled)
        activated = self.store.activate_bundle(
            str(installed["manifest"]["bundle_id"]), str(installed["manifest"]["canon_version"])
        )
        return {"status": "updated", "installed": installed, "activated": activated, "etag": response.get("etag")}

    def observe_user_turn(
        self,
        text: str,
        *,
        turn_id: str | int,
        correlation_id: str,
        turn_interpretation: Mapping[str, Any] | None = None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        persona_frame: Mapping[str, Any] | None = None,
        relationship_frame: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self.runtime.observe_turn(
                text,
                turn_id=turn_id,
                correlation_id=correlation_id,
                turn_interpretation=turn_interpretation,
                interruptibility_frame=interruptibility_frame,
                persona_frame=persona_frame,
                relationship_frame=relationship_frame,
            ),
            priority=1,
            name="worldops_observe_turn",
        )

    def frame(
        self,
        *,
        interruptibility_frame: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> JsonDict:
        return self.runtime.frame(
            interruptibility_frame=interruptibility_frame,
            correlation_id=correlation_id,
        )

    def start_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="sora-worldops-outbox",
                daemon=True,
            )
            self._worker.start()

    def stop_worker(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                raise RuntimeError("WorldOps outbox worker did not stop cleanly")
        self._worker = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self.process_outbox(limit=50)
                if time.monotonic() - self._last_maintenance >= 3600.0:
                    self.store.cleanup_observability()
                    self._last_maintenance = time.monotonic()
                if result["processed"] == 0:
                    self._stop_event.wait(self.worker_interval_seconds)
            except Exception as exc:
                self.store.record_trace(
                    "outbox_worker",
                    "failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                self._stop_event.wait(min(5.0, self.worker_interval_seconds * 4))

    def process_outbox(self, *, limit: int = 100) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self._process_outbox(limit=limit),
            priority=2,
            name="worldops_process_outbox",
            timeout=30.0,
        )

    def _process_outbox(self, *, limit: int = 100) -> JsonDict:
        with self._outbox_delivery_lock:
            processed = 0
            failed = 0
            results: list[JsonDict] = []
            for row in self.store.pending_outbox(limit=limit):
                envelope = dict(row["payload"])
                try:
                    validate_event_envelope(envelope)
                    outcome = self._deliver_outbox_event(envelope)
                    self.store.mark_outbox_processed(row["event_id"], consumer="worldops-system", result=outcome)
                    processed += 1
                    results.append({"event_id": row["event_id"], "status": "processed", "result": outcome})
                except Exception as exc:
                    failed += 1
                    retryable = not isinstance(exc, (WorldOpsContractError, ValueError, PermissionError))
                    state = self.store.mark_outbox_failed(
                        row["event_id"], f"{type(exc).__name__}: {exc}", retryable=retryable
                    )
                    results.append({"event_id": row["event_id"], "status": state["status"], "error": str(exc)})
            return {"processed": processed, "failed": failed, "results": results}

    def _run_consumer_effect(
        self,
        envelope: Mapping[str, Any],
        effect_key: str,
        action: Callable[[], JsonDict],
    ) -> JsonDict:
        source_event_id = str(envelope["event_id"])
        existing = self.store.consumer_effect(source_event_id, effect_key)
        if existing is not None and existing["status"] == "completed":
            return dict(existing["result"])
        self.store.start_consumer_effect(source_event_id, effect_key)
        try:
            result = action()
        except Exception as exc:
            self.store.fail_consumer_effect(
                source_event_id,
                effect_key,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        self.store.complete_consumer_effect(source_event_id, effect_key, result)
        return result

    def _deliver_outbox_event(self, envelope: JsonDict) -> JsonDict:
        event_type = str(envelope["event_type"])
        payload = dict(envelope["payload"])
        result: JsonDict = {"event_type": event_type}
        if event_type == "worldops.shared_episode.completed.v1":
            result["memory"] = self._run_consumer_effect(
                envelope,
                "memory.shared_episode",
                lambda: self._propose_shared_episode_memory(envelope),
            )
            result["contribution"] = self._run_consumer_effect(
                envelope,
                "canon.contribution",
                lambda: self._submit_eligible_contribution(envelope),
            )
        consumer = self._event_consumers.get(event_type)
        if consumer is not None:
            result["domain_consumer"] = self._run_consumer_effect(
                envelope,
                f"domain.{event_type}",
                lambda: self._deliver_registered_consumer(consumer, envelope),
            )
        return result

    @staticmethod
    def _deliver_registered_consumer(
        consumer: Callable[[JsonDict], Any],
        envelope: JsonDict,
    ) -> JsonDict:
        external = consumer(envelope)
        return external if isinstance(external, dict) else {"status": "delivered"}

    def _propose_shared_episode_memory(self, envelope: Mapping[str, Any]) -> JsonDict:
        if self.memory_runtime is None:
            return {"status": "not_configured"}
        payload = dict(envelope["payload"])
        instance = self.store.get_event_instance(str(payload["instance_id"]))
        event = self.store.event_definition(
            str(instance["event_id"]), definition_version=int(instance["definition_version"])
        ) or {}
        policy = dict(event.get("memory_policy") or {})
        if policy.get("write_only_if_user_participates", True):
            evidence = self.store.list_task_evidence(instance["instance_id"])
            if not any(item["accepted"] for item in evidence):
                return {"status": "skipped_no_user_participation"}
        summary = f"User and Sora completed the WorldOps event '{event.get('title') or instance['event_id']}'."
        transition_id = str(payload["transition_id"])
        existing_evidence = self.memory_runtime.find_evidence_by_source(
            source_type="worldops_transition",
            source_ref=transition_id,
        )
        if existing_evidence is None:
            evidence_id = self.memory_runtime.add_evidence(
                source_type="worldops_transition",
                source_ref=transition_id,
                content_summary=summary,
                raw_content=None,
                sensitivity="low",
                retention_policy="persistent",
                metadata={
                    "world_id": self.store.world_id,
                    "event_id": instance["event_id"],
                    "instance_id": instance["instance_id"],
                    "timeline_revision": payload["timeline_revision"],
                    "worldops_outbox_event_id": envelope["event_id"],
                    "raw_dialogue_stored": False,
                },
            )
        else:
            evidence_id = str(existing_evidence["id"])
        existing_candidate = self.memory_runtime.find_candidate_by_evidence(evidence_id)
        if existing_candidate is not None:
            return {
                "status": "already_proposed",
                "candidate": existing_candidate,
                "evidence_id": evidence_id,
            }
        candidate = self.memory_runtime.propose(
            summary=summary,
            memory_type="worldops_episode",
            evidence_ids=[evidence_id],
            tags=["worldops", str(instance["event_id"]), "shared_episode"],
            confidence=0.95,
            importance=0.72,
            stability=0.8,
            specificity=0.9,
            novelty=0.8,
            conflict_score=0.0,
            reason="worldops_completed_shared_episode",
            schema_version="WorldOpsMemoryCandidate.v1",
            trace={"source_event_id": envelope["event_id"], "correlation_id": envelope.get("correlation_id")},
            allow_auto_commit=False,
        )
        return {"status": "proposed", "candidate": candidate, "evidence_id": evidence_id}

    def _submit_eligible_contribution(self, envelope: Mapping[str, Any]) -> JsonDict:
        control = self.store.get_control()
        if not control["contribution_enabled"]:
            return {"status": "disabled"}
        payload = dict(envelope["payload"])
        instance = self.store.get_event_instance(str(payload["instance_id"]))
        event = self.store.event_definition(
            str(instance["event_id"]), definition_version=int(instance["definition_version"])
        ) or {}
        policy = dict(event.get("contribution_policy") or {})
        if not policy.get("eligible", False):
            return {"status": "ineligible"}
        proposal_id = "ncp." + content_hash(
            {"timeline": self.store.identity.timeline_id, "transition": payload["transition_id"]}
        )[:32]
        proposal = {
            "contract": NARRATIVE_CONTRIBUTION_CONTRACT,
            "proposal_id": proposal_id,
            "world_id": self.store.world_id,
            "pseudonymous_subject_id": self.store.identity.pseudonymous_subject_id,
            "source_event_id": instance["event_id"],
            "source_definition_version": int(instance["definition_version"]),
            "experience_summary": (
                f"An anonymous participant and Sora completed '{event.get('title') or instance['event_id']}' "
                f"through authored choice '{payload.get('choice_id') or instance.get('selected_choice_id') or 'task_completion'}'."
            ),
            "user_choice": {"choice_id": payload.get("choice_id") or instance.get("selected_choice_id")},
            "suggested_changes": [
                {
                    "operation": "editorial_review",
                    "target": instance["event_id"],
                    "claim": "Consider this typed outcome for future public canon.",
                }
            ],
            "affected_entities": list(event.get("entity_ids") or []),
            "privacy": {
                "mode": "anonymous",
                "raw_text_included": False,
                "attribution_allowed": False,
                "single_submission_opt_in": False,
            },
            "canon_conflicts": [],
            "novelty_score": 0.5,
            "promotion_recommendation": "editorial_review",
            "risk": {"raw_dialogue_present": False, "automatic_publication_allowed": False},
        }
        capabilities = self.secret_vault.get(proposal_id)
        if capabilities is None:
            capabilities = {
                "receipt_token": secrets.token_urlsafe(32),
                "withdrawal_token": secrets.token_urlsafe(32),
            }
            self.secret_vault.put(
                proposal_id,
                receipt_token=str(capabilities["receipt_token"]),
                withdrawal_token=str(capabilities["withdrawal_token"]),
            )
        proposal["_capability_tokens"] = dict(capabilities)
        response = self.distribution.post_contribution(proposal)
        if int(response.get("status", 0)) != 202:
            raise RuntimeError("content service rejected the contribution transport")
        receipt = dict(response["receipt"])
        receipt.pop("receipt_token", None)
        receipt.pop("withdrawal_token", None)
        receipt_token = str(capabilities["receipt_token"])
        withdrawal_token = str(capabilities["withdrawal_token"])
        self.store.save_contribution_receipt(
            receipt,
            receipt_token=receipt_token,
            withdrawal_token=withdrawal_token,
        )
        return {"status": "received", "proposal_id": proposal_id, "payload_hash": receipt.get("payload_hash")}

    def sync_contribution(self, proposal_id: str) -> JsonDict:
        secret = self.secret_vault.get(proposal_id)
        if secret is None:
            raise KeyError("contribution capability is unavailable")
        response = self.distribution.get_contribution(
            proposal_id, receipt_token=str(secret["receipt_token"])
        )
        contribution = dict(response["contribution"])
        self.store.save_contribution_receipt(
            contribution,
            receipt_token=str(secret["receipt_token"]),
            withdrawal_token=str(secret["withdrawal_token"]),
        )
        return contribution

    def withdraw_contribution(self, proposal_id: str) -> JsonDict:
        secret = self.secret_vault.get(proposal_id)
        if secret is None:
            raise KeyError("contribution capability is unavailable")
        response = self.distribution.withdraw_contribution(
            proposal_id, withdrawal_token=str(secret["withdrawal_token"])
        )
        result = dict(response["result"])
        if result.get("status") == "withdrawn":
            self.secret_vault.delete(proposal_id)
        return result

    def register_domain_event_consumer(self, event_type: str, consumer: Callable[[JsonDict], Any]) -> None:
        self._event_consumers[event_type] = consumer

    def apply_sync_snapshot(self, snapshot: Mapping[str, Any], *, transport_authenticated: bool) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self.store.apply_sync_snapshot(
                snapshot,
                transport_authenticated=transport_authenticated,
            ),
            priority=1,
            name="worldops_apply_sync_snapshot",
            timeout=30.0,
        )

    def fork_timeline(self, timeline_id: str, *, fork_revision: int | None = None) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self.store.fork_timeline(timeline_id, fork_revision=fork_revision),
            priority=0,
            name="worldops_fork_timeline",
            timeout=30.0,
        )

    def resolve_sync_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
        fork_timeline_id: str | None = None,
    ) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self.store.resolve_sync_conflict(
                conflict_id,
                resolution=resolution,
                fork_timeline_id=fork_timeline_id,
            ),
            priority=0,
            name="worldops_resolve_sync_conflict",
            timeout=30.0,
        )

    def projection_snapshot(
        self,
        *,
        source_memory_ids: list[str],
        ingestion: Mapping[str, Any],
    ) -> JsonDict:
        frame = self.frame()
        snapshot_version = f"{frame.get('bundle_id') or 'none'}:{frame.get('timeline_revision', 0)}"
        return {
            "contract": PROJECTION_SNAPSHOT_CONTRACT,
            "subsystem": "worldops",
            "snapshot_ref": f"worldops:{self.store.identity.timeline_id}",
            "snapshot_version": snapshot_version,
            "frame": {
                "contract": PROJECTION_FRAME_CONTRACT,
                "subsystem": "worldops",
                "status": "active",
                "snapshot_ref": f"worldops:{self.store.identity.timeline_id}",
                "snapshot_version": snapshot_version,
                "source_memory_ids": source_memory_ids,
                "state": frame,
            },
            "source_memory_ids": source_memory_ids,
            "updated_at": utc_now(),
            "adapter": WORLDOPS_PROJECTION_ADAPTER_VERSION,
            "metadata": {"ingestion": dict(ingestion)},
        }

    def migrate_legacy(self, legacy: Mapping[str, Any] | None) -> JsonDict:
        return self.write_coordinator.run(
            lambda: self._migrate_legacy(legacy),
            priority=2,
            name="worldops_migrate_legacy",
        )

    def _migrate_legacy(self, legacy: Mapping[str, Any] | None) -> JsonDict:
        payload = dict(legacy or {})
        active_event_id = str(payload.get("active_event_id") or "")
        if not active_event_id:
            return {"status": "nothing_to_migrate"}
        instance = self.store.event_instance_for(active_event_id)
        if instance is None:
            return {"status": "legacy_event_not_in_signed_bundle", "event_id": active_event_id}
        if instance["state"] == "dormant":
            self.runtime.invite(active_event_id, correlation_id="legacy-migration")
        return {"status": "migrated_as_invited", "event_id": active_event_id}

    def sync_state_cache(self, state: JsonDict) -> None:
        frame = self.frame()
        cache = state.setdefault("worldops", {})
        cache["runtime_mode"] = "canonical_worldops_system"
        cache["active_event_id"] = frame.get("selected_event_id")
        cache["canonical_frame"] = frame
        cache["content_pack"] = {
            "world_name": (self.store.active_bundle() or {"bundle": {"world": {}}})["bundle"]["world"].get("name"),
            "events": list((self.store.active_bundle() or {"bundle": {}})["bundle"].get("events") or []),
        }

    def reset(self) -> None:
        was_running = self._worker is not None and self._worker.is_alive()
        trusted_keys = self.store.trusted_keys(active_only=True)
        if was_running:
            self.stop_worker()
        self.store.reset()
        if self.signer is None:
            for key in trusted_keys:
                self.store.register_trusted_key(
                    key_id=str(key["key_id"]),
                    public_key=bytes(key["public_key"]),
                    algorithm=str(key["algorithm"]),
                    valid_from=str(key["valid_from"]),
                    valid_to=key.get("valid_to"),
                    metadata=dict(key.get("metadata") or {}),
                )
        if self.server_store is not None:
            self.server_store.reset()
        self.secret_vault.reset()
        self.bootstrap_default_content()
        if was_running:
            self.start_worker()

    def close(self) -> None:
        self.stop_worker()
        self.write_coordinator.close()
        if self.server_store is not None:
            self.server_store.close()
        self.store.close()

    def health(self) -> JsonDict:
        worker_alive = bool(self._worker and self._worker.is_alive())
        health = self.store.health()
        return health | {
            "worker": {"running": worker_alive, "interval_seconds": self.worker_interval_seconds},
            "write_coordinator": {
                "queue_depth": self.write_coordinator.queue_depth,
                "under_backpressure": self.write_coordinator.under_backpressure,
                "high_watermark": self.write_coordinator.high_watermark,
                "max_queue": self.write_coordinator.max_queue,
            },
            "deployment": self.deployment_mode,
        }

    def export_state(self) -> JsonDict:
        from .quality import worldops_slo_snapshot

        active = self.store.active_bundle()
        return {
            "schema_version": "WorldOpsSystemExport.v1",
            "exported_at": utc_now(),
            "identity": self.store.identity_state(),
            "timeline": self.store.timeline(),
            "active_manifest": (active or {}).get("manifest"),
            "events": self.store.list_event_instances(),
            "facts": self.store.active_facts(),
            "controls": self.store.get_control(),
            "contribution_receipts": self.store.list_contribution_receipts(limit=100),
            "memory_projections": self.store.list_memory_projections(active=True),
            "sync_conflicts": self.store.list_sync_conflicts(status=None, limit=100),
            "quality": self.store.quality_snapshot(),
            "health": self.health(),
            "slo": worldops_slo_snapshot(self),
            "recent_traces": self.store.list_traces(limit=100),
            "server_audit": self.server_store.list_audit(limit=100) if self.server_store is not None else [],
        }

    def register_quality_ops(self, memory_runtime: Any) -> None:
        from ..memory_quality_ops import AdminActionSpec
        from .quality import run_worldops_eval

        memory_runtime.register_quality_eval_suite(
            "worldops",
            lambda: run_worldops_eval(self),
            source_module="worldops_narrative_system",
        )
        memory_runtime.register_quality_export_section("worldops", self.export_state)
        actions = [
            AdminActionSpec(
                name="worldops_process_outbox",
                risk_level="medium",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_outbox", "memory_candidate", "narrative_contribution"], "limit": int(params.get("limit") or 100)}},
                execute=lambda params: self.process_outbox(limit=int(params.get("limit") or 100)),
                rollback_plan=lambda params: {"type": "outbox_idempotent_replay", "note": "Downstream writes use stable idempotency keys."},
                description="Process WorldOps domain events through asynchronous consumers.",
            ),
            AdminActionSpec(
                name="worldops_refresh_bundle",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_bundle_registry", "worldops_event_instance"], "signed_only": True}},
                execute=lambda params: self.refresh_from_server(),
                rollback_plan=lambda params: {"type": "signed_compensating_release", "note": "Downgrade is forbidden; publish a higher-sequence compensating bundle."},
                description="Fetch, verify, and atomically activate the latest signed stable content bundle.",
            ),
            AdminActionSpec(
                name="worldops_rebuild_verify",
                risk_level="medium",
                preview=lambda params: {"impact_summary": {"mutates": [], "dry_run": True}},
                execute=lambda params: self.store.rebuild(dry_run=True),
                rollback_plan=lambda params: {"type": "none", "note": "The operation is a read-only parity verification."},
                description="Replay the personal timeline ledger and verify deterministic parity.",
            ),
            AdminActionSpec(
                name="worldops_forget_source",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_forget_tombstone", "worldops_memory_projection"], "source_ref_present": bool(params.get("source_ref"))}},
                execute=lambda params: self.store.forget_source(str(params["source_ref"]), reason_code=str(params.get("reason") or "module10_admin_forget")),
                rollback_plan=lambda params: {"type": "not_automatically_reversible", "note": "Restoration requires explicit user consent and tombstone review."},
                description="Forget a WorldOps source projection and prevent replay from relearning it.",
            ),
            AdminActionSpec(
                name="worldops_resolve_dead_letter",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_dead_letter", "worldops_outbox"], "resolution": params.get("resolution")}},
                execute=lambda params: self.store.resolve_dead_letter(str(params["dead_letter_id"]), resolution=str(params["resolution"])),
                rollback_plan=lambda params: {"type": "manual_review", "note": "A discarded event requires explicit reconstruction from its audit export."},
                description="Requeue, discard, or mark a WorldOps dead letter as exported.",
            ),
            AdminActionSpec(
                name="worldops_cleanup_observability",
                risk_level="medium",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_trace", "worldops_metric", "worldops_outbox"], "canonical_ledger_untouched": True}},
                execute=lambda params: self.store.cleanup_observability(
                    trace_days=int(params.get("trace_days") or 30),
                    metric_days=int(params.get("metric_days") or 90),
                    processed_outbox_days=int(params.get("processed_outbox_days") or 30),
                ),
                rollback_plan=lambda params: {"type": "not_required", "note": "Only expired derived observability rows are removed; canonical ledgers remain intact."},
                description="Apply bounded retention to WorldOps traces, metrics, and processed outbox rows.",
            ),
            AdminActionSpec(
                name="worldops_fork_timeline",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["worldops_timeline", "worldops_ledgers"], "new_timeline_id": params.get("timeline_id"), "explicit_user_choice_required": True}},
                execute=lambda params: self.fork_timeline(
                    str(params["timeline_id"]),
                    fork_revision=(int(params["fork_revision"]) if params.get("fork_revision") is not None else None),
                ),
                rollback_plan=lambda params: {"type": "archive_fork", "note": "The source timeline is never mutated by a fork."},
                description="Create an explicit personal timeline branch at a selected revision.",
            ),
        ]
        for action in actions:
            memory_runtime.quality_ops.register_admin_action(action)
