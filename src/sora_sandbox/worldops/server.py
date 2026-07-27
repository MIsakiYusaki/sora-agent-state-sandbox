from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from ..memory_schema import json_dumps, json_loads, new_id
from .compiler import CanonCompiler, verify_compiled_bundle
from .contracts import (
    CONTRIBUTION_STATUSES,
    EDITORIAL_DECISIONS,
    NARRATIVE_CONTRIBUTION_CONTRACT,
    JsonDict,
    WorldOpsConflictError,
    WorldOpsContractError,
    WorldBundleManifest,
    content_hash,
    utc_now,
    validate_content_bundle,
)
from .schema import WORLDOPS_SERVER_SCHEMA_SQL


ROLE_ACTIONS = {
    "writer": {"draft.write", "draft.read", "simulate"},
    "editor": {"draft.read", "contribution.read", "contribution.decide", "simulate"},
    "canon_owner": {"draft.read", "changeset.approve", "release.create", "simulate"},
    "release_manager": {"release.promote", "release.revoke", "release.read"},
    "security_admin": {"release.revoke", "audit.read", "contribution.quarantine"},
}

FORBIDDEN_RAW_PATTERNS = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)"),
    re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_release_gate(compiled: JsonDict) -> JsonDict:
    """Fail closed on malformed, unsigned, or lint-failing release artifacts."""
    errors: list[str] = []
    try:
        bundle = validate_content_bundle(dict(compiled.get("bundle") or {}))
        manifest = WorldBundleManifest.from_dict(dict(compiled.get("manifest") or {}))
        if content_hash(bundle) != manifest.content_hash:
            errors.append("content_hash_mismatch")
        if not manifest.signature:
            errors.append("signature_missing")
    except (TypeError, ValueError, WorldOpsContractError) as exc:
        errors.append(f"contract_validation_failed:{exc}")
    report = dict(compiled.get("compile_report") or {})
    errors.extend(f"compiler:{item}" for item in report.get("errors") or [])
    return {
        "status": "failed" if errors else "passed",
        "run_id": "wogate." + content_hash({"errors": errors, "manifest": compiled.get("manifest")})[:24],
        "errors": errors,
        "warnings": list(report.get("warnings") or []),
    }


