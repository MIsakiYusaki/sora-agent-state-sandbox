from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    ModelDescriptor,
    OwnerScope,
    ProfileStatus,
    ProviderEndpoint,
    ProviderKind,
    ProviderProfile,
    ProviderProtocol,
    Workload,
    WorkloadBinding,
    utc_now,
)
from .errors import ModelProviderError, ProviderErrorCode, RevisionConflict


class ProviderRegistry:
    """SQLite metadata source of truth. Secret material is intentionally absent."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_provider_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_profile (
                    profile_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    provider_kind TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    endpoint_json TEXT NOT NULL,
                    credential_ref TEXT,
                    auth_scheme TEXT NOT NULL,
                    status TEXT NOT NULL,
                    capability_revision INTEGER NOT NULL,
                    policy_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_provider_profile_owner
                    ON provider_profile(tenant_id, user_id, agent_id, status);

                CREATE TABLE IF NOT EXISTS provider_model (
                    provider_profile_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    descriptor_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    verified_at TEXT,
                    expires_at TEXT,
                    PRIMARY KEY(provider_profile_id, model_id),
                    FOREIGN KEY(provider_profile_id) REFERENCES provider_profile(profile_id)
                );

                CREATE TABLE IF NOT EXISTS workload_binding (
                    binding_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    workload TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(tenant_id, user_id, agent_id, workload)
                );

                CREATE TABLE IF NOT EXISTS provider_probe (
                    probe_id TEXT PRIMARY KEY,
                    provider_profile_id TEXT NOT NULL,
                    model_id TEXT,
                    probe_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(provider_profile_id) REFERENCES provider_profile(profile_id)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_probe_profile
                    ON provider_probe(provider_profile_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS provider_invocation_trace (
                    invocation_id TEXT PRIMARY KEY,
                    correlation_id TEXT,
                    workload TEXT NOT NULL,
                    provider_profile_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    routing_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    fallback_ordinal INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    provider_request_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(provider_profile_id) REFERENCES provider_profile(profile_id)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_trace_created
                    ON provider_invocation_trace(created_at);
                CREATE INDEX IF NOT EXISTS idx_provider_trace_workload
                    ON provider_invocation_trace(workload, provider_profile_id, model_id);

                CREATE TABLE IF NOT EXISTS provider_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO model_provider_meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO model_provider_meta(key, value) VALUES('registry_revision', '0')"
            )

    @property
    def revision(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM model_provider_meta WHERE key='registry_revision'"
            ).fetchone()
        return int(row["value"] if row else 0)

    def create_profile(self, profile: ProviderProfile, *, actor: str = "system") -> ProviderProfile:
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM provider_profile WHERE profile_id=?", (profile.profile_id,)
            ).fetchone():
                raise ValueError(f"provider profile already exists: {profile.profile_id}")
            self._conn.execute(
                """
                INSERT INTO provider_profile(
                    profile_id, tenant_id, user_id, agent_id, display_name,
                    provider_kind, protocol, endpoint_json, credential_ref,
                    auth_scheme, status, capability_revision, policy_revision,
                    created_at, updated_at, deleted_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    profile.profile_id,
                    profile.owner.tenant_id,
                    profile.owner.user_id,
                    profile.owner.agent_id,
                    profile.display_name,
                    profile.provider_kind.value,
                    profile.protocol.value,
                    profile.endpoint.model_dump_json(),
                    profile.credential_ref,
                    profile.auth_scheme,
                    profile.status.value,
                    profile.capability_revision,
                    profile.policy_revision,
                    profile.created_at,
                    profile.updated_at,
                ),
            )
            self._bump_revision()
            self._audit("profile.create", "provider_profile", profile.profile_id, actor, {})
        return self.get_profile(profile.profile_id)

    def get_profile(self, profile_id: str, *, include_deleted: bool = False) -> ProviderProfile:
        with self._lock:
            query = "SELECT * FROM provider_profile WHERE profile_id=?"
            params: tuple[Any, ...] = (profile_id,)
            if not include_deleted:
                query += " AND deleted_at IS NULL"
            row = self._conn.execute(query, params).fetchone()
        if row is None:
            raise ModelProviderError(
                ProviderErrorCode.PROFILE_NOT_FOUND,
                f"provider profile not found: {profile_id}",
            )
        return self._profile_from_row(row)

    def list_profiles(
        self,
        owner: OwnerScope,
        *,
        include_deleted: bool = False,
    ) -> list[ProviderProfile]:
        query = """
            SELECT * FROM provider_profile
            WHERE tenant_id=? AND user_id=? AND agent_id=?
        """
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY created_at, profile_id"
        with self._lock:
            rows = self._conn.execute(query, self._owner_params(owner)).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def bind_credential(
        self,
        profile_id: str,
        secret_ref: str,
        *,
        expected_policy_revision: int | None = None,
        actor: str = "system",
    ) -> ProviderProfile:
        profile = self.get_profile(profile_id)
        self._check_policy_revision(profile, expected_policy_revision)
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE provider_profile
                SET credential_ref=?, status=?, policy_revision=policy_revision+1, updated_at=?
                WHERE profile_id=? AND deleted_at IS NULL
                """,
                (secret_ref, ProfileStatus.CREDENTIAL_BOUND.value, now, profile_id),
            )
            self._bump_revision()
            self._audit("credential.bind", "provider_profile", profile_id, actor, {})
        return self.get_profile(profile_id)

    def set_profile_status(
        self,
        profile_id: str,
        status: ProfileStatus,
        *,
        actor: str = "system",
        detail: dict[str, Any] | None = None,
    ) -> ProviderProfile:
        self.get_profile(profile_id)
        if status == ProfileStatus.DELETED:
            raise ValueError("use delete_profile for the deleted state")
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE provider_profile
                SET status=?, policy_revision=policy_revision+1, updated_at=?
                WHERE profile_id=? AND deleted_at IS NULL
                """,
                (status.value, now, profile_id),
            )
            self._bump_revision()
            self._audit("profile.status", "provider_profile", profile_id, actor, detail or {"status": status.value})
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: str, *, actor: str = "user") -> None:
        self.get_profile(profile_id)
        with self._lock, self._conn:
            active = self._conn.execute(
                "SELECT COUNT(*) AS count FROM workload_binding WHERE active=1 AND binding_json LIKE ?",
                (f'%"provider_profile_id":"{profile_id}"%',),
            ).fetchone()
            if int(active["count"] or 0):
                raise ValueError("provider profile is still referenced by an active workload binding")
            now = utc_now()
            self._conn.execute(
                """
                UPDATE provider_profile
                SET status=?, credential_ref=NULL, policy_revision=policy_revision+1,
                    updated_at=?, deleted_at=?
                WHERE profile_id=?
                """,
                (ProfileStatus.DELETED.value, now, now, profile_id),
            )
            self._bump_revision()
            self._audit("profile.delete", "provider_profile", profile_id, actor, {})

    def purge_owner(self, owner: OwnerScope) -> dict[str, int]:
        """Offline privacy erasure. Shared-registry rows of other owners survive."""
        with self._lock, self._conn:
            if self._conn.in_transaction:
                raise ValueError("provider registry transaction is busy")
            self._conn.execute("BEGIN IMMEDIATE")
            scope = (owner.tenant_id, owner.user_id, owner.agent_id)
            profiles = self._conn.execute("SELECT profile_id FROM provider_profile WHERE tenant_id=? AND user_id=? AND agent_id=?", scope).fetchall()
            bindings = self._conn.execute("SELECT binding_id FROM workload_binding WHERE tenant_id=? AND user_id=? AND agent_id=?", scope).fetchall()
            for binding in bindings:
                self._conn.execute("DELETE FROM provider_audit WHERE object_type='workload_binding' AND object_id=?", (binding["binding_id"],))
            self._conn.execute("DELETE FROM workload_binding WHERE tenant_id=? AND user_id=? AND agent_id=?", scope)
            for row in profiles:
                identifier = row["profile_id"]
                self._conn.execute("DELETE FROM provider_audit WHERE (object_type='provider_profile' AND object_id=?) OR (object_type='provider_model' AND substr(object_id,1,?)=?)", (identifier, len(identifier) + 1, identifier + ":"))
                for table in ("provider_invocation_trace", "provider_probe", "provider_model"):
                    self._conn.execute(f"DELETE FROM {table} WHERE provider_profile_id=?", (identifier,))
                self._conn.execute("DELETE FROM provider_profile WHERE profile_id=?", (identifier,))
            self._bump_revision()
        return {"provider_profiles": len(profiles), "workload_bindings": len(bindings)}

    def upsert_model(self, descriptor: ModelDescriptor, *, actor: str = "probe") -> ModelDescriptor:
        self.get_profile(descriptor.provider_profile_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT revision FROM provider_model WHERE provider_profile_id=? AND model_id=?",
                (descriptor.provider_profile_id, descriptor.model_id),
            ).fetchone()
            revision = int(row["revision"] or 0) + 1 if row else 1
            updated = descriptor.model_copy(update={"revision": revision})
            self._conn.execute(
                """
                INSERT INTO provider_model(
                    provider_profile_id, model_id, descriptor_json, revision,
                    verified_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_profile_id, model_id) DO UPDATE SET
                    descriptor_json=excluded.descriptor_json,
                    revision=excluded.revision,
                    verified_at=excluded.verified_at,
                    expires_at=excluded.expires_at
                """,
                (
                    updated.provider_profile_id,
                    updated.model_id,
                    updated.model_dump_json(),
                    revision,
                    updated.verified_at,
                    updated.expires_at,
                ),
            )
            self._conn.execute(
                """
                UPDATE provider_profile
                SET capability_revision=capability_revision+1, updated_at=?
                WHERE profile_id=?
                """,
                (utc_now(), descriptor.provider_profile_id),
            )
            self._bump_revision()
            self._audit(
                "model.upsert",
                "provider_model",
                f"{descriptor.provider_profile_id}:{descriptor.model_id}",
                actor,
                {"source": descriptor.source, "revision": revision},
            )
        return self.get_model(descriptor.provider_profile_id, descriptor.model_id)

    def get_model(self, profile_id: str, model_id: str) -> ModelDescriptor:
        with self._lock:
            row = self._conn.execute(
                "SELECT descriptor_json FROM provider_model WHERE provider_profile_id=? AND model_id=?",
                (profile_id, model_id),
            ).fetchone()
        if row is None:
            raise ModelProviderError(
                ProviderErrorCode.MODEL_NOT_FOUND,
                f"provider model not found: {profile_id}/{model_id}",
            )
        return ModelDescriptor.model_validate_json(row["descriptor_json"])

    def list_models(self, profile_id: str) -> list[ModelDescriptor]:
        self.get_profile(profile_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT descriptor_json FROM provider_model
                WHERE provider_profile_id=? ORDER BY model_id
                """,
                (profile_id,),
            ).fetchall()
        return [ModelDescriptor.model_validate_json(row["descriptor_json"]) for row in rows]

    def put_binding(
        self,
        binding: WorkloadBinding,
        *,
        expected_revision: int | None = None,
        actor: str = "user",
    ) -> WorkloadBinding:
        existing = self.get_binding(binding.owner, binding.workload, required=False)
        if expected_revision is not None:
            actual = existing.revision if existing else 0
            if expected_revision != actual:
                raise RevisionConflict(
                    f"workload binding revision changed: expected {expected_revision}, actual {actual}"
                )
        revision = (existing.revision + 1) if existing else 1
        created_at = existing.created_at if existing else binding.created_at
        updated = binding.model_copy(
            update={"revision": revision, "created_at": created_at, "updated_at": utc_now()}
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO workload_binding(
                    binding_id, tenant_id, user_id, agent_id, workload,
                    binding_json, revision, created_at, updated_at, active
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(tenant_id, user_id, agent_id, workload) DO UPDATE SET
                    binding_id=excluded.binding_id,
                    binding_json=excluded.binding_json,
                    revision=excluded.revision,
                    updated_at=excluded.updated_at,
                    active=1
                """,
                (
                    updated.binding_id,
                    updated.owner.tenant_id,
                    updated.owner.user_id,
                    updated.owner.agent_id,
                    updated.workload.value,
                    updated.model_dump_json(),
                    updated.revision,
                    updated.created_at,
                    updated.updated_at,
                ),
            )
            self._bump_revision()
            self._audit(
                "binding.put",
                "workload_binding",
                updated.binding_id,
                actor,
                {
                    "workload": updated.workload.value,
                    "revision": updated.revision,
                    "cross_provider_fallback": updated.cross_provider_fallback,
                },
            )
        return self.get_binding(updated.owner, updated.workload)

    def get_binding(
        self,
        owner: OwnerScope,
        workload: Workload,
        *,
        required: bool = True,
    ) -> WorkloadBinding | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT binding_json FROM workload_binding
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND workload=? AND active=1
                """,
                (*self._owner_params(owner), workload.value),
            ).fetchone()
        if row is None:
            if required:
                raise ModelProviderError(
                    ProviderErrorCode.BINDING_NOT_FOUND,
                    f"no provider binding exists for workload: {workload.value}",
                )
            return None
        return WorkloadBinding.model_validate_json(row["binding_json"])

    def list_bindings(self, owner: OwnerScope) -> list[WorkloadBinding]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT binding_json FROM workload_binding
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND active=1
                ORDER BY workload
                """,
                self._owner_params(owner),
            ).fetchall()
        return [WorkloadBinding.model_validate_json(row["binding_json"]) for row in rows]

    def remove_binding(self, owner: OwnerScope, workload: Workload, *, actor: str = "user") -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT binding_id FROM workload_binding
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND workload=? AND active=1
                """,
                (*self._owner_params(owner), workload.value),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                """
                UPDATE workload_binding SET active=0, updated_at=?
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND workload=?
                """,
                (utc_now(), *self._owner_params(owner), workload.value),
            )
            self._bump_revision()
            self._audit("binding.remove", "workload_binding", row["binding_id"], actor, {})

    def record_probe(
        self,
        *,
        probe_id: str,
        profile_id: str,
        model_id: str | None,
        probe_kind: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        safe_result = self._redact_mapping(result)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO provider_probe(
                    probe_id, provider_profile_id, model_id, probe_kind,
                    status, result_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    probe_id,
                    profile_id,
                    model_id,
                    probe_kind,
                    status,
                    json.dumps(safe_result, separators=(",", ":"), sort_keys=True),
                    utc_now(),
                ),
            )

    def record_invocation(self, trace: dict[str, Any]) -> None:
        allowed = {
            "invocation_id",
            "correlation_id",
            "workload",
            "provider_profile_id",
            "model_id",
            "routing_revision",
            "status",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "retry_count",
            "fallback_ordinal",
            "error_code",
            "provider_request_id",
        }
        unknown = set(trace) - allowed
        if unknown:
            raise ValueError(f"provider invocation trace contains forbidden fields: {sorted(unknown)}")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO provider_invocation_trace(
                    invocation_id, correlation_id, workload, provider_profile_id,
                    model_id, routing_revision, status, latency_ms, prompt_tokens,
                    completion_tokens, retry_count, fallback_ordinal, error_code,
                    provider_request_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace["invocation_id"],
                    trace.get("correlation_id"),
                    trace["workload"],
                    trace["provider_profile_id"],
                    trace["model_id"],
                    int(trace["routing_revision"]),
                    trace["status"],
                    trace.get("latency_ms"),
                    trace.get("prompt_tokens"),
                    trace.get("completion_tokens"),
                    int(trace.get("retry_count") or 0),
                    int(trace.get("fallback_ordinal") or 0),
                    trace.get("error_code"),
                    trace.get("provider_request_id"),
                    utc_now(),
                ),
            )

    def export_redacted(self, owner: OwnerScope) -> dict[str, Any]:
        profiles = []
        for profile in self.list_profiles(owner):
            payload = profile.model_dump(mode="json")
            payload["credential_ref"] = "bound" if profile.credential_ref else None
            profiles.append(payload)
        return {
            "schema_version": "ModelProviderExport.v1",
            "registry_revision": self.revision,
            "profiles": profiles,
            "models": {
                profile.profile_id: [model.model_dump(mode="json") for model in self.list_models(profile.profile_id)]
                for profile in self.list_profiles(owner)
            },
            "bindings": [binding.model_dump(mode="json") for binding in self.list_bindings(owner)],
            "secrets_included": False,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            journal_mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = int(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
            profile_count = int(self._conn.execute(
                "SELECT COUNT(*) FROM provider_profile WHERE deleted_at IS NULL"
            ).fetchone()[0])
        return {
            "status": "ok",
            "schema_version": self.SCHEMA_VERSION,
            "registry_revision": self.revision,
            "journal_mode": str(journal_mode).lower(),
            "foreign_keys": bool(foreign_keys),
            "profile_count": profile_count,
        }

    def maintain(
        self,
        *,
        invocation_retention_days: int = 30,
        probe_retention_days: int = 90,
        audit_retention_days: int = 180,
    ) -> dict[str, int]:
        values = {
            "invocation_retention_days": invocation_retention_days,
            "probe_retention_days": probe_retention_days,
            "audit_retention_days": audit_retention_days,
        }
        if any(value < 1 for value in values.values()):
            raise ValueError("provider retention days must be positive")
        now = datetime.now(timezone.utc)
        cutoffs = {
            "provider_invocation_trace": (now - timedelta(days=invocation_retention_days)).isoformat(),
            "provider_probe": (now - timedelta(days=probe_retention_days)).isoformat(),
            "provider_audit": (now - timedelta(days=audit_retention_days)).isoformat(),
        }
        deleted: dict[str, int] = {}
        with self._lock, self._conn:
            for table, cutoff in cutoffs.items():
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE created_at < ?",  # table names are fixed above.
                    (cutoff,),
                )
                deleted[table] = max(0, int(cursor.rowcount or 0))
        return deleted

    def _bump_revision(self) -> int:
        self._conn.execute(
            """
            UPDATE model_provider_meta
            SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            WHERE key='registry_revision'
            """
        )
        row = self._conn.execute(
            "SELECT value FROM model_provider_meta WHERE key='registry_revision'"
        ).fetchone()
        return int(row["value"])

    def _audit(
        self,
        action: str,
        object_type: str,
        object_id: str,
        actor: str,
        detail: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO provider_audit(action, object_type, object_id, actor, detail_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                object_type,
                object_id,
                actor,
                json.dumps(self._redact_mapping(detail), separators=(",", ":"), sort_keys=True),
                utc_now(),
            ),
        )

    @staticmethod
    def _owner_params(owner: OwnerScope) -> tuple[str, str, str]:
        return owner.tenant_id, owner.user_id, owner.agent_id

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> ProviderProfile:
        return ProviderProfile(
            profile_id=row["profile_id"],
            owner=OwnerScope(
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
            ),
            display_name=row["display_name"],
            provider_kind=ProviderKind(row["provider_kind"]),
            protocol=ProviderProtocol(row["protocol"]),
            endpoint=ProviderEndpoint.model_validate_json(row["endpoint_json"]),
            credential_ref=row["credential_ref"],
            auth_scheme=row["auth_scheme"],
            status=ProfileStatus(row["status"]),
            capability_revision=int(row["capability_revision"]),
            policy_revision=int(row["policy_revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _check_policy_revision(profile: ProviderProfile, expected: int | None) -> None:
        if expected is not None and profile.policy_revision != expected:
            raise RevisionConflict(
                f"provider profile revision changed: expected {expected}, actual {profile.policy_revision}"
            )

    @staticmethod
    def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
        forbidden_fragments = ("api_key", "authorization", "secret", "token", "password", "prompt", "content")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if any(fragment in key.lower() for fragment in forbidden_fragments):
                continue
            if isinstance(item, dict):
                result[key] = ProviderRegistry._redact_mapping(item)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[key] = item
            elif isinstance(item, list):
                result[key] = [
                    ProviderRegistry._redact_mapping(entry) if isinstance(entry, dict) else entry
                    for entry in item
                ]
        return result
