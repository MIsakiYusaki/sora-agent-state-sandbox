from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..memory_schema import json_dumps, json_loads, new_id
from .compiler import Ed25519BundleVerifier, verify_compiled_bundle
from .contracts import (
    EVENT_STATES,
    IdentityScope,
    JsonDict,
    WorldBundleManifest,
    WorldOpsConflictError,
    WorldOpsContractError,
    WorldOpsSecurityError,
    WORLDOPS_SYNC_SNAPSHOT_CONTRACT,
    canonical_json,
    content_hash,
    make_event_envelope,
    utc_now,
)
from .effects import DEFAULT_EFFECT_REGISTRY, EffectRegistry
from .observability import TraceSink, prometheus_text
from .schema import WORLDOPS_CLIENT_SCHEMA_SQL


TRANSITION_TRIGGER_ALIASES = {
    "manual_eligibility": "eligible",
    "user_reference": "surface_selected",
}


class WorldOpsStore:
    """Canonical client store for one isolated personal timeline."""

    def __init__(
        self,
        path: str | Path,
        *,
        identity: IdentityScope,
        world_id: str = "world.default",
        accepted_channel: str = "stable",
        accepted_cohort: str = "default",
        effect_registry: EffectRegistry | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        identity.validate()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.world_id = world_id
        self.accepted_channel = accepted_channel
        self.accepted_cohort = accepted_cohort
        self.effect_registry = effect_registry or DEFAULT_EFFECT_REGISTRY
        self.trace_sink = trace_sink
        self._lock = threading.RLock()
        self._read_local = threading.local()
        self._read_connections: list[sqlite3.Connection] = []
        self._read_connections_lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure(self._conn, query_only=False)
        self._conn.executescript(WORLDOPS_CLIENT_SCHEMA_SQL)
        self._migrate_schema()
        self._conn.commit()
        self._ensure_identity()

    def _migrate_schema(self) -> None:
        """Apply additive migrations for databases created by earlier WorldOps builds."""
        transition_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(worldops_transition_ledger)").fetchall()
        }
        if "origin_action_id" not in transition_columns:
            self._conn.execute("ALTER TABLE worldops_transition_ledger ADD COLUMN origin_action_id TEXT")
        self._conn.execute(
            "UPDATE worldops_transition_ledger SET origin_action_id=transition_id "
            "WHERE origin_action_id IS NULL OR origin_action_id=''"
        )
        clock_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(worldops_clock_ledger)").fetchall()
        }
        if "origin_action_id" not in clock_columns:
            self._conn.execute("ALTER TABLE worldops_clock_ledger ADD COLUMN origin_action_id TEXT")
        self._conn.execute(
            "UPDATE worldops_clock_ledger SET origin_action_id=clock_event_id "
            "WHERE origin_action_id IS NULL OR origin_action_id=''"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_worldops_transition_origin "
            "ON worldops_transition_ledger(timeline_id, origin_action_id)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_worldops_clock_origin "
            "ON worldops_clock_ledger(timeline_id, origin_action_id)"
        )

    @staticmethod
    def _configure(connection: sqlite3.Connection, *, query_only: bool) -> None:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        if query_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")

    def _read_connection(self) -> sqlite3.Connection:
        connection = getattr(self._read_local, "connection", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        self._configure(connection, query_only=True)
        self._read_local.connection = connection
        with self._read_connections_lock:
            self._read_connections.append(connection)
        return connection

    def close(self) -> None:
        with self._lock:
            with self._read_connections_lock:
                for connection in self._read_connections:
                    connection.close()
                self._read_connections.clear()
            self._conn.close()

    def reset(self) -> None:
        tables = [
            "worldops_forget_tombstone",
            "worldops_memory_projection",
            "worldops_sync_conflict",
            "worldops_contribution_receipt",
            "worldops_control",
            "worldops_dead_letter",
            "worldops_metric",
            "worldops_trace",
            "worldops_checkpoint",
            "worldops_consumer_effect",
            "worldops_outbox",
            "worldops_clock_ledger",
            "worldops_transition_ledger",
            "worldops_timeline_fact",
            "worldops_task_evidence",
            "worldops_choice_ledger",
            "worldops_event_instance",
            "worldops_timeline",
            "worldops_bundle_registry",
            "worldops_trusted_key",
            "worldops_identity",
        ]
        with self._lock, self._conn:
            for table in tables:
                self._conn.execute(f"DELETE FROM {table}")
        self._ensure_identity()

    def _ensure_identity(self) -> None:
        now = utc_now()
        values = self.identity.to_dict()
        with self._lock, self._conn:
            existing = self._conn.execute(
                """
                SELECT world_id FROM worldops_identity
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                tuple(values[key] for key in ("tenant_id", "user_id", "sora_id", "timeline_id")),
            ).fetchone()
            if existing is not None and str(existing["world_id"]) != self.world_id:
                raise WorldOpsConflictError("timeline is already bound to a different world")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO worldops_identity (
                    tenant_id,user_id,sora_id,timeline_id,world_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                    self.world_id,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO worldops_timeline (
                    timeline_id,tenant_id,user_id,sora_id,world_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    self.identity.timeline_id,
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.world_id,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO worldops_control (
                    tenant_id,user_id,sora_id,updated_at
                ) VALUES (?,?,?,?)
                """,
                (self.identity.tenant_id, self.identity.user_id, self.identity.sora_id, now),
            )

    def identity_state(self) -> JsonDict:
        row = self._read_connection().execute(
            """
            SELECT * FROM worldops_identity
            WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
            """,
            (
                self.identity.tenant_id,
                self.identity.user_id,
                self.identity.sora_id,
                self.identity.timeline_id,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("WorldOps identity is missing")
        return dict(row)

    def timeline(self) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_timeline WHERE timeline_id=?",
            (self.identity.timeline_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("WorldOps timeline is missing")
        return dict(row)

    def register_trusted_key(
        self,
        *,
        key_id: str,
        public_key: bytes,
        algorithm: str = "Ed25519",
        valid_from: str | None = None,
        valid_to: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM worldops_trusted_key WHERE key_id=?",
                (key_id,),
            ).fetchone()
            if existing is not None:
                if bytes(existing["public_key"]) != public_key or str(existing["algorithm"]) != algorithm:
                    raise WorldOpsSecurityError("trusted key identifiers are immutably bound to key material")
                if existing["status"] != "active":
                    raise WorldOpsSecurityError("a revoked signing key cannot be reactivated")
                next_valid_to = valid_to if valid_to is not None else existing["valid_to"]
                next_metadata = dict(json_loads(existing["metadata_json"], {}))
                next_metadata.update(dict(metadata or {}))
                self._conn.execute(
                    "UPDATE worldops_trusted_key SET valid_to=?,metadata_json=? WHERE key_id=?",
                    (next_valid_to, json_dumps(next_metadata), key_id),
                )
                return
            self._conn.execute(
                """
                INSERT INTO worldops_trusted_key (
                    key_id,algorithm,public_key,status,valid_from,valid_to,metadata_json
                ) VALUES (?,?,?,'active',?,?,?)
                """,
                (key_id, algorithm, public_key, valid_from or now, valid_to, json_dumps(dict(metadata or {}))),
            )

    def trusted_keys(self, *, active_only: bool = True) -> list[JsonDict]:
        if active_only:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_trusted_key WHERE status='active' ORDER BY key_id"
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_trusted_key ORDER BY key_id"
            ).fetchall()
        return [dict(row) | {"metadata": json_loads(row["metadata_json"], {})} for row in rows]

    def revoke_key(self, key_id: str, *, revocation_list_version: int, reason: str) -> None:
        now = utc_now()
        with self._lock, self._conn:
            result = self._conn.execute(
                "UPDATE worldops_trusted_key SET status='revoked', revoked_at=? WHERE key_id=?",
                (now, key_id),
            )
            if result.rowcount != 1:
                raise KeyError(key_id)
            self._conn.execute(
                """
                UPDATE worldops_identity SET revocation_watermark=MAX(revocation_watermark,?),updated_at=?
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                (
                    revocation_list_version,
                    now,
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                ),
            )
            self._trace_locked("key_revoked", "applied", {"key_id": key_id, "reason": reason})

    def install_bundle(self, compiled: Mapping[str, Any], *, allow_expired_for_recovery: bool = False) -> JsonDict:
        raw_manifest = dict(compiled.get("manifest") or {})
        key_id = str(raw_manifest.get("key_id") or "")
        key = self._read_connection().execute(
            "SELECT * FROM worldops_trusted_key WHERE key_id=?",
            (key_id,),
        ).fetchone()
        if key is None or key["status"] != "active":
            raise WorldOpsSecurityError("bundle signing key is not trusted")
        if key["algorithm"] != "Ed25519":
            raise WorldOpsSecurityError("unsupported bundle signature algorithm")
        verifier = Ed25519BundleVerifier(bytes(key["public_key"]), key_id=key_id)
        manifest = verify_compiled_bundle(compiled, verifier)
        if manifest.world_id != self.world_id:
            raise WorldOpsSecurityError("bundle world_id does not match timeline")
        if manifest.channel != self.accepted_channel or manifest.cohort != self.accepted_cohort:
            raise WorldOpsSecurityError("bundle channel or cohort is not accepted by this client")
        now_dt = datetime.now(timezone.utc)
        issued_at = datetime.fromisoformat(manifest.issued_at)
        expires_at = datetime.fromisoformat(manifest.expires_at)
        if issued_at.timestamp() > now_dt.timestamp() + 300:
            raise WorldOpsSecurityError("bundle manifest was issued too far in the future")
        key_valid_from = datetime.fromisoformat(str(key["valid_from"]))
        if issued_at < key_valid_from:
            raise WorldOpsSecurityError("bundle predates the signing key validity window")
        if key["valid_to"] and issued_at > datetime.fromisoformat(str(key["valid_to"])):
            raise WorldOpsSecurityError("bundle exceeds the signing key validity window")
        if expires_at < now_dt and not allow_expired_for_recovery:
            raise WorldOpsSecurityError("bundle manifest has expired")
        identity = self.identity_state()
        if manifest.revocation_list_version < int(identity["revocation_watermark"]):
            raise WorldOpsSecurityError("bundle uses a stale revocation list")
        if manifest.release_sequence < int(identity["minimum_accepted_sequence"]):
            raise WorldOpsSecurityError("bundle is below the minimum accepted release sequence")
        now = utc_now()
        bundle = dict(compiled["bundle"])
        version = manifest.canon_version
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO worldops_bundle_registry (
                    bundle_id,version,world_id,channel,cohort,release_sequence,
                    minimum_accepted_sequence,key_id,revocation_list_version,content_hash,
                    manifest_json,bundle_json,status,verified_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'verified',?,?)
                ON CONFLICT(bundle_id,version) DO UPDATE SET
                    manifest_json=excluded.manifest_json,bundle_json=excluded.bundle_json,
                    status='verified',verified_at=excluded.verified_at,rejected_reason=NULL
                """,
                (
                    manifest.bundle_id,
                    version,
                    manifest.world_id,
                    manifest.channel,
                    manifest.cohort,
                    manifest.release_sequence,
                    manifest.minimum_accepted_sequence,
                    manifest.key_id,
                    manifest.revocation_list_version,
                    manifest.content_hash,
                    json_dumps(manifest.to_dict()),
                    json_dumps(bundle),
                    now,
                    now,
                ),
            )
            self._trace_locked(
                "bundle_verified",
                "passed",
                {"bundle_id": manifest.bundle_id, "sequence": manifest.release_sequence},
                bundle_id=manifest.bundle_id,
            )
        return {"status": "verified", "manifest": manifest.to_dict()}

    def activate_bundle(self, bundle_id: str, version: str, *, recovery: bool = False) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM worldops_bundle_registry WHERE bundle_id=? AND version=?",
                (bundle_id, version),
            ).fetchone()
            if row is None or row["status"] not in {"verified", "active", "superseded"}:
                raise WorldOpsSecurityError("bundle is not verified")
            identity = self._conn.execute(
                """
                SELECT * FROM worldops_identity
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                (
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                ),
            ).fetchone()
            sequence = int(row["release_sequence"])
            highest = int(identity["active_release_sequence"])
            if sequence < highest:
                manifest = json_loads(row["manifest_json"], {})
                if not recovery or not manifest.get("rollback_authorization"):
                    raise WorldOpsSecurityError("anti-rollback rejected an older signed bundle")
                raise WorldOpsSecurityError(
                    "rollback must be delivered as a new higher-sequence compensating release"
                )
            self._conn.execute(
                "UPDATE worldops_bundle_registry SET status='superseded' WHERE world_id=? AND status='active'",
                (self.world_id,),
            )
            self._conn.execute(
                """
                UPDATE worldops_bundle_registry SET status='active',activated_at=?
                WHERE bundle_id=? AND version=?
                """,
                (now, bundle_id, version),
            )
            self._conn.execute(
                """
                UPDATE worldops_identity SET
                    active_bundle_id=?,active_bundle_version=?,active_release_sequence=?,
                    minimum_accepted_sequence=MAX(minimum_accepted_sequence,?),
                    revocation_watermark=MAX(revocation_watermark,?),updated_at=?
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                (
                    bundle_id,
                    version,
                    sequence,
                    int(row["minimum_accepted_sequence"]),
                    int(row["revocation_list_version"]),
                    now,
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                ),
            )
            bundle = json_loads(row["bundle_json"], {})
            for event in bundle.get("events") or []:
                self._ensure_event_instance_locked(event, bundle_id=bundle_id, now=now)
            self._enqueue_locked(
                "worldops.bundle.activated.v1",
                event_key=f"bundle:{bundle_id}:{version}:activated",
                payload={"bundle_id": bundle_id, "version": version, "release_sequence": sequence},
                correlation_id=None,
                causation_id=None,
            )
            self._trace_locked(
                "bundle_activated",
                "applied",
                {"bundle_id": bundle_id, "version": version, "sequence": sequence},
                bundle_id=bundle_id,
            )
        return {"status": "active", "bundle_id": bundle_id, "version": version, "release_sequence": sequence}

    def active_bundle(self) -> JsonDict | None:
        identity = self.identity_state()
        if not identity.get("active_bundle_id"):
            return None
        row = self._read_connection().execute(
            "SELECT * FROM worldops_bundle_registry WHERE bundle_id=? AND version=? AND status='active'",
            (identity["active_bundle_id"], identity["active_bundle_version"]),
        ).fetchone()
        if row is None:
            return None
        return {
            "manifest": json_loads(row["manifest_json"], {}),
            "bundle": json_loads(row["bundle_json"], {}),
        }

    def event_definition(self, event_id: str, *, definition_version: int | None = None) -> JsonDict | None:
        active = self.active_bundle()
        if active is None:
            return None
        candidates = [item for item in active["bundle"].get("events") or [] if item.get("event_id") == event_id]
        if definition_version is not None:
            candidates = [item for item in candidates if int(item.get("definition_version", 1)) == definition_version]
        return dict(candidates[0]) if candidates else None

    def list_event_instances(self, *, states: set[str] | None = None) -> list[JsonDict]:
        params: list[Any] = [self.identity.timeline_id]
        where = "timeline_id=?"
        if states:
            placeholders = ",".join("?" for _ in states)
            where += f" AND state IN ({placeholders})"
            params.extend(sorted(states))
        rows = self._read_connection().execute(
            f"SELECT * FROM worldops_event_instance WHERE {where} ORDER BY updated_at DESC,instance_id",
            tuple(params),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_event_instance(self, instance_id: str) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_event_instance WHERE instance_id=? AND timeline_id=?",
            (instance_id, self.identity.timeline_id),
        ).fetchone()
        if row is None:
            raise KeyError(instance_id)
        return self._event_from_row(row)

    def event_instance_for(self, event_id: str) -> JsonDict | None:
        row = self._read_connection().execute(
            """
            SELECT * FROM worldops_event_instance
            WHERE timeline_id=? AND event_id=? ORDER BY definition_version DESC LIMIT 1
            """,
            (self.identity.timeline_id, event_id),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def transition_event(
        self,
        instance_id: str,
        *,
        to_state: str,
        expected_revision: int,
        trigger_type: str,
        trigger_ref: str | None = None,
        choice_id: str | None = None,
        effects: list[JsonDict] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        origin_action_id: str | None = None,
        emit_events: bool = True,
    ) -> JsonDict:
        if to_state not in EVENT_STATES:
            raise WorldOpsContractError("invalid target event state")
        now = utc_now()
        effects = [dict(item) for item in (effects or [])]
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM worldops_event_instance WHERE instance_id=? AND timeline_id=?",
                (instance_id, self.identity.timeline_id),
            ).fetchone()
            if row is None:
                raise KeyError(instance_id)
            if int(row["revision"]) != expected_revision:
                raise WorldOpsConflictError("event revision conflict")
            if row["state"] == to_state:
                return {"status": "duplicate", "event": self._event_from_row(row)}
            definition = self.event_definition(
                str(row["event_id"]), definition_version=int(row["definition_version"])
            )
            if definition is None:
                raise WorldOpsContractError("event definition is unavailable")
            authored_transition = self._authored_transition(
                definition,
                from_state=str(row["state"]),
                to_state=to_state,
                trigger_type=trigger_type,
                choice_id=choice_id,
            )
            if authored_transition is None:
                raise WorldOpsContractError("event transition is not authorized by the signed definition")
            authored_effects = [dict(item) for item in authored_transition.get("effects") or []]
            if effects and content_hash(effects) != content_hash(authored_effects):
                raise WorldOpsContractError("transition effects must exactly match the signed definition")
            effects = authored_effects
            if trigger_type == "tasks_complete":
                accepted = self._conn.execute(
                    "SELECT COUNT(*) FROM worldops_task_evidence WHERE instance_id=? AND accepted=1",
                    (instance_id,),
                ).fetchone()[0]
                if int(accepted) == 0:
                    raise WorldOpsContractError("task completion requires accepted evidence")
            next_event_revision = expected_revision + 1
            timeline = self._conn.execute(
                "SELECT revision FROM worldops_timeline WHERE timeline_id=?",
                (self.identity.timeline_id,),
            ).fetchone()
            next_timeline_revision = int(timeline["revision"]) + 1
            transition_id = new_id("wot")
            stable_origin_action_id = str(
                origin_action_id
                or "wota."
                + content_hash(
                    {
                        "event_id": row["event_id"],
                        "definition_version": row["definition_version"],
                        "from_state": row["state"],
                        "to_state": to_state,
                        "event_revision": next_event_revision,
                        "trigger_type": trigger_type,
                        "trigger_ref": trigger_ref,
                        "choice_id": choice_id,
                        "effects": effects,
                    }
                )[:40]
            )
            existing_origin = self._conn.execute(
                "SELECT transition_id FROM worldops_transition_ledger WHERE timeline_id=? AND origin_action_id=?",
                (self.identity.timeline_id, stable_origin_action_id),
            ).fetchone()
            if existing_origin is not None:
                return {
                    "status": "duplicate",
                    "transition_id": str(existing_origin["transition_id"]),
                    "event": self._event_from_row(row),
                }
            timestamps = {
                "activated_at": now if to_state == "active" else row["activated_at"],
                "completed_at": now if to_state == "completed" else row["completed_at"],
                "archived_at": now if to_state == "archived" else row["archived_at"],
            }
            updated = self._conn.execute(
                """
                UPDATE worldops_event_instance SET
                    state=?,revision=?,selected_choice_id=COALESCE(?,selected_choice_id),
                    activated_at=?,completed_at=?,archived_at=?,updated_at=?
                WHERE instance_id=? AND revision=?
                """,
                (
                    to_state,
                    next_event_revision,
                    choice_id,
                    timestamps["activated_at"],
                    timestamps["completed_at"],
                    timestamps["archived_at"],
                    now,
                    instance_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise WorldOpsConflictError("event revision conflict")
            self._conn.execute(
                "UPDATE worldops_timeline SET revision=?,updated_at=? WHERE timeline_id=?",
                (next_timeline_revision, now, self.identity.timeline_id),
            )
            self._conn.execute(
                """
                UPDATE worldops_identity SET state_revision=?,updated_at=?
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                (
                    next_timeline_revision,
                    now,
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO worldops_transition_ledger (
                    transition_id,origin_action_id,timeline_id,instance_id,event_id,from_state,to_state,
                    event_revision,timeline_revision,trigger_type,trigger_ref,choice_id,
                    effects_json,definition_version,bundle_id,correlation_id,causation_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    transition_id,
                    stable_origin_action_id,
                    self.identity.timeline_id,
                    instance_id,
                    row["event_id"],
                    row["state"],
                    to_state,
                    next_event_revision,
                    next_timeline_revision,
                    trigger_type,
                    trigger_ref,
                    choice_id,
                    json_dumps(effects),
                    row["definition_version"],
                    row["bundle_id"],
                    correlation_id,
                    causation_id,
                    now,
                ),
            )
            for effect in effects:
                self._apply_effect_locked(
                    effect,
                    timeline_revision=next_timeline_revision,
                    instance_id=instance_id,
                    transition_id=transition_id,
                    now=now,
                )
            event_payload = {
                "transition_id": transition_id,
                "origin_action_id": stable_origin_action_id,
                "event_id": row["event_id"],
                "instance_id": instance_id,
                "from_state": row["state"],
                "to_state": to_state,
                "choice_id": choice_id,
                "timeline_revision": next_timeline_revision,
                "effects": effects,
            }
            if emit_events:
                self._enqueue_locked(
                    "worldops.event.transitioned.v1",
                    event_key=f"transition:{stable_origin_action_id}",
                    payload=event_payload,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                )
            participated = bool(
                self._conn.execute(
                    """
                    SELECT 1 FROM worldops_choice_ledger
                    WHERE timeline_id=? AND instance_id=? AND status='accepted' AND choice_id IS NOT NULL
                    UNION ALL
                    SELECT 1 FROM worldops_task_evidence
                    WHERE timeline_id=? AND instance_id=? AND accepted=1
                    LIMIT 1
                    """,
                    (
                        self.identity.timeline_id,
                        instance_id,
                        self.identity.timeline_id,
                        instance_id,
                    ),
                ).fetchone()
            )
            if emit_events and to_state == "completed" and participated:
                self._enqueue_locked(
                    "worldops.shared_episode.completed.v1",
                    event_key=f"shared-episode:{row['event_id']}:{stable_origin_action_id}",
                    payload=event_payload,
                    correlation_id=correlation_id,
                    causation_id=transition_id,
                )
            self._trace_locked(
                "event_transition",
                "applied",
                event_payload,
                bundle_id=row["bundle_id"],
                instance_id=instance_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        return {
            "status": "applied",
            "transition_id": transition_id,
            "timeline_revision": next_timeline_revision,
            "event": self.get_event_instance(instance_id),
        }

    def record_choice(
        self,
        interpretation: Mapping[str, Any],
        *,
        status: str,
    ) -> JsonDict:
        now = utc_now()
        input_id = str(interpretation["input_id"])
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM worldops_choice_ledger WHERE timeline_id=? AND input_id=?",
                (self.identity.timeline_id, input_id),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", "choice": self._choice_from_row(existing)}
            choice_id = new_id("choice")
            self._conn.execute(
                """
                INSERT INTO worldops_choice_ledger (
                    choice_ledger_id,timeline_id,instance_id,input_id,interpretation_id,
                    choice_id,interpretation_json,evidence_refs_json,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    choice_id,
                    self.identity.timeline_id,
                    interpretation["event_instance_id"],
                    input_id,
                    interpretation["interpretation_id"],
                    interpretation.get("choice_id"),
                    json_dumps(dict(interpretation)),
                    json_dumps(list(interpretation.get("evidence_refs") or [])),
                    status,
                    now,
                ),
            )
        return {"status": "recorded", "choice": self.get_choice(choice_id)}

    def get_choice(self, choice_ledger_id: str) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_choice_ledger WHERE choice_ledger_id=? AND timeline_id=?",
            (choice_ledger_id, self.identity.timeline_id),
        ).fetchone()
        if row is None:
            raise KeyError(choice_ledger_id)
        return self._choice_from_row(row)

    def add_task_evidence(
        self,
        *,
        instance_id: str,
        task_id: str,
        input_id: str,
        evidence_type: str,
        payload: Mapping[str, Any],
        confidence: float,
        accepted: bool,
        correlation_id: str | None,
    ) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                """
                SELECT * FROM worldops_task_evidence
                WHERE instance_id=? AND task_id=? AND input_id=? AND evidence_type=?
                """,
                (instance_id, task_id, input_id, evidence_type),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", "evidence": self._evidence_from_row(existing)}
            evidence_id = new_id("wote")
            self._conn.execute(
                """
                INSERT INTO worldops_task_evidence (
                    evidence_id,timeline_id,instance_id,task_id,input_id,evidence_type,
                    payload_json,confidence,accepted,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_id,
                    self.identity.timeline_id,
                    instance_id,
                    task_id,
                    input_id,
                    evidence_type,
                    json_dumps(dict(payload)),
                    max(0.0, min(1.0, float(confidence))),
                    int(accepted),
                    now,
                ),
            )
            if accepted:
                self._enqueue_locked(
                    "worldops.task.evidence.accepted.v1",
                    event_key=f"task-evidence:{evidence_id}",
                    payload={"evidence_id": evidence_id, "instance_id": instance_id, "task_id": task_id},
                    correlation_id=correlation_id,
                    causation_id=input_id,
                )
        return {"status": "recorded", "evidence": self.task_evidence(evidence_id)}

    def task_evidence(self, evidence_id: str) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_task_evidence WHERE evidence_id=? AND timeline_id=?",
            (evidence_id, self.identity.timeline_id),
        ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        return self._evidence_from_row(row)

    def list_task_evidence(self, instance_id: str, task_id: str | None = None) -> list[JsonDict]:
        if task_id is None:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_task_evidence WHERE timeline_id=? AND instance_id=? ORDER BY created_at",
                (self.identity.timeline_id, instance_id),
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                """
                SELECT * FROM worldops_task_evidence
                WHERE timeline_id=? AND instance_id=? AND task_id=? ORDER BY created_at
                """,
                (self.identity.timeline_id, instance_id, task_id),
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def active_facts(self) -> JsonDict:
        rows = self._read_connection().execute(
            """
            SELECT fact_key,fact_value_json FROM worldops_timeline_fact
            WHERE timeline_id=? AND valid_to_revision IS NULL ORDER BY fact_key
            """,
            (self.identity.timeline_id,),
        ).fetchall()
        return {row["fact_key"]: json_loads(row["fact_value_json"], None) for row in rows}

    def get_control(self) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_control WHERE tenant_id=? AND user_id=? AND sora_id=?",
            (self.identity.tenant_id, self.identity.user_id, self.identity.sora_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("WorldOps control row is missing")
        result = dict(row)
        for key in ("ambient_enabled", "story_mode", "contribution_enabled", "attribution_allowed"):
            result[key] = bool(result[key])
        return result

    def update_control(self, **changes: Any) -> JsonDict:
        allowed = {
            "ambient_enabled": lambda v: int(bool(v)),
            "story_mode": lambda v: int(bool(v)),
            "contribution_enabled": lambda v: int(bool(v)),
            "attribution_allowed": lambda v: int(bool(v)),
            "expression_permission": str,
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported WorldOps control: {key}")
            if key == "expression_permission" and str(value) not in {"silent", "implicit", "explicit"}:
                raise ValueError("expression_permission must be silent, implicit, or explicit")
            assignments.append(f"{key}=?")
            params.append(allowed[key](value))
        if not assignments:
            return self.get_control()
        params.extend(
            [
                utc_now(),
                self.identity.tenant_id,
                self.identity.user_id,
                self.identity.sora_id,
            ]
        )
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE worldops_control SET {','.join(assignments)},updated_at=? WHERE tenant_id=? AND user_id=? AND sora_id=?",
                tuple(params),
            )
        return self.get_control()

    def advance_world_clock(
        self,
        delta: float,
        *,
        reason: str,
        correlation_id: str | None = None,
        origin_action_id: str | None = None,
        emit_events: bool = True,
    ) -> JsonDict:
        if not 0.0 < float(delta) <= 86400.0:
            raise ValueError("world clock delta must be in (0, 86400]")
        now = utc_now()
        with self._lock, self._conn:
            stable_origin_action_id = str(origin_action_id or new_id("woclock-origin"))
            existing_origin = self._conn.execute(
                "SELECT clock_event_id FROM worldops_clock_ledger WHERE timeline_id=? AND origin_action_id=?",
                (self.identity.timeline_id, stable_origin_action_id),
            ).fetchone()
            if existing_origin is not None:
                current = self._conn.execute(
                    "SELECT world_clock,revision FROM worldops_timeline WHERE timeline_id=?",
                    (self.identity.timeline_id,),
                ).fetchone()
                return {
                    "status": "duplicate",
                    "world_clock": float(current["world_clock"]),
                    "timeline_revision": int(current["revision"]),
                }
            row = self._conn.execute(
                "SELECT world_clock,revision FROM worldops_timeline WHERE timeline_id=?",
                (self.identity.timeline_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("WorldOps timeline is missing")
            world_clock = float(row["world_clock"]) + float(delta)
            revision = int(row["revision"]) + 1
            self._conn.execute(
                "UPDATE worldops_timeline SET world_clock=?,revision=?,updated_at=? WHERE timeline_id=?",
                (world_clock, revision, now, self.identity.timeline_id),
            )
            clock_event_id = new_id("woclock")
            self._conn.execute(
                """
                INSERT INTO worldops_clock_ledger (
                    clock_event_id,origin_action_id,timeline_id,timeline_revision,delta,world_clock,reason,
                    correlation_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    clock_event_id,
                    stable_origin_action_id,
                    self.identity.timeline_id,
                    revision,
                    float(delta),
                    world_clock,
                    reason,
                    correlation_id,
                    now,
                ),
            )
            self._conn.execute(
                """
                UPDATE worldops_identity SET state_revision=?,updated_at=?
                WHERE tenant_id=? AND user_id=? AND sora_id=? AND timeline_id=?
                """,
                (
                    revision,
                    now,
                    self.identity.tenant_id,
                    self.identity.user_id,
                    self.identity.sora_id,
                    self.identity.timeline_id,
                ),
            )
            if emit_events:
                self._enqueue_locked(
                    "worldops.clock.advanced.v1",
                    event_key=f"clock:{self.identity.timeline_id}:{stable_origin_action_id}",
                    payload={"delta": float(delta), "world_clock": world_clock, "revision": revision, "reason": reason},
                    correlation_id=correlation_id,
                    causation_id=None,
                )
        return {"status": "applied", "world_clock": world_clock, "timeline_revision": revision}

    def save_contribution_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        receipt_token: str,
        withdrawal_token: str,
    ) -> JsonDict:
        proposal_id = str(receipt.get("proposal_id") or "")
        if not proposal_id or not receipt_token or not withdrawal_token:
            raise WorldOpsContractError("complete contribution receipt is required")
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO worldops_contribution_receipt (
                    proposal_id,timeline_id,receipt_token_hash,withdrawal_token_hash,status,
                    result_summary,server_revision,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status=excluded.status,result_summary=excluded.result_summary,
                    server_revision=excluded.server_revision,updated_at=excluded.updated_at
                """,
                (
                    proposal_id,
                    self.identity.timeline_id,
                    hashlib.sha256(receipt_token.encode("utf-8")).hexdigest(),
                    hashlib.sha256(withdrawal_token.encode("utf-8")).hexdigest(),
                    str(receipt.get("status") or "received"),
                    receipt.get("result_summary"),
                    receipt.get("server_revision"),
                    now,
                    now,
                ),
            )
        return self.contribution_receipt(proposal_id)

    def contribution_receipt(self, proposal_id: str) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_contribution_receipt WHERE proposal_id=? AND timeline_id=?",
            (proposal_id, self.identity.timeline_id),
        ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        result = dict(row)
        result.pop("receipt_token_hash", None)
        result.pop("withdrawal_token_hash", None)
        return result

    def list_contribution_receipts(self, *, limit: int = 100) -> list[JsonDict]:
        rows = self._read_connection().execute(
            """
            SELECT proposal_id,timeline_id,status,result_summary,server_revision,created_at,updated_at
            FROM worldops_contribution_receipt
            WHERE timeline_id=? ORDER BY updated_at DESC LIMIT ?
            """,
            (self.identity.timeline_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def forget_source(self, source_ref: str, *, reason_code: str) -> JsonDict:
        ref_hash = content_hash({"timeline_id": self.identity.timeline_id, "source_ref": source_ref})
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO worldops_forget_tombstone (
                    timeline_id,ref_hash,ref_kind,reason_code,created_at
                ) VALUES (?,?,?,?,?)
                """,
                (self.identity.timeline_id, ref_hash, "source_ref", reason_code, now),
            )
            self._conn.execute(
                "UPDATE worldops_memory_projection SET active=0,updated_at=? WHERE timeline_id=? AND source_memory_id=?",
                (now, self.identity.timeline_id, source_ref),
            )
        return {"status": "forgotten", "source_ref_hash": ref_hash, "reason_code": reason_code}

    def source_is_forgotten(self, source_ref: str) -> bool:
        ref_hash = content_hash({"timeline_id": self.identity.timeline_id, "source_ref": source_ref})
        row = self._read_connection().execute(
            "SELECT 1 FROM worldops_forget_tombstone WHERE timeline_id=? AND ref_hash=?",
            (self.identity.timeline_id, ref_hash),
        ).fetchone()
        return row is not None

    def export_sync_snapshot(self) -> JsonDict:
        """Export a typed, content-addressed timeline delta without raw dialogue."""
        conn = self._read_connection()
        instances = {
            row["instance_id"]: row["event_id"]
            for row in conn.execute(
                "SELECT instance_id,event_id FROM worldops_event_instance WHERE timeline_id=?",
                (self.identity.timeline_id,),
            ).fetchall()
        }
        transitions = conn.execute(
            "SELECT * FROM worldops_transition_ledger WHERE timeline_id=? ORDER BY timeline_revision,transition_id",
            (self.identity.timeline_id,),
        ).fetchall()
        clocks = conn.execute(
            "SELECT * FROM worldops_clock_ledger WHERE timeline_id=? ORDER BY timeline_revision,clock_event_id",
            (self.identity.timeline_id,),
        ).fetchall()
        actions: list[JsonDict] = []
        for row in transitions:
            actions.append(
                {
                    "kind": "transition",
                    "origin_action_id": str(row["origin_action_id"] or row["transition_id"]),
                    "timeline_revision": int(row["timeline_revision"]),
                    "event_id": row["event_id"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "event_revision": int(row["event_revision"]),
                    "trigger_type": row["trigger_type"],
                    "trigger_ref": row["trigger_ref"],
                    "choice_id": row["choice_id"],
                    "effects": json_loads(row["effects_json"], []),
                    "definition_version": int(row["definition_version"]),
                    "correlation_id": row["correlation_id"],
                    "causation_id": row["causation_id"],
                }
            )
        for row in clocks:
            actions.append(
                {
                    "kind": "clock",
                    "origin_action_id": str(row["origin_action_id"] or row["clock_event_id"]),
                    "timeline_revision": int(row["timeline_revision"]),
                    "delta": float(row["delta"]),
                    "world_clock": float(row["world_clock"]),
                    "reason": row["reason"],
                    "correlation_id": row["correlation_id"],
                }
            )
        actions.sort(key=lambda item: (int(item["timeline_revision"]), item["kind"]))
        rolling = "genesis"
        action_hashes: list[str] = []
        for action in actions:
            rolling = content_hash({"previous": rolling, "action": action})
            action_hashes.append(rolling)
        action_ids = [str(action["origin_action_id"]) for action in actions]
        choices: list[JsonDict] = []
        for row in conn.execute(
            "SELECT * FROM worldops_choice_ledger WHERE timeline_id=? ORDER BY created_at,choice_ledger_id",
            (self.identity.timeline_id,),
        ).fetchall():
            interpretation = json_loads(row["interpretation_json"], {})
            interpretation["event_id"] = instances.get(row["instance_id"])
            interpretation.pop("event_instance_id", None)
            choices.append({"interpretation": interpretation, "status": row["status"]})
        evidence: list[JsonDict] = []
        for row in conn.execute(
            "SELECT * FROM worldops_task_evidence WHERE timeline_id=? ORDER BY created_at,evidence_id",
            (self.identity.timeline_id,),
        ).fetchall():
            evidence.append(
                {
                    "event_id": instances.get(row["instance_id"]),
                    "task_id": row["task_id"],
                    "input_id": row["input_id"],
                    "evidence_type": row["evidence_type"],
                    "payload": json_loads(row["payload_json"], {}),
                    "confidence": float(row["confidence"]),
                    "accepted": bool(row["accepted"]),
                }
            )
        active = self.active_bundle()
        body = {
            "contract": WORLDOPS_SYNC_SNAPSHOT_CONTRACT,
            "identity": self.identity.to_dict(),
            "world_id": self.world_id,
            "active_manifest": (active or {}).get("manifest"),
            "timeline_revision": int(self.timeline()["revision"]),
            "actions": actions,
            "action_hashes": action_hashes,
            "action_ids": action_ids,
            "action_set_digest": content_hash(sorted(action_ids)),
            "choices": choices,
            "task_evidence": evidence,
            "raw_dialogue_included": False,
        }
        return body | {"checksum": content_hash(body)}

    def apply_sync_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        transport_authenticated: bool = False,
    ) -> JsonDict:
        if not transport_authenticated:
            raise WorldOpsSecurityError("WorldOps sync requires an authenticated transport session")
        payload = dict(snapshot)
        checksum = str(payload.pop("checksum", ""))
        if payload.get("contract") != WORLDOPS_SYNC_SNAPSHOT_CONTRACT or content_hash(payload) != checksum:
            raise WorldOpsSecurityError("WorldOps sync snapshot integrity check failed")
        if dict(payload.get("identity") or {}) != self.identity.to_dict():
            raise WorldOpsSecurityError("WorldOps sync identity does not match this timeline")
        if payload.get("raw_dialogue_included") is not False:
            raise WorldOpsSecurityError("WorldOps sync snapshots must not carry raw dialogue")
        active = self.active_bundle()
        incoming_manifest = dict(payload.get("active_manifest") or {})
        if active is None or incoming_manifest.get("content_hash") != active["manifest"].get("content_hash"):
            raise WorldOpsConflictError("install the matching signed bundle before timeline sync")
        local = self.export_sync_snapshot()
        local_actions = {_action_origin_id(item): dict(item) for item in local.get("actions") or []}
        incoming_actions = {_action_origin_id(item): dict(item) for item in payload.get("actions") or []}
        for action_id in local_actions.keys() & incoming_actions.keys():
            if _action_fingerprint(local_actions[action_id]) != _action_fingerprint(incoming_actions[action_id]):
                raise WorldOpsSecurityError("sync action origin was rebound to different content")
        incoming_missing = [
            action for action_id, action in incoming_actions.items() if action_id not in local_actions
        ]
        if not incoming_missing:
            return {
                "status": "current",
                "timeline_revision": int(self.timeline()["revision"]),
                "action_set_digest": local.get("action_set_digest"),
            }
        local_only = [action for action_id, action in local_actions.items() if action_id not in incoming_actions]
        locally_changed_events = {
            str(action.get("event_id")) for action in local_only if action.get("kind") == "transition"
        }
        conflicting_events = sorted(
            {
                str(action.get("event_id"))
                for action in incoming_missing
                if action.get("kind") == "transition"
                and str(action.get("event_id")) in locally_changed_events
            }
        )
        if conflicting_events:
            conflict = self._record_sync_conflict(
                payload | {"checksum": checksum},
                local_action_ids=sorted(local_actions),
                incoming_action_ids=sorted(incoming_actions),
                conflicting_event_ids=conflicting_events,
            )
            return {
                "status": "conflict",
                "conflict_id": conflict["conflict_id"],
                "conflicting_event_ids": conflicting_events,
                "allowed_resolutions": ["keep_local", "fork_local"],
            }
        incoming_missing.sort(key=lambda item: (int(item.get("timeline_revision", 0)), _action_origin_id(item)))
        with self._lock:
            self._validate_merge_actions(incoming_missing)
            for item in payload.get("choices") or []:
                interpretation = dict(item.get("interpretation") or {})
                event_id = str(interpretation.pop("event_id", ""))
                instance = self.event_instance_for(event_id)
                if instance is None:
                    raise WorldOpsConflictError(f"sync choice references unknown event {event_id}")
                interpretation["event_instance_id"] = instance["instance_id"]
                self.record_choice(interpretation, status=str(item.get("status") or "accepted"))
            for item in payload.get("task_evidence") or []:
                instance = self.event_instance_for(str(item.get("event_id") or ""))
                if instance is None:
                    raise WorldOpsConflictError("sync evidence references an unknown event")
                self.add_task_evidence(
                    instance_id=instance["instance_id"],
                    task_id=str(item["task_id"]),
                    input_id=str(item["input_id"]),
                    evidence_type=str(item["evidence_type"]),
                    payload=dict(item.get("payload") or {}),
                    confidence=float(item.get("confidence", 0.0)),
                    accepted=bool(item.get("accepted")),
                    correlation_id="device-sync",
                )
            applied = 0
            for action in incoming_missing:
                origin_action_id = _action_origin_id(action)
                if action["kind"] == "clock":
                    self.advance_world_clock(
                        float(action["delta"]),
                        reason=str(action.get("reason") or "device_sync"),
                        correlation_id=action.get("correlation_id"),
                        origin_action_id=origin_action_id,
                    )
                else:
                    instance = self.event_instance_for(str(action["event_id"]))
                    if instance is None:
                        raise WorldOpsConflictError("sync transition references an unknown event")
                    self.transition_event(
                        instance["instance_id"],
                        to_state=str(action["to_state"]),
                        expected_revision=int(instance["revision"]),
                        trigger_type=str(action["trigger_type"]),
                        trigger_ref=action.get("trigger_ref"),
                        choice_id=action.get("choice_id"),
                        effects=[dict(item) for item in action.get("effects") or []],
                        correlation_id=action.get("correlation_id"),
                        causation_id=action.get("causation_id"),
                        origin_action_id=origin_action_id,
                    )
                applied += 1
        final = self.export_sync_snapshot()
        final_action_ids = set(final.get("action_ids") or [])
        if not set(incoming_actions).issubset(final_action_ids):
            raise WorldOpsConflictError("WorldOps sync merge did not retain every incoming action")
        return {
            "status": "merged" if local_only else "applied",
            "applied_actions": applied,
            "timeline_revision": int(self.timeline()["revision"]),
            "action_set_digest": final.get("action_set_digest"),
            "requires_peer_reconciliation": bool(local_only),
        }

    def _validate_merge_actions(self, actions: list[JsonDict]) -> None:
        simulated: dict[str, tuple[str, int]] = {}
        for action in actions:
            if action.get("kind") == "clock":
                continue
            event_id = str(action.get("event_id") or "")
            instance = self.event_instance_for(event_id)
            if instance is None:
                raise WorldOpsConflictError(f"sync transition references unknown event {event_id}")
            state, revision = simulated.get(
                event_id,
                (str(instance["state"]), int(instance["revision"])),
            )
            if str(action.get("from_state")) != state or int(action.get("event_revision", 0)) != revision + 1:
                raise WorldOpsConflictError(f"sync transition conflicts with current event state: {event_id}")
            definition = self.event_definition(
                event_id,
                definition_version=int(action.get("definition_version", instance["definition_version"])),
            )
            if definition is None or not self._transition_is_authored(
                definition,
                from_state=state,
                to_state=str(action.get("to_state") or ""),
                trigger_type=str(action.get("trigger_type") or ""),
                choice_id=action.get("choice_id"),
            ):
                raise WorldOpsContractError("sync transition is not authorized by the signed definition")
            simulated[event_id] = (str(action["to_state"]), revision + 1)

    def _record_sync_conflict(
        self,
        snapshot: Mapping[str, Any],
        *,
        local_action_ids: list[str],
        incoming_action_ids: list[str],
        conflicting_event_ids: list[str],
    ) -> JsonDict:
        checksum = str(snapshot.get("checksum") or content_hash(dict(snapshot)))
        existing = self._read_connection().execute(
            """
            SELECT * FROM worldops_sync_conflict
            WHERE timeline_id=? AND incoming_snapshot_checksum=? AND status='open'
            ORDER BY created_at DESC LIMIT 1
            """,
            (self.identity.timeline_id, checksum),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        conflict_id = new_id("wosc")
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO worldops_sync_conflict (
                    conflict_id,timeline_id,incoming_snapshot_checksum,common_action_count,
                    local_action_ids_json,incoming_action_ids_json,conflicting_event_ids_json,
                    incoming_snapshot_json,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    conflict_id,
                    self.identity.timeline_id,
                    checksum,
                    len(set(local_action_ids) & set(incoming_action_ids)),
                    json_dumps(local_action_ids),
                    json_dumps(incoming_action_ids),
                    json_dumps(conflicting_event_ids),
                    json_dumps(dict(snapshot)),
                    "open",
                    now,
                ),
            )
            self._trace_locked(
                "device_sync",
                "conflict",
                {"conflict_id": conflict_id, "event_ids": conflicting_event_ids},
                correlation_id="device-sync",
            )
        return self.get_sync_conflict(conflict_id)

    def get_sync_conflict(self, conflict_id: str) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_sync_conflict WHERE conflict_id=? AND timeline_id=?",
            (conflict_id, self.identity.timeline_id),
        ).fetchone()
        if row is None:
            raise KeyError(conflict_id)
        result = dict(row)
        for key in ("local_action_ids", "incoming_action_ids", "conflicting_event_ids"):
            result[key] = json_loads(result.pop(f"{key}_json"), [])
        result["incoming_snapshot"] = json_loads(result.pop("incoming_snapshot_json"), {})
        return result

    def list_sync_conflicts(self, *, status: str | None = "open", limit: int = 100) -> list[JsonDict]:
        if status:
            rows = self._read_connection().execute(
                """
                SELECT conflict_id FROM worldops_sync_conflict
                WHERE timeline_id=? AND status=? ORDER BY created_at DESC LIMIT ?
                """,
                (self.identity.timeline_id, status, max(1, min(limit, 1000))),
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                """
                SELECT conflict_id FROM worldops_sync_conflict
                WHERE timeline_id=? ORDER BY created_at DESC LIMIT ?
                """,
                (self.identity.timeline_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self.get_sync_conflict(str(row["conflict_id"])) for row in rows]

    def resolve_sync_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
        fork_timeline_id: str | None = None,
    ) -> JsonDict:
        if resolution not in {"keep_local", "fork_local"}:
            raise ValueError("sync conflict resolution must be keep_local or fork_local")
        conflict = self.get_sync_conflict(conflict_id)
        if conflict["status"] != "open":
            return conflict
        fork_result: JsonDict | None = None
        if resolution == "fork_local":
            if not fork_timeline_id:
                raise ValueError("fork_timeline_id is required for fork_local")
            fork_result = self.fork_timeline(fork_timeline_id)
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE worldops_sync_conflict SET status='resolved',resolution=?,resolved_timeline_id=?,resolved_at=?
                WHERE conflict_id=? AND timeline_id=? AND status='open'
                """,
                (
                    resolution,
                    (fork_result or {}).get("timeline_id"),
                    now,
                    conflict_id,
                    self.identity.timeline_id,
                ),
            )
        return self.get_sync_conflict(conflict_id)

    def fork_timeline(self, new_timeline_id: str, *, fork_revision: int | None = None) -> JsonDict:
        """Create an explicit, provenance-preserving branch without replaying side effects."""
        child_identity = IdentityScope(
            tenant_id=self.identity.tenant_id,
            user_id=self.identity.user_id,
            sora_id=self.identity.sora_id,
            timeline_id=str(new_timeline_id),
        )
        child_identity.validate()
        if child_identity.timeline_id == self.identity.timeline_id:
            raise ValueError("fork timeline must use a new timeline_id")
        source_timeline = self.timeline()
        source_revision = int(source_timeline["revision"])
        target_revision = source_revision if fork_revision is None else int(fork_revision)
        if not 0 <= target_revision <= source_revision:
            raise ValueError("fork_revision is outside the source timeline")
        identity = self.identity_state()
        active = self.active_bundle()
        if active is None:
            raise WorldOpsConflictError("cannot fork a timeline without an active signed bundle")
        now = utc_now()
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM worldops_timeline WHERE timeline_id=?",
                (child_identity.timeline_id,),
            ).fetchone()
            if exists is not None:
                raise WorldOpsConflictError("fork timeline_id already exists")
            self._conn.execute(
                """
                INSERT INTO worldops_identity (
                    tenant_id,user_id,sora_id,timeline_id,world_id,active_bundle_id,
                    active_bundle_version,active_release_sequence,minimum_accepted_sequence,
                    revocation_watermark,state_revision,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    child_identity.tenant_id,
                    child_identity.user_id,
                    child_identity.sora_id,
                    child_identity.timeline_id,
                    self.world_id,
                    identity["active_bundle_id"],
                    identity["active_bundle_version"],
                    identity["active_release_sequence"],
                    identity["minimum_accepted_sequence"],
                    identity["revocation_watermark"],
                    target_revision,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO worldops_timeline (
                    timeline_id,tenant_id,user_id,sora_id,world_id,parent_timeline_id,
                    fork_revision,revision,world_clock,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)
                """,
                (
                    child_identity.timeline_id,
                    child_identity.tenant_id,
                    child_identity.user_id,
                    child_identity.sora_id,
                    self.world_id,
                    self.identity.timeline_id,
                    target_revision,
                    target_revision,
                    0.0,
                    now,
                    now,
                ),
            )
            instance_map: dict[str, str] = {}
            for event in active["bundle"].get("events") or []:
                parent_instance = self._conn.execute(
                    """
                    SELECT * FROM worldops_event_instance
                    WHERE timeline_id=? AND event_id=? AND definition_version=?
                    """,
                    (
                        self.identity.timeline_id,
                        str(event["event_id"]),
                        int(event.get("definition_version", 1)),
                    ),
                ).fetchone()
                if parent_instance is None:
                    continue
                child_instance_id = new_id("woi")
                instance_map[str(parent_instance["instance_id"])] = child_instance_id
                self._conn.execute(
                    """
                    INSERT INTO worldops_event_instance (
                        instance_id,timeline_id,event_id,definition_version,bundle_id,state,
                        revision,metadata_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        child_instance_id,
                        child_identity.timeline_id,
                        event["event_id"],
                        int(event.get("definition_version", 1)),
                        identity["active_bundle_id"],
                        str(event.get("initial_state") or "dormant"),
                        0,
                        "{}",
                        now,
                        now,
                    ),
                )
            transitions = self._conn.execute(
                """
                SELECT * FROM worldops_transition_ledger
                WHERE timeline_id=? AND timeline_revision<=?
                ORDER BY timeline_revision,transition_id
                """,
                (self.identity.timeline_id, target_revision),
            ).fetchall()
            for row in transitions:
                child_instance_id = instance_map[str(row["instance_id"])]
                child_transition_id = new_id("wot")
                self._conn.execute(
                    """
                    INSERT INTO worldops_transition_ledger (
                        transition_id,origin_action_id,timeline_id,instance_id,event_id,from_state,to_state,
                        event_revision,timeline_revision,trigger_type,trigger_ref,choice_id,effects_json,
                        definition_version,bundle_id,correlation_id,causation_id,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        child_transition_id,
                        str(row["origin_action_id"] or row["transition_id"]),
                        child_identity.timeline_id,
                        child_instance_id,
                        row["event_id"],
                        row["from_state"],
                        row["to_state"],
                        row["event_revision"],
                        row["timeline_revision"],
                        row["trigger_type"],
                        row["trigger_ref"],
                        row["choice_id"],
                        row["effects_json"],
                        row["definition_version"],
                        row["bundle_id"],
                        row["correlation_id"],
                        row["causation_id"],
                        row["created_at"],
                    ),
                )
                timestamps = {
                    "activated_at": row["created_at"] if row["to_state"] == "active" else None,
                    "completed_at": row["created_at"] if row["to_state"] == "completed" else None,
                    "archived_at": row["created_at"] if row["to_state"] == "archived" else None,
                }
                self._conn.execute(
                    """
                    UPDATE worldops_event_instance SET state=?,revision=?,selected_choice_id=COALESCE(?,selected_choice_id),
                        activated_at=COALESCE(?,activated_at),completed_at=COALESCE(?,completed_at),
                        archived_at=COALESCE(?,archived_at),updated_at=? WHERE instance_id=?
                    """,
                    (
                        row["to_state"],
                        row["event_revision"],
                        row["choice_id"],
                        timestamps["activated_at"],
                        timestamps["completed_at"],
                        timestamps["archived_at"],
                        row["created_at"],
                        child_instance_id,
                    ),
                )
                for effect in json_loads(row["effects_json"], []):
                    key = str(effect.get("key") or "")
                    if not key:
                        continue
                    self._conn.execute(
                        """
                        UPDATE worldops_timeline_fact SET valid_to_revision=?
                        WHERE timeline_id=? AND fact_key=? AND valid_to_revision IS NULL
                        """,
                        (row["timeline_revision"], child_identity.timeline_id, key),
                    )
                    if effect.get("op") == "set_fact":
                        self._conn.execute(
                            """
                            INSERT INTO worldops_timeline_fact (
                                fact_revision_id,timeline_id,fact_key,fact_value_json,source_instance_id,
                                source_transition_id,valid_from_revision,created_at
                            ) VALUES (?,?,?,?,?,?,?,?)
                            """,
                            (
                                new_id("wof"),
                                child_identity.timeline_id,
                                key,
                                json_dumps(effect.get("value")),
                                child_instance_id,
                                child_transition_id,
                                row["timeline_revision"],
                                row["created_at"],
                            ),
                        )
            clocks = self._conn.execute(
                """
                SELECT * FROM worldops_clock_ledger
                WHERE timeline_id=? AND timeline_revision<=?
                ORDER BY timeline_revision,clock_event_id
                """,
                (self.identity.timeline_id, target_revision),
            ).fetchall()
            for row in clocks:
                self._conn.execute(
                    """
                    INSERT INTO worldops_clock_ledger (
                        clock_event_id,origin_action_id,timeline_id,timeline_revision,delta,
                        world_clock,reason,correlation_id,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("woclock"),
                        str(row["origin_action_id"] or row["clock_event_id"]),
                        child_identity.timeline_id,
                        row["timeline_revision"],
                        row["delta"],
                        row["world_clock"],
                        row["reason"],
                        row["correlation_id"],
                        row["created_at"],
                    ),
                )
            child_clock = float(clocks[-1]["world_clock"]) if clocks else 0.0
            self._conn.execute(
                "UPDATE worldops_timeline SET world_clock=? WHERE timeline_id=?",
                (child_clock, child_identity.timeline_id),
            )
            cutoff = now
            if target_revision < source_revision:
                action_times = [str(row["created_at"]) for row in transitions] + [str(row["created_at"]) for row in clocks]
                cutoff = max(action_times) if action_times else str(source_timeline["created_at"])
            for row in self._conn.execute(
                """
                SELECT * FROM worldops_choice_ledger
                WHERE timeline_id=? AND created_at<=? ORDER BY created_at,choice_ledger_id
                """,
                (self.identity.timeline_id, cutoff),
            ).fetchall():
                child_instance_id = instance_map.get(str(row["instance_id"]))
                if child_instance_id is None:
                    continue
                interpretation = json_loads(row["interpretation_json"], {})
                interpretation["event_instance_id"] = child_instance_id
                interpretation["interpretation_id"] = "woifork." + content_hash(
                    {"source": row["interpretation_id"], "timeline": child_identity.timeline_id}
                )[:32]
                self._conn.execute(
                    """
                    INSERT INTO worldops_choice_ledger (
                        choice_ledger_id,timeline_id,instance_id,input_id,interpretation_id,choice_id,
                        interpretation_json,evidence_refs_json,status,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("wocl"),
                        child_identity.timeline_id,
                        child_instance_id,
                        row["input_id"],
                        interpretation["interpretation_id"],
                        row["choice_id"],
                        json_dumps(interpretation),
                        row["evidence_refs_json"],
                        row["status"],
                        row["created_at"],
                    ),
                )
            for row in self._conn.execute(
                """
                SELECT * FROM worldops_task_evidence
                WHERE timeline_id=? AND created_at<=? ORDER BY created_at,evidence_id
                """,
                (self.identity.timeline_id, cutoff),
            ).fetchall():
                child_instance_id = instance_map.get(str(row["instance_id"]))
                if child_instance_id is None:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO worldops_task_evidence (
                        evidence_id,timeline_id,instance_id,task_id,input_id,evidence_type,payload_json,
                        confidence,accepted,consumed,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("woe"),
                        child_identity.timeline_id,
                        child_instance_id,
                        row["task_id"],
                        row["input_id"],
                        row["evidence_type"],
                        row["payload_json"],
                        row["confidence"],
                        row["accepted"],
                        row["consumed"],
                        row["created_at"],
                    ),
                )
            self._trace_locked(
                "timeline_fork",
                "created",
                {"child_timeline_id": child_identity.timeline_id, "fork_revision": target_revision},
            )
        return {
            "status": "forked",
            "timeline_id": child_identity.timeline_id,
            "parent_timeline_id": self.identity.timeline_id,
            "fork_revision": target_revision,
            "world_clock": child_clock,
        }

    def store_memory_projection(self, event: Mapping[str, Any]) -> JsonDict:
        memory = dict(event.get("memory") or {})
        memory_id = str(memory.get("id") or "")
        if not memory_id:
            raise WorldOpsContractError("memory projection requires memory.id")
        action = str(event.get("action") or "upsert")
        now = utc_now()
        payload_hash = content_hash(event)
        with self._lock, self._conn:
            if action in {"delete", "rollback", "forget", "tombstone"} or event.get("event_type") == "memory_delete":
                self._conn.execute(
                    "UPDATE worldops_memory_projection SET active=0,updated_at=? WHERE source_memory_id=? AND timeline_id=?",
                    (now, memory_id, self.identity.timeline_id),
                )
                status = "tombstoned"
            else:
                self._conn.execute(
                    """
                    INSERT INTO worldops_memory_projection (
                        source_memory_id,timeline_id,event_id,payload_hash,payload_json,active,created_at,updated_at
                    ) VALUES (?,?,?,?,?,1,?,?)
                    ON CONFLICT(source_memory_id) DO UPDATE SET
                        event_id=excluded.event_id,payload_hash=excluded.payload_hash,
                        payload_json=excluded.payload_json,active=1,updated_at=excluded.updated_at
                    """,
                    (
                        memory_id,
                        self.identity.timeline_id,
                        str(event.get("event_id") or new_id("projection")),
                        payload_hash,
                        json_dumps(dict(event)),
                        now,
                        now,
                    ),
                )
                status = "applied"
        return {"status": status, "memory_id": memory_id, "payload_hash": payload_hash}

    def list_memory_projections(self, *, active: bool = True) -> list[JsonDict]:
        rows = self._read_connection().execute(
            """
            SELECT * FROM worldops_memory_projection
            WHERE timeline_id=? AND active=? ORDER BY updated_at DESC
            """,
            (self.identity.timeline_id, int(active)),
        ).fetchall()
        return [
            {
                **dict(row),
                "payload": json_loads(row["payload_json"], {}),
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    def pending_outbox(self, *, limit: int = 100) -> list[JsonDict]:
        now = utc_now()
        rows = self._read_connection().execute(
            """
            SELECT * FROM worldops_outbox
            WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY created_at,event_id LIMIT ?
            """,
            (now, max(1, min(limit, 1000))),
        ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def consumer_effect(self, source_event_id: str, effect_key: str) -> JsonDict | None:
        row = self._read_connection().execute(
            "SELECT * FROM worldops_consumer_effect WHERE source_event_id=? AND effect_key=?",
            (source_event_id, effect_key),
        ).fetchone()
        if row is None:
            return None
        return {**dict(row), "result": json_loads(row["result_json"], {})}

    def start_consumer_effect(self, source_event_id: str, effect_key: str) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            outbox = self._conn.execute(
                "SELECT event_id FROM worldops_outbox WHERE event_id=?",
                (source_event_id,),
            ).fetchone()
            if outbox is None:
                raise KeyError(source_event_id)
            self._conn.execute(
                """
                INSERT INTO worldops_consumer_effect (
                    source_event_id,effect_key,status,attempt_count,result_json,created_at,updated_at
                ) VALUES (?,?,'started',1,'{}',?,?)
                ON CONFLICT(source_event_id,effect_key) DO UPDATE SET
                    status=CASE
                        WHEN worldops_consumer_effect.status='completed' THEN 'completed'
                        ELSE 'started'
                    END,
                    attempt_count=CASE
                        WHEN worldops_consumer_effect.status='completed'
                            THEN worldops_consumer_effect.attempt_count
                        ELSE worldops_consumer_effect.attempt_count+1
                    END,
                    last_error=CASE
                        WHEN worldops_consumer_effect.status='completed'
                            THEN worldops_consumer_effect.last_error
                        ELSE NULL
                    END,
                    updated_at=excluded.updated_at
                """,
                (source_event_id, effect_key, now, now),
            )
        effect = self.consumer_effect(source_event_id, effect_key)
        if effect is None:
            raise RuntimeError("consumer effect checkpoint was not created")
        return effect

    def complete_consumer_effect(
        self,
        source_event_id: str,
        effect_key: str,
        result: Mapping[str, Any],
    ) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE worldops_consumer_effect
                SET status='completed',result_json=?,last_error=NULL,updated_at=?
                WHERE source_event_id=? AND effect_key=?
                """,
                (json_dumps(dict(result)), now, source_event_id, effect_key),
            )
            if updated.rowcount != 1:
                raise KeyError(f"{source_event_id}:{effect_key}")
        effect = self.consumer_effect(source_event_id, effect_key)
        if effect is None:
            raise RuntimeError("consumer effect checkpoint disappeared")
        return effect

    def fail_consumer_effect(self, source_event_id: str, effect_key: str, error: str) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE worldops_consumer_effect
                SET status='failed',last_error=?,updated_at=?
                WHERE source_event_id=? AND effect_key=?
                """,
                (error[:2000], now, source_event_id, effect_key),
            )
            if updated.rowcount != 1:
                raise KeyError(f"{source_event_id}:{effect_key}")
        effect = self.consumer_effect(source_event_id, effect_key)
        if effect is None:
            raise RuntimeError("consumer effect checkpoint disappeared")
        return effect

    def mark_outbox_processed(self, event_id: str, *, consumer: str, result: Mapping[str, Any]) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM worldops_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["status"] == "processed":
                return self._outbox_from_row(row)
            self._conn.execute(
                "UPDATE worldops_outbox SET status='processed',processed_at=?,last_error=NULL WHERE event_id=?",
                (now, event_id),
            )
            partition = str(row["timeline_id"] or "global")
            self._conn.execute(
                """
                INSERT INTO worldops_checkpoint (
                    consumer,partition_key,last_event_id,processed_count,watermark,status,metadata_json,updated_at
                ) VALUES (?,?,?,1,?,'ok',?,?)
                ON CONFLICT(consumer,partition_key) DO UPDATE SET
                    last_event_id=excluded.last_event_id,
                    processed_count=worldops_checkpoint.processed_count+1,
                    watermark=excluded.watermark,status='ok',metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (consumer, partition, event_id, now, json_dumps(dict(result)), now),
            )
        return self.get_outbox(event_id)

    def mark_outbox_failed(self, event_id: str, error: str, *, retryable: bool = True) -> JsonDict:
        now_dt = datetime.now(timezone.utc)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM worldops_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            attempts = int(row["attempts"]) + 1
            permanent = not retryable or attempts >= int(row["max_attempts"])
            if permanent:
                status = "dead_letter"
                next_attempt = None
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO worldops_dead_letter (
                        dead_letter_id,source_event_id,source_kind,error_code,error,payload_json,
                        attempt_history_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("wodl"),
                        event_id,
                        "outbox",
                        "delivery_failed",
                        error[:2000],
                        row["payload_json"],
                        json_dumps([{"attempt": attempts, "error": error[:1000], "at": now_dt.isoformat()}]),
                        now_dt.isoformat(),
                    ),
                )
            else:
                status = "pending"
                delay = min(3600, 2 ** attempts) + (int(hashlib.sha256(event_id.encode()).hexdigest()[:4], 16) % 1000) / 1000
                next_attempt = datetime.fromtimestamp(now_dt.timestamp() + delay, timezone.utc).isoformat()
            self._conn.execute(
                """
                UPDATE worldops_outbox SET status=?,attempts=?,next_attempt_at=?,last_error=?,
                    processed_at=CASE WHEN ?='dead_letter' THEN ? ELSE NULL END
                WHERE event_id=?
                """,
                (status, attempts, next_attempt, error[:2000], status, now_dt.isoformat(), event_id),
            )
        return self.get_outbox(event_id)

    def get_outbox(self, event_id: str) -> JsonDict:
        row = self._read_connection().execute("SELECT * FROM worldops_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._outbox_from_row(row)

    def list_traces(self, *, limit: int = 100, correlation_id: str | None = None) -> list[JsonDict]:
        if correlation_id:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_trace WHERE correlation_id=? ORDER BY created_at DESC LIMIT ?",
                (correlation_id, max(1, min(limit, 1000))),
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_trace ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [{**dict(row), "details": json_loads(row["details_json"], {})} for row in rows]

    def record_trace(
        self,
        stage: str,
        status: str,
        details: Mapping[str, Any],
        *,
        latency_ms: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        bundle_id: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._trace_locked(
                stage,
                status,
                details,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
                causation_id=causation_id,
                bundle_id=bundle_id,
                instance_id=instance_id,
            )

    def record_metric(self, name: str, value: float, *, labels: Mapping[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO worldops_metric (metric_id,name,value,labels_json,created_at) VALUES (?,?,?,?,?)",
                (new_id("wom"), name, float(value), json_dumps(dict(labels or {})), utc_now()),
            )

    def rebuild(self, *, dry_run: bool = True) -> JsonDict:
        rows = self._read_connection().execute(
            """
            SELECT * FROM worldops_transition_ledger
            WHERE timeline_id=? ORDER BY timeline_revision,transition_id
            """,
            (self.identity.timeline_id,),
        ).fetchall()
        clock_rows = self._read_connection().execute(
            """
            SELECT * FROM worldops_clock_ledger
            WHERE timeline_id=? ORDER BY timeline_revision,clock_event_id
            """,
            (self.identity.timeline_id,),
        ).fetchall()
        replay: dict[str, JsonDict] = {}
        facts: JsonDict = {}
        revision = 0
        world_clock = 0.0
        actions = [(int(row["timeline_revision"]), "transition", row) for row in rows]
        actions.extend((int(row["timeline_revision"]), "clock", row) for row in clock_rows)
        actions.sort(key=lambda item: (item[0], item[1], item[2][0]))
        for action_revision, action_type, row in actions:
            if action_revision != revision + 1:
                raise WorldOpsConflictError("timeline ledger has a revision gap")
            revision = action_revision
            if action_type == "clock":
                world_clock += float(row["delta"])
                if abs(world_clock - float(row["world_clock"])) > 1e-9:
                    raise WorldOpsConflictError("world clock ledger is inconsistent")
                continue
            current = replay.setdefault(
                row["instance_id"],
                {"event_id": row["event_id"], "state": row["from_state"], "revision": 0},
            )
            if int(row["event_revision"]) != int(current["revision"]) + 1:
                raise WorldOpsConflictError("transition ledger has a revision gap")
            if current["state"] != row["from_state"]:
                raise WorldOpsConflictError("transition ledger state chain is inconsistent")
            current.update({"state": row["to_state"], "revision": int(row["event_revision"])})
            for effect in json_loads(row["effects_json"], []):
                if effect.get("op") == "set_fact":
                    facts[str(effect["key"])] = effect.get("value")
                elif effect.get("op") == "remove_fact":
                    facts.pop(str(effect["key"]), None)
        actual_events = {item["instance_id"]: item for item in self.list_event_instances() if int(item["revision"]) > 0}
        actual_projection = {
            key: {"event_id": value["event_id"], "state": value["state"], "revision": int(value["revision"])}
            for key, value in actual_events.items()
        }
        timeline = self.timeline()
        parity = (
            replay == actual_projection
            and facts == self.active_facts()
            and revision == int(timeline["revision"])
            and abs(world_clock - float(timeline["world_clock"])) <= 1e-9
        )
        result = {
            "dry_run": dry_run,
            "timeline_id": self.identity.timeline_id,
            "ledger_entries": len(rows),
            "clock_ledger_entries": len(clock_rows),
            "replayed_revision": revision,
            "replayed_world_clock": world_clock,
            "event_count": len(replay),
            "fact_count": len(facts),
            "parity": parity,
            "events": replay,
            "facts": facts,
        }
        if not dry_run and not parity:
            raise WorldOpsConflictError("automatic mutation from divergent ledger is intentionally blocked")
        return result

    def health(self) -> JsonDict:
        conn = self._read_connection()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for table in (
            "worldops_bundle_registry",
            "worldops_event_instance",
            "worldops_transition_ledger",
            "worldops_clock_ledger",
            "worldops_choice_ledger",
            "worldops_task_evidence",
            "worldops_outbox",
            "worldops_consumer_effect",
            "worldops_dead_letter",
            "worldops_trace",
        ):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        pending = int(conn.execute("SELECT COUNT(*) FROM worldops_outbox WHERE status='pending'").fetchone()[0])
        dead = int(conn.execute("SELECT COUNT(*) FROM worldops_outbox WHERE status='dead_letter'").fetchone()[0])
        incomplete_effects = int(
            conn.execute(
                "SELECT COUNT(*) FROM worldops_consumer_effect WHERE status!='completed'"
            ).fetchone()[0]
        )
        open_sync_conflicts = int(
            conn.execute(
                "SELECT COUNT(*) FROM worldops_sync_conflict WHERE timeline_id=? AND status='open'",
                (self.identity.timeline_id,),
            ).fetchone()[0]
        )
        return {
            "status": (
                "healthy"
                if integrity == "ok" and dead == 0 and incomplete_effects == 0
                else "degraded"
            ),
            "integrity": integrity,
            "identity": self.identity.to_dict(public=True),
            "active_bundle": self.identity_state().get("active_bundle_id"),
            "timeline_revision": int(self.timeline()["revision"]),
            "outbox": {
                "pending": pending,
                "dead_letter": dead,
                "incomplete_consumer_effects": incomplete_effects,
            },
            "open_sync_conflicts": open_sync_conflicts,
            "counts": counts,
        }

    def quality_snapshot(self) -> JsonDict:
        conn = self._read_connection()
        completed_without_evidence = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM worldops_event_instance e
                WHERE e.timeline_id=? AND e.state='completed'
                  AND NOT EXISTS (
                    SELECT 1 FROM worldops_task_evidence t
                    WHERE t.instance_id=e.instance_id AND t.accepted=1
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM worldops_choice_ledger c
                    WHERE c.instance_id=e.instance_id AND c.status='accepted' AND c.choice_id IS NOT NULL
                  )
                """,
                (self.identity.timeline_id,),
            ).fetchone()[0]
        )
        duplicate_inputs = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT input_id,COUNT(*) AS n FROM worldops_choice_ledger
                    WHERE timeline_id=? GROUP BY input_id HAVING n>1
                )
                """,
                (self.identity.timeline_id,),
            ).fetchone()[0]
        )
        open_dead_letters = int(
            conn.execute("SELECT COUNT(*) FROM worldops_dead_letter WHERE status='open'").fetchone()[0]
        )
        incomplete_effects = int(
            conn.execute(
                "SELECT COUNT(*) FROM worldops_consumer_effect WHERE status!='completed'"
            ).fetchone()[0]
        )
        open_sync_conflicts = int(
            conn.execute(
                "SELECT COUNT(*) FROM worldops_sync_conflict WHERE timeline_id=? AND status='open'",
                (self.identity.timeline_id,),
            ).fetchone()[0]
        )
        return {
            "completed_without_user_evidence": completed_without_evidence,
            "duplicate_choice_inputs": duplicate_inputs,
            "open_dead_letters": open_dead_letters,
            "incomplete_consumer_effects": incomplete_effects,
            "open_sync_conflicts": open_sync_conflicts,
            "rebuild": self.rebuild(dry_run=True),
            "health": self.health(),
        }

    def list_dead_letters(self, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        if status:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_dead_letter WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, max(1, min(limit, 1000))),
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_dead_letter ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        result: list[JsonDict] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json_loads(row["payload_json"], {})
            item["attempt_history"] = json_loads(row["attempt_history_json"], [])
            item.pop("payload_json", None)
            item.pop("attempt_history_json", None)
            result.append(item)
        return result

    def resolve_dead_letter(self, dead_letter_id: str, *, resolution: str) -> JsonDict:
        if resolution not in {"requeue", "discard", "exported"}:
            raise ValueError("invalid dead-letter resolution")
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM worldops_dead_letter WHERE dead_letter_id=?",
                (dead_letter_id,),
            ).fetchone()
            if row is None:
                raise KeyError(dead_letter_id)
            if resolution == "requeue":
                self._conn.execute(
                    "UPDATE worldops_outbox SET status='pending',attempts=0,next_attempt_at=NULL,last_error=NULL,processed_at=NULL WHERE event_id=?",
                    (row["source_event_id"],),
                )
            self._conn.execute(
                "UPDATE worldops_dead_letter SET status='resolved',resolution=?,resolved_at=? WHERE dead_letter_id=?",
                (resolution, now, dead_letter_id),
            )
        return {"dead_letter_id": dead_letter_id, "status": "resolved", "resolution": resolution}

    def list_metrics(self, *, name: str | None = None, limit: int = 1000) -> list[JsonDict]:
        if name:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_metric WHERE name=? ORDER BY created_at DESC LIMIT ?",
                (name, max(1, min(limit, 10000))),
            ).fetchall()
        else:
            rows = self._read_connection().execute(
                "SELECT * FROM worldops_metric ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 10000)),),
            ).fetchall()
        return [dict(row) | {"labels": json_loads(row["labels_json"], {})} for row in rows]

    def export_prometheus_metrics(self, *, limit: int = 10000) -> str:
        return prometheus_text(self.list_metrics(limit=limit))

    def cleanup_observability(
        self,
        *,
        trace_days: int = 30,
        metric_days: int = 90,
        processed_outbox_days: int = 30,
    ) -> JsonDict:
        if min(trace_days, metric_days, processed_outbox_days) < 1:
            raise ValueError("WorldOps retention windows must be at least one day")
        now = datetime.now(timezone.utc)
        trace_cutoff = (now - timedelta(days=trace_days)).isoformat()
        metric_cutoff = (now - timedelta(days=metric_days)).isoformat()
        outbox_cutoff = (now - timedelta(days=processed_outbox_days)).isoformat()
        with self._lock, self._conn:
            trace_count = self._conn.execute(
                "DELETE FROM worldops_trace WHERE created_at<?",
                (trace_cutoff,),
            ).rowcount
            metric_count = self._conn.execute(
                "DELETE FROM worldops_metric WHERE created_at<?",
                (metric_cutoff,),
            ).rowcount
            effect_count = self._conn.execute(
                """
                DELETE FROM worldops_consumer_effect
                WHERE source_event_id IN (
                    SELECT event_id FROM worldops_outbox
                    WHERE status='processed' AND processed_at<?
                )
                """,
                (outbox_cutoff,),
            ).rowcount
            outbox_count = self._conn.execute(
                "DELETE FROM worldops_outbox WHERE status='processed' AND processed_at<?",
                (outbox_cutoff,),
            ).rowcount
        return {
            "deleted": {
                "traces": trace_count,
                "metrics": metric_count,
                "consumer_effects": effect_count,
                "processed_outbox": outbox_count,
            },
            "retention_days": {
                "traces": trace_days,
                "metrics": metric_days,
                "processed_outbox": processed_outbox_days,
            },
        }

    @staticmethod
    def _transition_is_authored(
        definition: Mapping[str, Any],
        *,
        from_state: str,
        to_state: str,
        trigger_type: str,
        choice_id: str | None,
    ) -> bool:
        return WorldOpsStore._authored_transition(
            definition,
            from_state=from_state,
            to_state=to_state,
            trigger_type=trigger_type,
            choice_id=choice_id,
        ) is not None

    @staticmethod
    def _authored_transition(
        definition: Mapping[str, Any],
        *,
        from_state: str,
        to_state: str,
        trigger_type: str,
        choice_id: str | None,
    ) -> Mapping[str, Any] | None:
        if trigger_type in {"migration", "break_glass_recovery"}:
            return {"from": from_state, "to": to_state, "trigger": trigger_type, "effects": []}
        authored_trigger = TRANSITION_TRIGGER_ALIASES.get(trigger_type, trigger_type)
        for transition in definition.get("transitions") or []:
            if (
                str(transition.get("from")) != from_state
                or str(transition.get("to")) != to_state
                or str(transition.get("trigger")) != authored_trigger
            ):
                continue
            choices = {str(item) for item in transition.get("choice_ids") or []}
            if not choices or choice_id in choices:
                return transition
        return None

    def _ensure_event_instance_locked(self, event: Mapping[str, Any], *, bundle_id: str, now: str) -> None:
        event_id = str(event["event_id"])
        version = int(event.get("definition_version", 1))
        self._conn.execute(
            """
            INSERT OR IGNORE INTO worldops_event_instance (
                instance_id,timeline_id,event_id,definition_version,bundle_id,state,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                new_id("woi"),
                self.identity.timeline_id,
                event_id,
                version,
                bundle_id,
                str(event.get("initial_state") or "dormant"),
                now,
                now,
            ),
        )

    def _apply_effect_locked(
        self,
        effect: Mapping[str, Any],
        *,
        timeline_revision: int,
        instance_id: str,
        transition_id: str,
        now: str,
    ) -> None:
        try:
            validated = self.effect_registry.validate(effect)
        except ValueError as exc:
            raise WorldOpsContractError(str(exc)) from exc
        op = str(validated["op"])
        if op == "emit_signal":
            self._enqueue_locked(
                "worldops.effect.signal.v1",
                event_key=f"effect-signal:{transition_id}:{validated['signal_type']}",
                payload={
                    "signal_type": validated["signal_type"],
                    "payload": dict(validated["payload"]),
                    "instance_id": instance_id,
                    "transition_id": transition_id,
                    "timeline_revision": timeline_revision,
                },
                correlation_id=None,
                causation_id=transition_id,
            )
            return
        key = str(validated["key"])
        self._conn.execute(
            """
            UPDATE worldops_timeline_fact SET valid_to_revision=?
            WHERE timeline_id=? AND fact_key=? AND valid_to_revision IS NULL
            """,
            (timeline_revision, self.identity.timeline_id, key),
        )
        if op == "set_fact":
            self._conn.execute(
                """
                INSERT INTO worldops_timeline_fact (
                    fact_revision_id,timeline_id,fact_key,fact_value_json,source_instance_id,
                    source_transition_id,valid_from_revision,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("wof"),
                    self.identity.timeline_id,
                    key,
                    json_dumps(validated.get("value")),
                    instance_id,
                    transition_id,
                    timeline_revision,
                    now,
                ),
            )

    def _enqueue_locked(
        self,
        event_type: str,
        *,
        event_key: str,
        payload: Mapping[str, Any],
        correlation_id: str | None,
        causation_id: str | None,
    ) -> JsonDict:
        event_id = new_id("woevt")
        envelope = make_event_envelope(
            event_type,
            subject={
                "world_id": self.world_id,
                "timeline_id": self.identity.timeline_id,
                "sora_id": self.identity.sora_id,
            },
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            producer="worldops-client-runtime",
            event_id=event_id,
            idempotency_key=event_key,
            privacy_class="private_local",
        )
        payload_hash = content_hash(envelope)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO worldops_outbox (
                event_id,timeline_id,event_type,event_key,payload_hash,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                event_id,
                self.identity.timeline_id,
                event_type,
                event_key,
                payload_hash,
                json_dumps(envelope),
                utc_now(),
            ),
        )
        return envelope

    def _trace_locked(
        self,
        stage: str,
        status: str,
        details: Mapping[str, Any],
        *,
        latency_ms: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        bundle_id: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        created_at = utc_now()
        trace_id = new_id("wotr")
        self._conn.execute(
            """
            INSERT INTO worldops_trace (
                trace_id,timeline_id,correlation_id,causation_id,stage,status,bundle_id,
                event_instance_id,details_json,latency_ms,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trace_id,
                self.identity.timeline_id,
                correlation_id,
                causation_id,
                stage,
                status,
                bundle_id,
                instance_id,
                json_dumps(dict(details)),
                latency_ms,
                created_at,
            ),
        )
        if self.trace_sink is not None:
            try:
                self.trace_sink.emit(
                    {
                        "trace_id": trace_id,
                        "subsystem": "worldops",
                        "timeline_id": self.identity.timeline_id,
                        "correlation_id": correlation_id,
                        "causation_id": causation_id,
                        "stage": stage,
                        "status": status,
                        "bundle_id": bundle_id,
                        "event_instance_id": instance_id,
                        "latency_ms": latency_ms,
                        "details": dict(details),
                        "created_at": created_at,
                    }
                )
            except Exception:
                pass

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["metadata"] = json_loads(row["metadata_json"], {})
        return result

    @staticmethod
    def _choice_from_row(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["interpretation"] = json_loads(row["interpretation_json"], {})
        result["evidence_refs"] = json_loads(row["evidence_refs_json"], [])
        return result

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["payload"] = json_loads(row["payload_json"], {})
        result["accepted"] = bool(row["accepted"])
        result["consumed"] = bool(row["consumed"])
        return result

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["payload"] = json_loads(row["payload_json"], {})
        return result


def _action_origin_id(action: Mapping[str, Any]) -> str:
    supplied = str(action.get("origin_action_id") or "").strip()
    if supplied:
        return supplied
    return "legacy." + content_hash(_action_fingerprint(action))[:40]


def _action_fingerprint(action: Mapping[str, Any]) -> JsonDict:
    payload = dict(action)
    for key in ("timeline_revision", "world_clock", "origin_action_id"):
        payload.pop(key, None)
    return payload