class CanonServerStore:
    """Server-owned canonical ledger, release registry, and editorial queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(WORLDOPS_SERVER_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        with self._lock, self._conn:
            for table in (
                "release_audit",
                "editorial_decision",
                "narrative_contribution",
                "content_bundle",
                "content_release",
                "canon_change_set",
                "canon_statement_ledger",
                "canon_object_revision",
            ):
                self._conn.execute(f"DELETE FROM {table}")

    def put_object(
        self,
        object_id: str,
        object_type: str,
        payload: Mapping[str, Any],
        *,
        branch: str,
        expected_revision: int,
        author_id: str,
    ) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT MAX(revision) AS revision FROM canon_object_revision WHERE object_id=? AND branch=?",
                (object_id, branch),
            ).fetchone()
            actual = int(current["revision"] or 0)
            if actual != expected_revision:
                raise WorldOpsConflictError(f"expected object revision {expected_revision}, found {actual}")
            revision = actual + 1
            body = dict(payload)
            self._conn.execute(
                """
                INSERT INTO canon_object_revision (
                    object_id,object_type,revision,branch,status,payload_json,payload_hash,author_id,created_at
                ) VALUES (?,?,?,?,'draft',?,?,?,?)
                """,
                (object_id, object_type, revision, branch, json_dumps(body), content_hash(body), author_id, now),
            )
            self._audit_locked(
                "draft.write", author_id, "writer", object_type, object_id, {"branch": branch, "revision": revision}
            )
        return {"object_id": object_id, "object_type": object_type, "branch": branch, "revision": revision, "payload": body}

    def branch_snapshot(self, branch: str) -> JsonDict:
        rows = self._conn.execute(
            """
            SELECT r.* FROM canon_object_revision r
            JOIN (
                SELECT object_id,MAX(revision) AS revision FROM canon_object_revision
                WHERE branch=? GROUP BY object_id
            ) latest ON latest.object_id=r.object_id AND latest.revision=r.revision
            WHERE r.branch=? AND r.status!='deprecated'
            ORDER BY r.object_type,r.object_id
            """,
            (branch, branch),
        ).fetchall()
        grouped: dict[str, list[JsonDict]] = {}
        max_revision = 0
        for row in rows:
            grouped.setdefault(str(row["object_type"]), []).append(json_loads(row["payload_json"], {}))
            max_revision = max(max_revision, int(row["revision"]))
        return {"branch": branch, "revision": max_revision, "objects": grouped}

    def create_change_set(
        self,
        *,
        world_id: str,
        branch: str,
        changes: list[JsonDict],
        impact_report: Mapping[str, Any],
        migration_plan: Mapping[str, Any],
        rollback_plan: Mapping[str, Any],
        author_id: str,
    ) -> JsonDict:
        if not changes:
            raise WorldOpsContractError("change set must contain at least one change")
        if not migration_plan or not rollback_plan:
            raise WorldOpsContractError("change set requires migration and rollback plans")
        snapshot = self.branch_snapshot(branch)
        change_set_id = new_id("wocs")
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO canon_change_set (
                    change_set_id,world_id,branch,base_revision,target_revision,changes_json,
                    impact_report_json,migration_plan_json,rollback_plan_json,status,author_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)
                """,
                (
                    change_set_id,
                    world_id,
                    branch,
                    int(snapshot["revision"]),
                    int(snapshot["revision"]) + 1,
                    json_dumps(changes),
                    json_dumps(dict(impact_report)),
                    json_dumps(dict(migration_plan)),
                    json_dumps(dict(rollback_plan)),
                    author_id,
                    now,
                ),
            )
            self._audit_locked(
                "changeset.create",
                author_id,
                "writer",
                "change_set",
                change_set_id,
                {"branch": branch, "change_count": len(changes)},
            )
        return self.get_change_set(change_set_id)

    def approve_change_set(self, change_set_id: str, *, actor_id: str) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM canon_change_set WHERE change_set_id=?",
                (change_set_id,),
            ).fetchone()
            if row is None:
                raise KeyError(change_set_id)
            if row["status"] not in {"draft", "approved"}:
                raise WorldOpsConflictError("change set is not approvable")
            self._conn.execute(
                "UPDATE canon_change_set SET status='approved',approved_at=? WHERE change_set_id=?",
                (now, change_set_id),
            )
            self._audit_locked(
                "changeset.approve",
                actor_id,
                "canon_owner",
                "change_set",
                change_set_id,
                {},
            )
        return self.get_change_set(change_set_id)

    def get_change_set(self, change_set_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM canon_change_set WHERE change_set_id=?",
            (change_set_id,),
        ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        result = dict(row)
        for column in ("changes_json", "impact_report_json", "migration_plan_json", "rollback_plan_json"):
            result[column.removesuffix("_json")] = json_loads(row[column], {})
            result.pop(column, None)
        return result

    def list_audit(self, *, limit: int = 100) -> list[JsonDict]:
        rows = self._conn.execute(
            "SELECT * FROM release_audit ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        result: list[JsonDict] = []
        for row in rows:
            item = dict(row)
            item["details"] = json_loads(row["details_json"], {})
            item.pop("details_json", None)
            result.append(item)
        return result

    def create_release(
        self,
        compiled: Mapping[str, Any],
        *,
        branch: str,
        actor_id: str,
        baseline_id: str,
        approvals: list[JsonDict],
        migration_plan: Mapping[str, Any],
        rollback_plan: Mapping[str, Any],
    ) -> JsonDict:
        manifest = dict(compiled["manifest"])
        release_id = new_id("worel")
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO content_release (
                    release_id,world_id,branch,release_sequence,canon_version,channel,cohort,
                    status,baseline_id,approvals_json,migration_plan_json,rollback_plan_json,created_at
                ) VALUES (?,?,?,?,?,?,?,'candidate',?,?,?,?,?)
                """,
                (
                    release_id,
                    manifest["world_id"],
                    branch,
                    manifest["release_sequence"],
                    manifest["canon_version"],
                    manifest["channel"],
                    manifest["cohort"],
                    baseline_id,
                    json_dumps(approvals),
                    json_dumps(dict(migration_plan)),
                    json_dumps(dict(rollback_plan)),
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO content_bundle (
                    bundle_id,version,release_id,content_hash,manifest_json,bundle_json,signature,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    manifest["bundle_id"],
                    manifest["canon_version"],
                    release_id,
                    manifest["content_hash"],
                    json_dumps(manifest),
                    json_dumps(dict(compiled["bundle"])),
                    manifest["signature"],
                    now,
                ),
            )
            self._audit_locked(
                "release.create", actor_id, "canon_owner", "release", release_id, {"manifest": manifest}
            )
        return self.get_release(release_id)

    def promote_release(
        self,
        release_id: str,
        *,
        target_status: str,
        actor_id: str,
        approval_token: str,
        gate_result: Mapping[str, Any],
    ) -> JsonDict:
        if target_status not in {"staging", "canary", "stable"}:
            raise ValueError("target_status must be staging, canary, or stable")
        if not approval_token:
            raise PermissionError("approval token is required")
        if gate_result.get("status") != "passed":
            raise WorldOpsContractError("release gate did not pass")
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM content_release WHERE release_id=?", (release_id,)).fetchone()
            if row is None:
                raise KeyError(release_id)
            approvals = json_loads(row["approvals_json"], [])
            if target_status == "stable" and len({item.get("actor_id") for item in approvals if item.get("approved")}) < 2:
                raise PermissionError("stable release requires two distinct approvals")
            self._conn.execute(
                "UPDATE content_release SET status=?,promoted_at=? WHERE release_id=?",
                (target_status, now, release_id),
            )
            self._audit_locked(
                "release.promote",
                actor_id,
                "release_manager",
                "release",
                release_id,
                {"target_status": target_status, "gate_run_id": gate_result.get("run_id")},
            )
        return self.get_release(release_id)

    def revoke_release(self, release_id: str, *, actor_id: str, reason: str, break_glass: bool) -> JsonDict:
        if not reason.strip():
            raise ValueError("revoke reason is required")
        now = utc_now()
        with self._lock, self._conn:
            result = self._conn.execute(
                "UPDATE content_release SET status='revoked',revoked_at=? WHERE release_id=?",
                (now, release_id),
            )
            if result.rowcount != 1:
                raise KeyError(release_id)
            self._audit_locked(
                "release.revoke",
                actor_id,
                "security_admin" if break_glass else "release_manager",
                "release",
                release_id,
                {"reason": reason, "break_glass": break_glass},
            )
        return self.get_release(release_id)

    def get_release(self, release_id: str) -> JsonDict:
        row = self._conn.execute("SELECT * FROM content_release WHERE release_id=?", (release_id,)).fetchone()
        if row is None:
            raise KeyError(release_id)
        return self._release_from_row(row)

    def latest_release(self, world_id: str, *, channel: str, cohort: str) -> JsonDict | None:
        row = self._conn.execute(
            """
            SELECT r.*,b.bundle_id,b.version AS bundle_version,b.content_hash
            FROM content_release r
            JOIN content_bundle b ON b.release_id=r.release_id
            WHERE r.world_id=? AND r.channel=? AND r.cohort=?
              AND r.status IN ('staging','canary','stable')
            ORDER BY r.release_sequence DESC LIMIT 1
            """,
            (world_id, channel, cohort),
        ).fetchone()
        return self._release_from_row(row) if row is not None else None

    def get_bundle(self, bundle_id: str, version: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM content_bundle WHERE bundle_id=? AND version=?",
            (bundle_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"{bundle_id}@{version}")
        return {
            "manifest": json_loads(row["manifest_json"], {}),
            "bundle": json_loads(row["bundle_json"], {}),
        }

    def submit_contribution(self, proposal: Mapping[str, Any]) -> JsonDict:
        capabilities = dict(proposal.get("_capability_tokens") or {})
        payload = validate_and_minimize_contribution(proposal)
        proposal_id = str(payload.get("proposal_id") or new_id("ncp"))
        receipt_token = str(capabilities.get("receipt_token") or secrets.token_urlsafe(32))
        withdrawal_token = str(capabilities.get("withdrawal_token") or secrets.token_urlsafe(32))
        if len(receipt_token) < 32 or len(withdrawal_token) < 32:
            raise WorldOpsContractError("contribution capabilities must have at least 32 characters")
        now = utc_now()
        digest = content_hash(payload)
        cluster_key = content_hash(
            {
                "world_id": payload["world_id"],
                "source_event_id": payload.get("source_event_id"),
                "suggested_changes": payload.get("suggested_changes") or [],
            }
        )[:32]
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM narrative_contribution WHERE proposal_id=? OR payload_hash=? LIMIT 1",
                (proposal_id, digest),
            ).fetchone()
            if existing is not None:
                capabilities_match = secrets.compare_digest(
                    str(existing["receipt_token_hash"]), _token_hash(receipt_token)
                ) and secrets.compare_digest(
                    str(existing["withdrawal_token_hash"]), _token_hash(withdrawal_token)
                )
                if capabilities and not capabilities_match:
                    raise PermissionError("contribution idempotency key is bound to different capabilities")
                return {
                    "status": "duplicate",
                    "proposal_id": existing["proposal_id"],
                    "review_status": existing["status"],
                    "payload_hash": existing["payload_hash"],
                    "capabilities_authenticated": bool(capabilities and capabilities_match),
                }
            self._conn.execute(
                """
                INSERT INTO narrative_contribution (
                    proposal_id,world_id,pseudonymous_subject_id,source_event_id,payload_hash,
                    payload_json,privacy_json,risk_json,cluster_key,status,receipt_token_hash,
                    withdrawal_token_hash,submitted_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'received',?,?,?,?)
                """,
                (
                    proposal_id,
                    payload["world_id"],
                    payload["pseudonymous_subject_id"],
                    payload.get("source_event_id"),
                    digest,
                    json_dumps(payload),
                    json_dumps(payload["privacy"]),
                    json_dumps(payload.get("risk") or {}),
                    cluster_key,
                    _token_hash(receipt_token),
                    _token_hash(withdrawal_token),
                    now,
                    now,
                ),
            )
        return {
            "status": "received",
            "proposal_id": proposal_id,
            "receipt_token": receipt_token,
            "withdrawal_token": withdrawal_token,
            "payload_hash": digest,
        }

    def get_contribution(self, proposal_id: str, *, receipt_token: str) -> JsonDict:
        row = self._conn.execute("SELECT * FROM narrative_contribution WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        if not secrets.compare_digest(str(row["receipt_token_hash"]), _token_hash(receipt_token)):
            raise PermissionError("invalid contribution receipt token")
        decision = self._conn.execute(
            "SELECT * FROM editorial_decision WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return {
            "proposal_id": proposal_id,
            "status": row["status"],
            "updated_at": row["updated_at"],
            "result_summary": (decision["reason"] if decision is not None else None),
            "decision": (decision["decision"] if decision is not None else None),
        }

    def withdraw_contribution(self, proposal_id: str, *, withdrawal_token: str) -> JsonDict:
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM narrative_contribution WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if not secrets.compare_digest(str(row["withdrawal_token_hash"]), _token_hash(withdrawal_token)):
                raise PermissionError("invalid withdrawal token")
            if row["status"] == "published":
                return {"proposal_id": proposal_id, "status": "privacy_review_required"}
            self._conn.execute(
                "UPDATE narrative_contribution SET status='withdrawn',payload_json='{}',withdrawn_at=?,updated_at=? WHERE proposal_id=?",
                (now, now, proposal_id),
            )
        return {"proposal_id": proposal_id, "status": "withdrawn"}

    def list_contributions(self, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        if status and status not in CONTRIBUTION_STATUSES:
            raise ValueError("invalid contribution status")
        if status:
            rows = self._conn.execute(
                "SELECT * FROM narrative_contribution WHERE status=? ORDER BY submitted_at LIMIT ?",
                (status, max(1, min(limit, 1000))),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM narrative_contribution ORDER BY submitted_at LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [self._contribution_for_editor(row) for row in rows]

    def decide_contribution(
        self,
        proposal_id: str,
        *,
        decision: str,
        reason: str,
        editor_id: str,
        target_change_set_id: str | None = None,
    ) -> JsonDict:
        if decision not in EDITORIAL_DECISIONS:
            raise ValueError("invalid editorial decision")
        status = {
            "merge_duplicate": "merged",
            "reject": "rejected",
        }.get(decision, "accepted")
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT status FROM narrative_contribution WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] == "withdrawn":
                raise WorldOpsConflictError("withdrawn contribution cannot be reviewed")
            decision_id = new_id("wodec")
            self._conn.execute(
                """
                INSERT INTO editorial_decision (
                    decision_id,proposal_id,decision,reason,editor_id,target_change_set_id,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (decision_id, proposal_id, decision, reason, editor_id, target_change_set_id, now),
            )
            self._conn.execute(
                "UPDATE narrative_contribution SET status=?,updated_at=? WHERE proposal_id=?",
                (status, now, proposal_id),
            )
            self._audit_locked(
                "contribution.decide",
                editor_id,
                "editor",
                "contribution",
                proposal_id,
                {"decision": decision, "reason": reason, "target_change_set_id": target_change_set_id},
            )
        return {"proposal_id": proposal_id, "status": status, "decision": decision, "decision_id": decision_id}

    def _audit_locked(
        self,
        action: str,
        actor_id: str,
        role: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO release_audit (
                audit_id,action,actor_id,role,target_type,target_id,correlation_id,details_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (new_id("woaudit"), action, actor_id, role, target_type, target_id, correlation_id, json_dumps(dict(details)), utc_now()),
        )

    @staticmethod
    def _release_from_row(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["approvals"] = json_loads(row["approvals_json"], [])
        result["migration_plan"] = json_loads(row["migration_plan_json"], {})
        result["rollback_plan"] = json_loads(row["rollback_plan_json"], {})
        return result

    @staticmethod
    def _contribution_for_editor(row: sqlite3.Row) -> JsonDict:
        return {
            "proposal_id": row["proposal_id"],
            "world_id": row["world_id"],
            "source_event_id": row["source_event_id"],
            "payload": json_loads(row["payload_json"], {}),
            "privacy": json_loads(row["privacy_json"], {}),
            "risk": json_loads(row["risk_json"], {}),
            "cluster_key": row["cluster_key"],
            "status": row["status"],
            "submitted_at": row["submitted_at"],
        }


class ContentStudioService:
    """Application service used by the writer/editor UI and release automation."""

    def __init__(
        self,
        store: CanonServerStore,
        compiler: CanonCompiler,
        *,
        release_gate: Callable[[JsonDict], JsonDict] | None = None,
    ) -> None:
        self.store = store
        self.compiler = compiler
        self.release_gate = release_gate or deterministic_release_gate

    @staticmethod
    def authorize(role: str, action: str) -> None:
        if action not in ROLE_ACTIONS.get(role, set()):
            raise PermissionError(f"role {role} cannot perform {action}")

    def save_draft(
        self,
        *,
        role: str,
        actor_id: str,
        object_id: str,
        object_type: str,
        payload: Mapping[str, Any],
        branch: str = "main",
        expected_revision: int = 0,
    ) -> JsonDict:
        self.authorize(role, "draft.write")
        return self.store.put_object(
            object_id,
            object_type,
            payload,
            branch=branch,
            expected_revision=expected_revision,
            author_id=actor_id,
        )

    def compile_branch(
        self,
        *,
        role: str,
        branch: str,
        canon_version: str,
        release_sequence: int,
        channel: str = "staging",
        cohort: str = "default",
    ) -> JsonDict:
        self.authorize(role, "simulate")
        snapshot = self.store.branch_snapshot(branch)
        objects = snapshot["objects"]
        if not objects.get("world"):
            raise WorldOpsContractError("branch must contain an authored world object")
        world = objects["world"][0]
        source = {
            "world": world,
            "entities": objects.get("entity") or [],
            "factions": objects.get("faction") or [],
            "relations": objects.get("relation") or [],
            "statements": objects.get("statement") or [],
            "events": objects.get("event") or [],
            "assets": objects.get("asset") or [],
            "locales": {},
            "defaults": {},
        }
        return self.compiler.compile(
            source,
            canon_version=canon_version,
            release_sequence=release_sequence,
            channel=channel,
            cohort=cohort,
        )

    def create_change_set(
        self,
        *,
        role: str,
        actor_id: str,
        world_id: str,
        branch: str,
        changes: list[JsonDict],
        impact_report: Mapping[str, Any],
        migration_plan: Mapping[str, Any],
        rollback_plan: Mapping[str, Any],
    ) -> JsonDict:
        self.authorize(role, "draft.write")
        return self.store.create_change_set(
            world_id=world_id,
            branch=branch,
            changes=changes,
            impact_report=impact_report,
            migration_plan=migration_plan,
            rollback_plan=rollback_plan,
            author_id=actor_id,
        )

    def approve_change_set(self, change_set_id: str, *, role: str, actor_id: str) -> JsonDict:
        self.authorize(role, "changeset.approve")
        return self.store.approve_change_set(change_set_id, actor_id=actor_id)

    def create_release(
        self,
        compiled: Mapping[str, Any],
        *,
        role: str,
        actor_id: str,
        branch: str,
        baseline_id: str,
        approvals: list[JsonDict],
        migration_plan: Mapping[str, Any],
        rollback_plan: Mapping[str, Any],
    ) -> JsonDict:
        self.authorize(role, "release.create")
        verifier_factory = getattr(self.compiler.signer, "verifier", None)
        if not callable(verifier_factory):
            raise WorldOpsContractError("release compiler must expose a cryptographic verifier")
        verify_compiled_bundle(compiled, verifier_factory())
        gate = self.release_gate(dict(compiled))
        if gate.get("status") != "passed":
            raise WorldOpsContractError("WorldOps release gate failed")
        return self.store.create_release(
            compiled,
            branch=branch,
            actor_id=actor_id,
            baseline_id=baseline_id,
            approvals=approvals,
            migration_plan=migration_plan,
            rollback_plan=rollback_plan,
        )


class WorldOpsServerAPI:
    """Transport-neutral implementation of the documented REST surface."""

    def __init__(self, store: CanonServerStore) -> None:
        self.store = store

    def get_latest_release(self, world_id: str, *, channel: str, cohort: str, etag: str | None = None) -> JsonDict:
        release = self.store.latest_release(world_id, channel=channel, cohort=cohort)
        if release is None:
            return {"status": 404}
        digest = content_hash(release)
        if etag == digest:
            return {"status": 304, "etag": digest}
        return {"status": 200, "etag": digest, "release": release}

    def get_bundle(self, bundle_id: str, version: str) -> JsonDict:
        return {"status": 200, "content": self.store.get_bundle(bundle_id, version)}

    def post_contribution(self, proposal: Mapping[str, Any]) -> JsonDict:
        return {"status": 202, "receipt": self.store.submit_contribution(proposal)}

    def get_contribution(self, proposal_id: str, *, receipt_token: str) -> JsonDict:
        return {"status": 200, "contribution": self.store.get_contribution(proposal_id, receipt_token=receipt_token)}

    def withdraw_contribution(self, proposal_id: str, *, withdrawal_token: str) -> JsonDict:
        return {"status": 200, "result": self.store.withdraw_contribution(proposal_id, withdrawal_token=withdrawal_token)}


def validate_and_minimize_contribution(proposal: Mapping[str, Any]) -> JsonDict:
    payload = dict(proposal)
    if payload.get("contract") != NARRATIVE_CONTRIBUTION_CONTRACT:
        raise WorldOpsContractError("unsupported contribution contract")
    required = ("world_id", "pseudonymous_subject_id", "experience_summary")
    for key in required:
        if not str(payload.get(key) or "").strip():
            raise WorldOpsContractError(f"contribution requires {key}")
    privacy = dict(payload.get("privacy") or {})
    if privacy.get("mode") != "anonymous":
        raise WorldOpsContractError("anonymous is the default and required contribution mode")
    if privacy.get("raw_text_included"):
        raise WorldOpsContractError("raw conversation text is not accepted")
    if privacy.get("attribution_allowed") and not privacy.get("single_submission_opt_in"):
        raise WorldOpsContractError("attribution requires single-submission explicit opt-in")
    summary = _redact_untrusted_text(str(payload["experience_summary"])[:2000])
    suggested_changes = [
        _redact_untrusted_value(dict(item))
        for item in payload.get("suggested_changes") or []
        if isinstance(item, dict)
    ][:20]
    canon_conflicts = [
        _redact_untrusted_text(str(item)[:1000])
        for item in payload.get("canon_conflicts") or []
    ][:50]
    minimized = {
        "contract": NARRATIVE_CONTRIBUTION_CONTRACT,
        "proposal_id": str(payload.get("proposal_id") or new_id("ncp")),
        "world_id": str(payload["world_id"]),
        "pseudonymous_subject_id": str(payload["pseudonymous_subject_id"]),
        "source_event_id": payload.get("source_event_id"),
        "source_definition_version": payload.get("source_definition_version"),
        "experience_summary": summary,
        "user_choice": dict(payload.get("user_choice") or {}),
        "suggested_changes": suggested_changes,
        "affected_entities": [str(item) for item in payload.get("affected_entities") or []][:50],
        "privacy": {
            "mode": "anonymous",
            "raw_text_included": False,
            "attribution_allowed": bool(privacy.get("attribution_allowed", False)),
            "single_submission_opt_in": bool(privacy.get("single_submission_opt_in", False)),
        },
        "canon_conflicts": canon_conflicts,
        "novelty_score": max(0.0, min(1.0, float(payload.get("novelty_score", 0.0)))),
        "promotion_recommendation": str(payload.get("promotion_recommendation") or "editorial_review"),
        "risk": dict(payload.get("risk") or {}),
    }
    serialized = json.dumps(
        {
            "experience_summary": minimized["experience_summary"],
            "suggested_changes": minimized["suggested_changes"],
            "canon_conflicts": minimized["canon_conflicts"],
        },
        ensure_ascii=False,
    )
    for pattern in FORBIDDEN_RAW_PATTERNS:
        if pattern.search(serialized):
            raise WorldOpsContractError("contribution still contains sensitive identifiers after redaction")
    return minimized


def _redact_untrusted_text(value: str) -> str:
    result = value
    for pattern in FORBIDDEN_RAW_PATTERNS:
        result = pattern.sub("[redacted]", result)
    return result


def _redact_untrusted_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise WorldOpsContractError("contribution nested content exceeds the privacy processing limit")
    if isinstance(value, str):
        return _redact_untrusted_text(value[:2000])
    if isinstance(value, dict):
        return {
            str(key)[:128]: _redact_untrusted_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_redact_untrusted_value(item, depth=depth + 1) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_untrusted_text(str(value)[:2000])
