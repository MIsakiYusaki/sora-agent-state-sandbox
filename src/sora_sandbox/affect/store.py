from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..memory_schema import json_dumps, json_loads, new_id, utc_now
from ..paths import SEEDS_DIR
from .contracts import (
    AFFECT_SCHEMA_VERSION,
    AffectAppraisal,
    AffectStimulusEvent,
    JsonDict,
    neutral_snapshot,
)
from .dynamics import snapshot_checksum
from .privacy import AffectPayloadProtectionError, AffectPayloadProtector
from .schema import AFFECT_SCHEMA_SQL


class AffectStore:
    """SQLite canonical store for Affect state and replayable contributions."""

    def __init__(
        self,
        path: str | Path,
        *,
        sora_id: str = "default-agent",
        profile: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sora_id = sora_id
        self._payload_protector = AffectPayloadProtector()
        self._lock = threading.RLock()
        self._read_local = threading.local()
        self._read_connections: list[sqlite3.Connection] = []
        self._read_connections_lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(AFFECT_SCHEMA_SQL)
        self._migrate_schema()
        self._conn.commit()
        self._ensure_instance(dict(profile or self._load_default_profile()))

    @staticmethod
    def _load_default_profile() -> JsonDict:
        return json.loads((SEEDS_DIR / "affect.seed.json").read_text(encoding="utf-8"))

    def close(self) -> None:
        with self._lock:
            with self._read_connections_lock:
                for connection in self._read_connections:
                    connection.close()
                self._read_connections.clear()
            self._conn.close()

    def _read_connection(self) -> sqlite3.Connection:
        connection = getattr(self._read_local, "connection", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._read_local.connection = connection
        with self._read_connections_lock:
            self._read_connections.append(connection)
        return connection

    def reset(self) -> None:
        with self._lock, self._conn:
            for table in (
                "affect_forget_tombstone",
                "affect_metric",
                "affect_trace",
                "affect_outbox",
                "affect_episode_source_event",
                "affect_emotion_episode",
                "affect_contribution",
                "affect_appraisal_evidence_ref",
                "affect_appraisal",
                "affect_event_evidence_ref",
                "affect_stimulus_event",
                "affect_core_snapshot",
                "affect_control",
                "affect_instance",
            ):
                self._conn.execute(f"DELETE FROM {table}")
        self._ensure_instance(self._load_default_profile())

    def profile(self) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT profile_json FROM affect_instance WHERE sora_id = ?",
            (self.sora_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Affect instance not found: {self.sora_id}")
        return json_loads(row["profile_json"], {})

    def update_profile(self, profile: Mapping[str, Any], *, actor: str, reason: str) -> JsonDict:
        payload = dict(profile)
        version = str(payload.get("version") or "")
        if not version:
            raise ValueError("Affect profile version is required")
        now = utc_now()
        with self._lock, self._conn:
            before = self.profile()
            self._conn.execute(
                "UPDATE affect_instance SET profile_version = ?, profile_json = ?, updated_at = ? WHERE sora_id = ?",
                (version, json_dumps(payload), now, self.sora_id),
            )
            self._insert_trace_locked(
                stage="profile_updated",
                status="applied",
                details={"actor": actor, "reason": reason, "before": before.get("version"), "after": version},
            )
        return payload

    def active_snapshot(self) -> JsonDict:
        row = self._read_connection().execute(
            """
            SELECT s.* FROM affect_core_snapshot s
            JOIN affect_instance i ON i.active_snapshot_id = s.snapshot_id
            WHERE i.sora_id = ?
            """,
            (self.sora_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Affect snapshot not found: {self.sora_id}")
        return json_loads(row["snapshot_json"], {})

    def insert_event(self, event: AffectStimulusEvent, *, semantic_enabled: bool) -> JsonDict:
        event.validate()
        if event.sora_id != self.sora_id:
            raise ValueError("Affect event sora_id does not match store")
        max_chars = max(1024, int(self.profile().get("max_untrusted_text_chars", 32768)))
        transient_text = (event.untrusted_text or "")[:max_chars]
        payload = dict(event.payload)
        if event.untrusted_text and len(event.untrusted_text) > max_chars:
            payload["untrusted_text_truncated"] = True
            payload["untrusted_text_original_chars"] = len(event.untrusted_text)
        raw_text = (
            self._payload_protector.protect(transient_text)
            if semantic_enabled and transient_text
            else None
        )
        content_hash = hashlib.sha256(transient_text.encode("utf-8")).hexdigest() if transient_text else None
        now = utc_now()
        retention = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self._lock, self._conn:
            ref_hashes = {_opaque_hash(event.source_ref), *(_opaque_hash(item) for item in event.evidence_refs)}
            placeholders = ",".join("?" for _ in ref_hashes)
            forgotten = self._conn.execute(
                f"""
                SELECT ref_hash FROM affect_forget_tombstone
                WHERE sora_id = ? AND ref_hash IN ({placeholders}) LIMIT 1
                """,
                (self.sora_id, *sorted(ref_hashes)),
            ).fetchone()
            if forgotten is not None:
                rejected = event.to_dict()
                rejected.update(
                    {
                        "semantic_status": "not_requested",
                        "semantic_attempts": 0,
                        "semantic_error": "source_forgotten",
                    }
                )
                return {"status": "rejected_forgotten", "event": rejected}
            existing = self._conn.execute(
                """
                SELECT * FROM affect_stimulus_event
                WHERE event_id = ? OR (sora_id = ? AND source_ref = ? AND source_revision = ?)
                LIMIT 1
                """,
                (event.event_id, event.sora_id, event.source_ref, event.source_revision),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", "event": self._event_from_row(existing)}
            self._conn.execute(
                """
                INSERT INTO affect_stimulus_event (
                    event_id, sora_id, source_type, source_ref, source_revision,
                    occurred_at, received_at, actor_user_id, target_kind, target_ref,
                    relationship_ref, conversation_scope, source_confidence, event_kind,
                    payload_json, evidence_refs_json, raw_text, content_hash, sensitivity,
                    correlation_id, semantic_status, retention_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.sora_id,
                    event.source_type,
                    event.source_ref,
                    event.source_revision,
                    event.occurred_at,
                    now,
                    event.actor_user_id,
                    event.target_kind,
                    event.target_ref,
                    event.relationship_ref,
                    event.conversation_scope,
                    event.source_confidence,
                    event.event_kind,
                    json_dumps(payload),
                    json_dumps(list(event.evidence_refs)),
                    raw_text,
                    content_hash,
                    event.sensitivity,
                    event.correlation_id,
                    "pending" if raw_text else "not_requested",
                    retention,
                ),
            )
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO affect_event_evidence_ref (sora_id, ref_hash, event_id)
                VALUES (?, ?, ?)
                """,
                [
                    (self.sora_id, _opaque_hash(ref), event.event_id)
                    for ref in sorted(set(event.evidence_refs))
                ],
            )
            row = self._conn.execute(
                "SELECT * FROM affect_stimulus_event WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
        return {"status": "inserted", "event": self._event_from_row(row)}

    def event(self, event_id: str, *, include_raw_text: bool = False) -> JsonDict:
        row = self._read_connection().execute(
            "SELECT * FROM affect_stimulus_event WHERE event_id = ? AND sora_id = ?",
            (event_id, self.sora_id),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._event_from_row(row, include_raw_text=include_raw_text)

    def commit_transition(
        self,
        *,
        appraisal: AffectAppraisal,
        contribution: Mapping[str, Any] | None,
        snapshot: Mapping[str, Any] | None,
        relationship_ref: str | None,
        conversation_scope: str,
        correlation_id: str | None,
        trace_details: Mapping[str, Any],
    ) -> JsonDict:
        appraisal.validate()
        now = utc_now()
        episode_id: str | None = None
        with self._lock, self._conn:
            if snapshot is not None:
                self._assert_next_revision_locked(snapshot)
            self._insert_appraisal_locked(appraisal, now=now)
            if contribution is not None:
                self._insert_contribution_locked(contribution, now=now)
                emotion = dict(contribution.get("emotion") or {})
                if emotion:
                    episode_id = self._upsert_episode_locked(
                        appraisal,
                        emotion,
                        relationship_ref=relationship_ref,
                        conversation_scope=conversation_scope,
                        now=now,
                    )
            if snapshot is not None:
                self._insert_snapshot_locked(snapshot, source_contribution_count=self.active_contribution_count(), now=now)
            self._conn.execute(
                "UPDATE affect_stimulus_event SET processed_at = ? WHERE event_id = ?",
                (now, appraisal.event_id),
            )
            trace_id = self._insert_trace_locked(
                stage=f"appraisal_{appraisal.lane}",
                status="applied",
                event_id=appraisal.event_id,
                appraisal_id=appraisal.appraisal_id,
                snapshot_id=(str(snapshot.get("snapshot_id")) if snapshot else None),
                correlation_id=correlation_id,
                details=dict(trace_details),
            )
        return {
            "status": "applied",
            "appraisal": appraisal.to_dict(),
            "contribution": dict(contribution or {}),
            "snapshot": dict(snapshot or self.active_snapshot()),
            "episode_id": episode_id,
            "trace_id": trace_id,
        }

    def fast_appraisal(self, event_id: str) -> AffectAppraisal | None:
        row = self._read_connection().execute(
            """
            SELECT * FROM affect_appraisal
            WHERE event_id = ? AND lane = 'fast'
            ORDER BY revision DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        return self._appraisal_from_row(row) if row is not None else None

    def claim_semantic_event(self, *, ignore_backoff: bool = False) -> JsonDict | None:
        with self._lock, self._conn:
            control = self.get_control()
            if control["processing_paused"]:
                return None
            backoff_clause = "" if ignore_backoff else "AND (semantic_next_attempt_at IS NULL OR semantic_next_attempt_at <= ?)"
            params: tuple[Any, ...] = (self.sora_id,) if ignore_backoff else (self.sora_id, utc_now())
            row = self._conn.execute(
                f"""
                SELECT * FROM affect_stimulus_event
                WHERE sora_id = ? AND semantic_status = 'pending' AND raw_text IS NOT NULL
                  {backoff_clause}
                ORDER BY occurred_at ASC, event_id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            updated = self._conn.execute(
                """
                UPDATE affect_stimulus_event
                SET semantic_status = 'evaluating', semantic_attempts = semantic_attempts + 1,
                    semantic_error = NULL, semantic_next_attempt_at = NULL
                WHERE event_id = ? AND semantic_status = 'pending'
                """,
                (row["event_id"],),
            )
            if updated.rowcount != 1:
                return None
            claimed = self._conn.execute(
                "SELECT * FROM affect_stimulus_event WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()
        try:
            return self._event_from_row(claimed, include_raw_text=True)
        except AffectPayloadProtectionError as exc:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    UPDATE affect_stimulus_event
                    SET semantic_status = 'dead_letter', semantic_error = ?, raw_text = NULL, processed_at = ?
                    WHERE event_id = ?
                    """,
                    (f"AFF-APPRAISE-101:{type(exc).__name__}", utc_now(), row["event_id"]),
                )
                self._insert_trace_locked(
                    stage="semantic_payload_unprotect",
                    status="dead_letter",
                    event_id=str(row["event_id"]),
                    correlation_id=row["correlation_id"],
                    details={"error_code": "AFF-APPRAISE-101", "raw_text_persisted": False},
                )
            return None

    def complete_semantic_event(
        self,
        event_id: str,
        *,
        status: str,
        error: str | None = None,
        retry: bool = False,
        redact_raw: bool = True,
    ) -> JsonDict:
        if status not in {"applied", "skipped", "quarantined", "failed", "dead_letter"}:
            raise ValueError(f"unsupported semantic completion status: {status}")
        now = utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT semantic_attempts FROM affect_stimulus_event WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if retry:
                next_status = "pending" if int(row["semantic_attempts"]) < 3 else "dead_letter"
            else:
                next_status = status
            next_attempt_at = None
            if next_status == "pending":
                backoff_seconds = min(60, 2 ** max(0, int(row["semantic_attempts"]) - 1))
                next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
            if redact_raw and next_status != "pending":
                self._conn.execute(
                    """
                    UPDATE affect_stimulus_event
                    SET semantic_status = ?, semantic_error = ?, semantic_next_attempt_at = ?,
                        processed_at = ?, raw_text = NULL
                    WHERE event_id = ?
                    """,
                    (next_status, error, next_attempt_at, now, event_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE affect_stimulus_event
                    SET semantic_status = ?, semantic_error = ?, semantic_next_attempt_at = ?,
                        processed_at = ?
                    WHERE event_id = ?
                    """,
                    (next_status, error, next_attempt_at, now, event_id),
                )
        return self.event(event_id)

    def retry_semantic_event(self, event_id: str) -> JsonDict:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE affect_stimulus_event
                SET semantic_status = CASE WHEN raw_text IS NULL THEN 'failed' ELSE 'pending' END,
                    semantic_error = NULL, semantic_next_attempt_at = NULL
                WHERE event_id = ?
                """,
                (event_id,),
            )
        return self.event(event_id)

    def list_events(self, *, semantic_status: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = "SELECT * FROM affect_stimulus_event WHERE sora_id = ?"
        params: list[Any] = [self.sora_id]
        if semantic_status is not None:
            sql += " AND semantic_status = ?"
            params.append(semantic_status)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        rows = self._read_connection().execute(sql, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_appraisals(self, *, active: bool | None = None, limit: int = 1000) -> list[JsonDict]:
        sql = "SELECT * FROM affect_appraisal WHERE sora_id = ?"
        params: list[Any] = [self.sora_id]
        if active is not None:
            sql += " AND active = ?"
            params.append(int(active))
        sql += " ORDER BY effective_at DESC, appraisal_id DESC LIMIT ?"
        params.append(limit)
        rows = self._read_connection().execute(sql, params).fetchall()
        return [self._appraisal_from_row(row).to_dict() | {"active": bool(row["active"])} for row in rows]

    def list_contributions(self, *, active: bool | None = None, limit: int = 10000) -> list[JsonDict]:
        sql = "SELECT * FROM affect_contribution WHERE sora_id = ?"
        params: list[Any] = [self.sora_id]
        if active is not None:
            sql += " AND active = ?"
            params.append(int(active))
        sql += " ORDER BY effective_at ASC, contribution_id ASC LIMIT ?"
        params.append(limit)
        rows = self._read_connection().execute(sql, params).fetchall()
        return [self._contribution_from_row(row) for row in rows]

    def active_contribution_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM affect_contribution WHERE sora_id = ? AND active = 1",
                    (self.sora_id,),
                ).fetchone()[0]
            )

    def deactivate_contribution(self, contribution_id: str, *, reason: str, actor: str) -> JsonDict:
        with self._lock, self._conn:
            source = self._conn.execute(
                "SELECT event_id FROM affect_contribution WHERE contribution_id = ? AND sora_id = ?",
                (contribution_id, self.sora_id),
            ).fetchone()
            updated = self._conn.execute(
                """
                UPDATE affect_contribution SET active = 0, deactivation_reason = ?
                WHERE contribution_id = ? AND sora_id = ? AND active = 1
                """,
                (f"{reason}:{actor}", contribution_id, self.sora_id),
            )
            if updated.rowcount != 1:
                raise KeyError(contribution_id)
            self._refresh_episodes_locked(
                {str(source["event_id"])} if source is not None else set(),
                now=utc_now(),
                reason=f"contribution_deactivated:{reason}",
            )
            self._insert_trace_locked(
                stage="contribution_deactivated",
                status="applied",
                details={"contribution_id": contribution_id, "reason": reason, "actor": actor},
            )
            row = self._conn.execute(
                "SELECT * FROM affect_contribution WHERE contribution_id = ?",
                (contribution_id,),
            ).fetchone()
        return self._contribution_from_row(row)

    def save_rebuild_snapshot(self, snapshot: Mapping[str, Any], *, reason: str, actor: str) -> JsonDict:
        with self._lock, self._conn:
            self._assert_next_revision_locked(snapshot)
            self._insert_snapshot_locked(snapshot, source_contribution_count=self.active_contribution_count(), now=utc_now())
            self._insert_trace_locked(
                stage="rebuild",
                status="applied",
                snapshot_id=str(snapshot["snapshot_id"]),
                details={"reason": reason, "actor": actor, "active_contributions": self.active_contribution_count()},
            )
        return dict(snapshot)

    def episodes_for_memory(self, *, limit: int = 50) -> list[JsonDict]:
        rows = self._read_connection().execute(
            """
            SELECT ep.*, ev.event_kind AS source_event_kind,
                   ev.source_type AS source_type, ev.sensitivity AS source_sensitivity
            FROM affect_emotion_episode ep
            JOIN affect_stimulus_event ev ON ev.event_id = ep.event_id
            WHERE ep.sora_id = ? AND ep.active = 1 AND ep.memory_proposal_id IS NULL
            ORDER BY ep.peak_intensity DESC, ep.updated_at ASC
            LIMIT ?
            """,
            (self.sora_id, limit),
        ).fetchall()
        return [self._episode_from_row(row) for row in rows]

    def forget_source(
        self,
        source_ref: str,
        *,
        evidence_refs: list[str] | None = None,
        reason_code: str = "user_forget",
    ) -> JsonDict:
        refs = {str(source_ref), *(str(item) for item in evidence_refs or [])}
        hashes = {_opaque_hash(item) for item in refs if item}
        now = utc_now()
        with self._lock, self._conn:
            ref_placeholders = ",".join("?" for _ in refs)
            hash_placeholders = ",".join("?" for _ in hashes)
            event_ids = {
                str(row["event_id"])
                for row in self._conn.execute(
                    f"""
                    SELECT event_id FROM affect_stimulus_event
                    WHERE sora_id = ? AND source_ref IN ({ref_placeholders})
                    UNION
                    SELECT event_id FROM affect_event_evidence_ref
                    WHERE sora_id = ? AND ref_hash IN ({hash_placeholders})
                    """,
                    (self.sora_id, *sorted(refs), self.sora_id, *sorted(hashes)),
                ).fetchall()
            }
            evidence_appraisal_rows = self._conn.execute(
                f"""
                SELECT appraisal_id, event_id FROM affect_appraisal_evidence_ref
                WHERE sora_id = ? AND ref_hash IN ({hash_placeholders})
                """,
                (self.sora_id, *sorted(hashes)),
            ).fetchall()
            event_ids.update(str(row["event_id"]) for row in evidence_appraisal_rows)
            appraisal_ids = {str(row["appraisal_id"]) for row in evidence_appraisal_rows}
            if event_ids:
                event_placeholders = ",".join("?" for _ in event_ids)
                appraisal_ids.update(
                    str(row["appraisal_id"])
                    for row in self._conn.execute(
                        f"""
                        SELECT appraisal_id FROM affect_appraisal
                        WHERE sora_id = ? AND event_id IN ({event_placeholders})
                        """,
                        (self.sora_id, *sorted(event_ids)),
                    ).fetchall()
                )
            for ref_hash in hashes:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO affect_forget_tombstone
                        (sora_id, ref_hash, ref_kind, reason_code, created_at)
                    VALUES (?, ?, 'opaque_source_or_evidence_ref', ?, ?)
                    """,
                    (self.sora_id, ref_hash, reason_code, now),
                )
            contribution_count = 0
            appraisal_count = 0
            episode_count = 0
            event_count = 0
            episode_ids: list[str] = []
            affected_episode_ids: set[str] = set()
            if event_ids:
                event_placeholders = ",".join("?" for _ in event_ids)
                affected_episode_ids = {
                    str(row["episode_id"])
                    for row in self._conn.execute(
                        f"""
                        SELECT DISTINCT episode_id FROM affect_episode_source_event
                        WHERE sora_id = ? AND event_id IN ({event_placeholders})
                        """,
                        (self.sora_id, *sorted(event_ids)),
                    ).fetchall()
                }
            episode_rows = []
            if affected_episode_ids:
                episode_placeholders = ",".join("?" for _ in affected_episode_ids)
                episode_rows = self._conn.execute(
                    f"""
                    SELECT * FROM affect_emotion_episode
                    WHERE sora_id = ? AND active = 1 AND episode_id IN ({episode_placeholders})
                    """,
                    (self.sora_id, *sorted(affected_episode_ids)),
                ).fetchall()
            if appraisal_ids:
                placeholders = ",".join("?" for _ in appraisal_ids)
                params = tuple(sorted(appraisal_ids))
                contribution_count = self._conn.execute(
                    f"""
                    UPDATE affect_contribution SET active = 0, deactivation_reason = ?
                    WHERE active = 1 AND appraisal_id IN ({placeholders})
                    """,
                    (f"{reason_code}:forget", *params),
                ).rowcount
                self._conn.execute(
                    f"DELETE FROM affect_appraisal_evidence_ref WHERE appraisal_id IN ({placeholders})",
                    params,
                )
                appraisal_count = self._conn.execute(
                    f"""
                    UPDATE affect_appraisal SET active = 0, deactivation_reason = ?
                    WHERE active = 1 AND appraisal_id IN ({placeholders})
                    """,
                    (f"{reason_code}:forget", *params),
                ).rowcount
            if affected_episode_ids:
                refresh = self._refresh_episodes_locked(
                    event_ids,
                    episode_ids=affected_episode_ids,
                    now=now,
                    reason=reason_code,
                )
                episode_ids = sorted(affected_episode_ids)
                episode_count = int(refresh["deactivated"])
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                self._conn.execute(
                    f"DELETE FROM affect_event_evidence_ref WHERE event_id IN ({placeholders})",
                    tuple(sorted(event_ids)),
                )
                for event_id in sorted(event_ids):
                    self._conn.execute(
                        """
                        UPDATE affect_stimulus_event
                        SET source_ref = ?, raw_text = NULL, content_hash = NULL,
                            payload_json = '{}', evidence_refs_json = '[]', sensitivity = 'forgotten',
                            semantic_status = CASE WHEN semantic_status IN ('pending', 'evaluating')
                                THEN 'skipped' ELSE semantic_status END,
                            semantic_error = 'source_forgotten', processed_at = ?
                        WHERE event_id = ?
                        """,
                        (f"forgotten:{_opaque_hash(event_id)}", now, event_id),
                    )
                    event_count += 1
            if episode_ids:
                outbox_rows = self._conn.execute(
                    "SELECT event_id, payload_json FROM affect_outbox WHERE sora_id = ? AND status IN ('pending', 'failed')",
                    (self.sora_id,),
                ).fetchall()
                for row in outbox_rows:
                    payload = json_loads(row["payload_json"], {})
                    if payload.get("episode_id") in episode_ids:
                        self._conn.execute(
                            "UPDATE affect_outbox SET status = 'cancelled', last_error = ?, processed_at = ? WHERE event_id = ?",
                            (reason_code, now, row["event_id"]),
                        )
            self._insert_trace_locked(
                stage="forget",
                status="applied",
                details={
                    "reason_code": reason_code,
                    "ref_hashes": sorted(hashes),
                    "events_redacted": event_count,
                    "appraisals_deactivated": appraisal_count,
                    "contributions_deactivated": contribution_count,
                    "episodes_deactivated": episode_count,
                    "episodes_rebuilt": len(episode_ids) - episode_count,
                },
            )
        return {
            "status": "forgotten",
            "events_redacted": event_count,
            "appraisals_deactivated": appraisal_count,
            "contributions_deactivated": contribution_count,
            "episodes_deactivated": episode_count,
            "episodes_rebuilt": len(episode_ids) - episode_count,
            "affected_episode_ids": episode_ids,
            "affected_memory_proposal_ids": sorted(
                {
                    str(row["memory_proposal_id"])
                    for row in episode_rows
                    if str(row["episode_id"]) in set(episode_ids) and row["memory_proposal_id"]
                }
            ),
            "tombstones_written": len(hashes),
        }

    def has_source_refs(self, refs: list[str]) -> bool:
        candidates = {str(item) for item in refs if item}
        if not candidates:
            return False
        hashes = {_opaque_hash(item) for item in candidates}
        candidate_placeholders = ",".join("?" for _ in candidates)
        hash_placeholders = ",".join("?" for _ in hashes)
        row = self._read_connection().execute(
            f"""
            SELECT 1 FROM affect_stimulus_event
            WHERE sora_id = ? AND source_ref IN ({candidate_placeholders})
            UNION ALL
            SELECT 1 FROM affect_event_evidence_ref
            WHERE sora_id = ? AND ref_hash IN ({hash_placeholders})
            UNION ALL
            SELECT 1 FROM affect_appraisal_evidence_ref refs
            JOIN affect_appraisal appraisal ON appraisal.appraisal_id = refs.appraisal_id
            WHERE refs.sora_id = ? AND refs.ref_hash IN ({hash_placeholders}) AND appraisal.active = 1
            LIMIT 1
            """,
            (
                self.sora_id,
                *sorted(candidates),
                self.sora_id,
                *sorted(hashes),
                self.sora_id,
                *sorted(hashes),
            ),
        ).fetchone()
        return row is not None

    def mark_episode_memory_proposed(self, episode_id: str, proposal_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE affect_emotion_episode SET memory_proposal_id = ?, updated_at = ? WHERE episode_id = ?",
                (proposal_id, utc_now(), episode_id),
            )

    def set_control(
        self,
        *,
        user_id: str | None = None,
        processing_paused: bool | None = None,
        expression_permission: str | None = None,
    ) -> JsonDict:
        key = user_id or ""
        current = self.get_control(user_id=user_id)
        paused = current["processing_paused"] if processing_paused is None else bool(processing_paused)
        permission = current["expression_permission"] if expression_permission is None else expression_permission
        if permission not in {"silent", "implicit", "explicit"}:
            raise ValueError("expression_permission must be silent, implicit, or explicit")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO affect_control (sora_id, user_id, processing_paused, expression_permission, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sora_id, user_id) DO UPDATE SET
                    processing_paused = excluded.processing_paused,
                    expression_permission = excluded.expression_permission,
                    updated_at = excluded.updated_at
                """,
                (self.sora_id, key, int(paused), permission, utc_now()),
            )
        return self.get_control(user_id=user_id)

    def get_control(self, *, user_id: str | None = None) -> JsonDict:
        key = user_id or ""
        row = self._read_connection().execute(
            "SELECT * FROM affect_control WHERE sora_id = ? AND user_id = ?",
            (self.sora_id, key),
        ).fetchone()
        if row is None and user_id is not None:
            return self.get_control()
        if row is None:
            return {"processing_paused": False, "expression_permission": "implicit", "user_id": user_id}
        return {
            "processing_paused": bool(row["processing_paused"]),
            "expression_permission": row["expression_permission"],
            "user_id": user_id,
            "updated_at": row["updated_at"],
        }

    def enqueue_outbox(self, event_type: str, event_key: str, payload: Mapping[str, Any]) -> JsonDict:
        payload_json = json_dumps(dict(payload))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event_id = new_id("aout")
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM affect_outbox WHERE event_key = ? AND payload_hash = ?",
                (event_key, payload_hash),
            ).fetchone()
            if existing is not None:
                return self._outbox_from_row(existing)
            self._conn.execute(
                """
                INSERT INTO affect_outbox (
                    event_id, sora_id, event_type, event_key, payload_hash,
                    payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (event_id, self.sora_id, event_type, event_key, payload_hash, payload_json, utc_now()),
            )
            row = self._conn.execute("SELECT * FROM affect_outbox WHERE event_id = ?", (event_id,)).fetchone()
        return self._outbox_from_row(row)

    def mark_outbox(self, event_id: str, *, status: str, error: str | None = None) -> JsonDict:
        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT attempts, max_attempts FROM affect_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if current is None:
                raise KeyError(event_id)
            next_status = status
            if status == "failed" and int(current["attempts"]) + 1 >= int(current["max_attempts"]):
                next_status = "dead_letter"
            next_attempt_at = None
            if next_status == "failed":
                backoff_seconds = min(300, 2 ** int(current["attempts"]))
                next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
            self._conn.execute(
                """
                UPDATE affect_outbox SET status = ?, attempts = attempts + 1,
                    last_error = ?, next_attempt_at = ?, processed_at = ? WHERE event_id = ?
                """,
                (next_status, error, next_attempt_at, utc_now(), event_id),
            )
            row = self._conn.execute("SELECT * FROM affect_outbox WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._outbox_from_row(row)

    def list_outbox(self, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = "SELECT * FROM affect_outbox WHERE sora_id = ?"
        params: list[Any] = [self.sora_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._read_connection().execute(sql, params).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def retry_outbox(self, event_id: str) -> JsonDict:
        with self._lock, self._conn:
            updated = self._conn.execute(
                """
                UPDATE affect_outbox
                SET status = 'pending', attempts = 0, next_attempt_at = NULL,
                    last_error = NULL, processed_at = NULL
                WHERE event_id = ? AND sora_id = ? AND status IN ('failed', 'dead_letter')
                """,
                (event_id, self.sora_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Affect outbox item is not retryable: {event_id}")
            row = self._conn.execute(
                "SELECT * FROM affect_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self._insert_trace_locked(
                stage="outbox_retry",
                status="pending",
                details={"event_id": event_id},
            )
        return self._outbox_from_row(row)

    def run_retention(
        self,
        *,
        now: str | None = None,
        trace_days: int = 30,
        metric_days: int = 30,
        processed_outbox_days: int = 14,
    ) -> JsonDict:
        current = datetime.fromisoformat((now or utc_now()).replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        trace_cutoff = (current - timedelta(days=max(1, trace_days))).isoformat()
        metric_cutoff = (current - timedelta(days=max(1, metric_days))).isoformat()
        outbox_cutoff = (current - timedelta(days=max(1, processed_outbox_days))).isoformat()
        with self._lock, self._conn:
            raw = self._conn.execute(
                """
                UPDATE affect_stimulus_event SET raw_text = NULL
                WHERE raw_text IS NOT NULL AND retention_expires_at IS NOT NULL
                  AND retention_expires_at <= ?
                """,
                (current.isoformat(),),
            ).rowcount
            traces = self._conn.execute(
                "DELETE FROM affect_trace WHERE sora_id = ? AND created_at < ?",
                (self.sora_id, trace_cutoff),
            ).rowcount
            metrics = self._conn.execute(
                "DELETE FROM affect_metric WHERE sora_id = ? AND created_at < ?",
                (self.sora_id, metric_cutoff),
            ).rowcount
            outbox = self._conn.execute(
                """
                DELETE FROM affect_outbox
                WHERE sora_id = ? AND status = 'processed' AND processed_at < ?
                """,
                (self.sora_id, outbox_cutoff),
            ).rowcount
            self._insert_trace_locked(
                stage="retention",
                status="applied",
                details={
                    "raw_text_redacted": raw,
                    "traces_deleted": traces,
                    "metrics_deleted": metrics,
                    "processed_outbox_deleted": outbox,
                    "trace_days": trace_days,
                    "metric_days": metric_days,
                    "processed_outbox_days": processed_outbox_days,
                },
            )
        return {
            "status": "retention_applied",
            "raw_text_redacted": raw,
            "traces_deleted": traces,
            "metrics_deleted": metrics,
            "processed_outbox_deleted": outbox,
        }

    def record_metric(self, name: str, value: float, *, labels: Mapping[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO affect_metric (metric_id, sora_id, name, value, labels_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("ametric"), self.sora_id, name, float(value), json_dumps(dict(labels or {})), utc_now()),
            )

    def record_trace(
        self,
        *,
        stage: str,
        status: str,
        details: Mapping[str, Any],
        event_id: str | None = None,
        appraisal_id: str | None = None,
        snapshot_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        with self._lock, self._conn:
            return self._insert_trace_locked(
                stage=stage,
                status=status,
                details=details,
                event_id=event_id,
                appraisal_id=appraisal_id,
                snapshot_id=snapshot_id,
                correlation_id=correlation_id,
            )

    def list_metrics(self, *, limit: int = 1000) -> list[JsonDict]:
        rows = self._read_connection().execute(
            "SELECT * FROM affect_metric WHERE sora_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.sora_id, limit),
        ).fetchall()
        return [
            {
                "metric_id": row["metric_id"],
                "name": row["name"],
                "value": row["value"],
                "labels": json_loads(row["labels_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_traces(self, *, correlation_id: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = "SELECT * FROM affect_trace WHERE sora_id = ?"
        params: list[Any] = [self.sora_id]
        if correlation_id:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._read_connection().execute(sql, params).fetchall()
        return [
            {
                "trace_id": row["trace_id"],
                "correlation_id": row["correlation_id"],
                "event_id": row["event_id"],
                "appraisal_id": row["appraisal_id"],
                "snapshot_id": row["snapshot_id"],
                "stage": row["stage"],
                "status": row["status"],
                "details": json_loads(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def table_counts(self) -> JsonDict:
        tables = (
            "affect_stimulus_event",
            "affect_event_evidence_ref",
            "affect_appraisal",
            "affect_appraisal_evidence_ref",
            "affect_contribution",
            "affect_emotion_episode",
            "affect_episode_source_event",
            "affect_core_snapshot",
            "affect_outbox",
            "affect_trace",
            "affect_metric",
            "affect_forget_tombstone",
        )
        connection = self._read_connection()
        return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def pragma_health(self) -> JsonDict:
        connection = self._read_connection()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        return {
            "integrity": integrity,
            "foreign_keys": foreign_keys,
            "journal_mode": journal_mode,
            "payload_protection": self._payload_protector.method,
        }

    def operational_status_counts(self) -> JsonDict:
        rows = self._read_connection().execute(
            """
            SELECT 'semantic' AS family, semantic_status AS status, COUNT(*) AS count
            FROM affect_stimulus_event WHERE sora_id = ? GROUP BY semantic_status
            UNION ALL
            SELECT 'outbox' AS family, status, COUNT(*) AS count
            FROM affect_outbox WHERE sora_id = ? GROUP BY status
            """,
            (self.sora_id, self.sora_id),
        ).fetchall()
        result: JsonDict = {"semantic": {}, "outbox": {}}
        for row in rows:
            result[str(row["family"])][str(row["status"])] = int(row["count"])
        return result

    def metric_totals(self, names: list[str]) -> dict[str, float]:
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        rows = self._read_connection().execute(
            f"""
            SELECT name, COALESCE(SUM(value), 0.0) AS total
            FROM affect_metric WHERE sora_id = ? AND name IN ({placeholders}) GROUP BY name
            """,
            (self.sora_id, *names),
        ).fetchall()
        totals = {name: 0.0 for name in names}
        totals.update({str(row["name"]): float(row["total"]) for row in rows})
        return totals

    def _migrate_schema(self) -> None:
        event_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(affect_stimulus_event)").fetchall()
        }
        if "semantic_next_attempt_at" not in event_columns:
            self._conn.execute(
                "ALTER TABLE affect_stimulus_event ADD COLUMN semantic_next_attempt_at TEXT"
            )
        episode_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(affect_emotion_episode)").fetchall()
        }
        for name in ("source_event_ids_json", "source_event_kinds_json", "source_types_json"):
            if name not in episode_columns:
                self._conn.execute(
                    f"ALTER TABLE affect_emotion_episode ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'"
                )
        self._conn.execute(
            """
            UPDATE affect_emotion_episode
            SET source_event_ids_json = json_array(event_id)
            WHERE source_event_ids_json = '[]'
            """
        )
        self._conn.execute(
            """
            UPDATE affect_emotion_episode
            SET source_event_kinds_json = COALESCE(
                (SELECT json_array(event_kind) FROM affect_stimulus_event ev
                 WHERE ev.event_id = affect_emotion_episode.event_id),
                '[]'
            )
            WHERE source_event_kinds_json = '[]'
            """
        )
        self._conn.execute(
            """
            UPDATE affect_emotion_episode
            SET source_types_json = COALESCE(
                (SELECT json_array(source_type) FROM affect_stimulus_event ev
                 WHERE ev.event_id = affect_emotion_episode.event_id),
                '[]'
            )
            WHERE source_types_json = '[]'
            """
        )
        event_ref_rows = self._conn.execute(
            "SELECT event_id, evidence_refs_json FROM affect_stimulus_event WHERE sora_id = ?",
            (self.sora_id,),
        ).fetchall()
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO affect_event_evidence_ref (sora_id, ref_hash, event_id)
            VALUES (?, ?, ?)
            """,
            [
                (self.sora_id, _opaque_hash(str(ref)), str(row["event_id"]))
                for row in event_ref_rows
                for ref in json_loads(row["evidence_refs_json"], [])
            ],
        )
        appraisal_ref_rows = self._conn.execute(
            "SELECT appraisal_id, event_id, evidence_refs_json FROM affect_appraisal WHERE sora_id = ?",
            (self.sora_id,),
        ).fetchall()
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO affect_appraisal_evidence_ref
                (sora_id, ref_hash, appraisal_id, event_id)
            VALUES (?, ?, ?, ?)
            """,
            [
                (self.sora_id, _opaque_hash(str(ref)), str(row["appraisal_id"]), str(row["event_id"]))
                for row in appraisal_ref_rows
                for ref in json_loads(row["evidence_refs_json"], [])
            ],
        )
        episode_source_rows = self._conn.execute(
            "SELECT episode_id, event_id, source_event_ids_json FROM affect_emotion_episode WHERE sora_id = ?",
            (self.sora_id,),
        ).fetchall()
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO affect_episode_source_event (sora_id, episode_id, event_id)
            VALUES (?, ?, ?)
            """,
            [
                (self.sora_id, str(row["episode_id"]), str(event_id))
                for row in episode_source_rows
                for event_id in json_loads(row["source_event_ids_json"], [row["event_id"]])
            ],
        )
        self._conn.execute(
            "UPDATE affect_instance SET schema_version = ? WHERE schema_version < ?",
            (AFFECT_SCHEMA_VERSION, AFFECT_SCHEMA_VERSION),
        )

    def _ensure_instance(self, profile: JsonDict) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT sora_id FROM affect_instance WHERE sora_id = ?",
                (self.sora_id,),
            ).fetchone()
            if existing is not None:
                return
            now = utc_now()
            snapshot = neutral_snapshot(
                sora_id=self.sora_id,
                snapshot_id=new_id("asnap"),
                observed_at=now,
                profile=profile,
            )
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO affect_instance (
                        sora_id, schema_version, active_snapshot_id, state_revision,
                        profile_version, profile_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        self.sora_id,
                        AFFECT_SCHEMA_VERSION,
                        snapshot["snapshot_id"],
                        profile["version"],
                        json_dumps(profile),
                        now,
                        now,
                    ),
                )
                self._insert_snapshot_locked(snapshot, source_contribution_count=0, now=now)
                self._conn.execute(
                    """
                    INSERT INTO affect_control (
                        sora_id, user_id, processing_paused, expression_permission, updated_at
                    ) VALUES (?, '', 0, 'implicit', ?)
                    """,
                    (self.sora_id, now),
                )

    def _insert_snapshot_locked(self, snapshot: Mapping[str, Any], *, source_contribution_count: int, now: str) -> None:
        checksum = snapshot_checksum(snapshot)
        self._conn.execute(
            """
            INSERT INTO affect_core_snapshot (
                snapshot_id, sora_id, state_revision, observed_at, snapshot_json,
                checksum, source_contribution_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["snapshot_id"],
                self.sora_id,
                snapshot["state_revision"],
                snapshot["observed_at"],
                json_dumps(dict(snapshot)),
                checksum,
                source_contribution_count,
                now,
            ),
        )
        self._conn.execute(
            """
            UPDATE affect_instance
            SET active_snapshot_id = ?, state_revision = ?, updated_at = ?
            WHERE sora_id = ?
            """,
            (snapshot["snapshot_id"], snapshot["state_revision"], now, self.sora_id),
        )

    def _assert_next_revision_locked(self, snapshot: Mapping[str, Any]) -> None:
        row = self._conn.execute(
            "SELECT state_revision FROM affect_instance WHERE sora_id = ?",
            (self.sora_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Affect instance not found: {self.sora_id}")
        expected = int(row["state_revision"]) + 1
        actual = int(snapshot.get("state_revision", -1))
        if actual != expected:
            raise RuntimeError(
                f"AFF-DYN-201: optimistic revision conflict; expected {expected}, got {actual}"
            )

    def _insert_appraisal_locked(self, appraisal: AffectAppraisal, *, now: str) -> None:
        factors = {
            key: getattr(appraisal, key)
            for key in (
                "persona_sensitivity",
                "relationship_modifier",
                "goal_importance",
                "novelty_modifier",
                "capacity_modifier",
                "source_confidence",
            )
        }
        self._conn.execute(
            """
            INSERT INTO affect_appraisal (
                appraisal_id, event_id, sora_id, lane, revision, emotion_type,
                target_kind, target_ref, vector_json, strength, confidence, factors_json,
                evidence_refs_json, reason_codes_json, occurred_at, effective_at,
                provider_trace_json, supersedes_appraisal_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appraisal.appraisal_id,
                appraisal.event_id,
                appraisal.sora_id,
                appraisal.lane,
                appraisal.revision,
                appraisal.emotion_type,
                appraisal.target_kind,
                appraisal.target_ref,
                json_dumps(dict(appraisal.vector)),
                appraisal.strength,
                appraisal.confidence,
                json_dumps(factors),
                json_dumps(list(appraisal.evidence_refs)),
                json_dumps(list(appraisal.reason_codes)),
                appraisal.occurred_at,
                appraisal.effective_at,
                json_dumps(dict(appraisal.provider_trace)),
                appraisal.supersedes_appraisal_id,
                now,
            ),
        )
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO affect_appraisal_evidence_ref
                (sora_id, ref_hash, appraisal_id, event_id)
            VALUES (?, ?, ?, ?)
            """,
            [
                (self.sora_id, _opaque_hash(ref), appraisal.appraisal_id, appraisal.event_id)
                for ref in sorted(set(appraisal.evidence_refs))
            ],
        )

    def _insert_contribution_locked(self, contribution: Mapping[str, Any], *, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO affect_contribution (
                contribution_id, sora_id, event_id, appraisal_id, effective_at,
                core_delta_json, mood_delta_json, capacity_cost_json, need_delta_json,
                emotion_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contribution["contribution_id"],
                self.sora_id,
                contribution["event_id"],
                contribution["appraisal_id"],
                contribution["effective_at"],
                json_dumps(dict(contribution.get("core_delta") or {})),
                json_dumps(dict(contribution.get("mood_delta") or {})),
                json_dumps(dict(contribution.get("capacity_cost") or {})),
                json_dumps(dict(contribution.get("need_delta") or {})),
                json_dumps(dict(contribution.get("emotion") or {})),
                now,
            ),
        )

    def _upsert_episode_locked(
        self,
        appraisal: AffectAppraisal,
        emotion: Mapping[str, Any],
        *,
        relationship_ref: str | None,
        conversation_scope: str,
        now: str,
    ) -> str:
        effective = datetime.fromisoformat(appraisal.effective_at.replace("Z", "+00:00"))
        if effective.tzinfo is None:
            effective = effective.replace(tzinfo=timezone.utc)
        merge_window = max(0, int(self.profile().get("episode_merge_window_seconds", 1800)))
        merge_cutoff = (effective - timedelta(seconds=merge_window)).isoformat()
        source = self._conn.execute(
            "SELECT event_kind, source_type FROM affect_stimulus_event WHERE event_id = ?",
            (appraisal.event_id,),
        ).fetchone()
        source_event_kind = str(source["event_kind"]) if source is not None else "unknown"
        source_type = str(source["source_type"]) if source is not None else "unknown"
        existing = self._conn.execute(
            """
            SELECT * FROM affect_emotion_episode
            WHERE sora_id = ? AND active = 1 AND emotion_type = ?
              AND target_kind = ? AND COALESCE(target_ref, '') = COALESCE(?, '')
              AND conversation_scope = ? AND started_at >= ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (
                self.sora_id,
                appraisal.emotion_type,
                appraisal.target_kind,
                appraisal.target_ref,
                conversation_scope,
                merge_cutoff,
            ),
        ).fetchone()
        if existing is not None:
            peak = max(float(existing["peak_intensity"]), float(emotion.get("peak_intensity", 0.0)))
            current = max(float(existing["current_intensity"]), float(emotion.get("intensity", 0.0)))
            refs = sorted(
                set(json_loads(existing["cause_refs_json"], [])) | set(appraisal.evidence_refs)
            )
            event_ids = sorted(
                set(json_loads(existing["source_event_ids_json"], [existing["event_id"]]))
                | {appraisal.event_id}
            )
            event_kinds = sorted(
                set(json_loads(existing["source_event_kinds_json"], []))
                | {source_event_kind}
            )
            source_types = sorted(
                set(json_loads(existing["source_types_json"], []))
                | {source_type}
            )
            self._conn.execute(
                """
                UPDATE affect_emotion_episode
                SET event_id = ?, appraisal_id = ?, cause_refs_json = ?,
                    source_event_ids_json = ?, source_event_kinds_json = ?, source_types_json = ?,
                    peak_intensity = ?,
                    current_intensity = ?, persistence = ?, resolution_state = ?, updated_at = ?
                WHERE episode_id = ?
                """,
                (
                    appraisal.event_id,
                    appraisal.appraisal_id,
                    json_dumps(refs),
                    json_dumps(event_ids),
                    json_dumps(event_kinds),
                    json_dumps(source_types),
                    peak,
                    current,
                    emotion.get("persistence", 0.0),
                    emotion.get("resolution_state", "rising"),
                    now,
                    existing["episode_id"],
                ),
            )
            self._replace_episode_source_events_locked(str(existing["episode_id"]), event_ids)
            return str(existing["episode_id"])
        episode_id = new_id("aepisode")
        self._conn.execute(
            """
            INSERT INTO affect_emotion_episode (
                episode_id, sora_id, event_id, appraisal_id, emotion_type,
                target_kind, target_ref, relationship_ref, conversation_scope,
                cause_refs_json, source_event_ids_json, source_event_kinds_json, source_types_json,
                peak_intensity, current_intensity, persistence,
                resolution_state, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                self.sora_id,
                appraisal.event_id,
                appraisal.appraisal_id,
                appraisal.emotion_type,
                appraisal.target_kind,
                appraisal.target_ref,
                relationship_ref,
                conversation_scope,
                json_dumps(list(appraisal.evidence_refs)),
                json_dumps([appraisal.event_id]),
                json_dumps([source_event_kind]),
                json_dumps([source_type]),
                emotion.get("peak_intensity", 0.0),
                emotion.get("intensity", 0.0),
                emotion.get("persistence", 0.0),
                emotion.get("resolution_state", "rising"),
                appraisal.effective_at,
                now,
            ),
        )
        self._replace_episode_source_events_locked(episode_id, [appraisal.event_id])
        return episode_id

    def _replace_episode_source_events_locked(self, episode_id: str, event_ids: list[str]) -> None:
        self._conn.execute(
            "DELETE FROM affect_episode_source_event WHERE sora_id = ? AND episode_id = ?",
            (self.sora_id, episode_id),
        )
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO affect_episode_source_event (sora_id, episode_id, event_id)
            VALUES (?, ?, ?)
            """,
            [(self.sora_id, episode_id, event_id) for event_id in sorted(set(event_ids))],
        )

    def _refresh_episodes_locked(
        self,
        event_ids: set[str],
        *,
        episode_ids: set[str] | None = None,
        now: str,
        reason: str,
    ) -> JsonDict:
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            targets = self._conn.execute(
                f"""
                SELECT * FROM affect_emotion_episode
                WHERE sora_id = ? AND active = 1 AND episode_id IN ({placeholders})
                """,
                (self.sora_id, *sorted(episode_ids)),
            ).fetchall()
        elif event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            targets = self._conn.execute(
                f"""
                SELECT DISTINCT episode.* FROM affect_emotion_episode episode
                JOIN affect_episode_source_event source ON source.episode_id = episode.episode_id
                WHERE episode.sora_id = ? AND episode.active = 1
                  AND source.event_id IN ({placeholders})
                """,
                (self.sora_id, *sorted(event_ids)),
            ).fetchall()
        else:
            targets = []
        deactivated = 0
        rebuilt = 0
        for row in targets:
            source_ids = [
                str(item)
                for item in json_loads(row["source_event_ids_json"], [row["event_id"]])
            ]
            if not source_ids:
                source_ids = [str(row["event_id"])]
            placeholders = ",".join("?" for _ in source_ids)
            active_rows = self._conn.execute(
                f"""
                SELECT c.*, a.evidence_refs_json, a.effective_at AS appraisal_effective_at,
                       ev.event_kind, ev.source_type
                FROM affect_contribution c
                JOIN affect_appraisal a ON a.appraisal_id = c.appraisal_id AND a.active = 1
                JOIN affect_stimulus_event ev ON ev.event_id = c.event_id
                WHERE c.sora_id = ? AND c.active = 1 AND c.event_id IN ({placeholders})
                  AND json_extract(c.emotion_json, '$.emotion_type') = ?
                ORDER BY c.effective_at ASC, c.contribution_id ASC
                """,
                (self.sora_id, *source_ids, row["emotion_type"]),
            ).fetchall()
            if not active_rows:
                self._conn.execute(
                    """
                    UPDATE affect_emotion_episode
                    SET active = 0, resolution_state = 'forgotten', resolved_at = ?, updated_at = ?
                    WHERE episode_id = ?
                    """,
                    (now, now, row["episode_id"]),
                )
                deactivated += 1
                continue
            emotions = [json_loads(item["emotion_json"], {}) for item in active_rows]
            latest = active_rows[-1]
            cause_refs = sorted(
                {
                    str(ref)
                    for item in active_rows
                    for ref in json_loads(item["evidence_refs_json"], [])
                }
            )
            remaining_event_ids = sorted({str(item["event_id"]) for item in active_rows})
            self._conn.execute(
                """
                UPDATE affect_emotion_episode
                SET event_id = ?, appraisal_id = ?, cause_refs_json = ?,
                    source_event_ids_json = ?, source_event_kinds_json = ?, source_types_json = ?,
                    peak_intensity = ?, current_intensity = ?, persistence = ?,
                    resolution_state = ?, resolved_at = NULL, updated_at = ?
                WHERE episode_id = ?
                """,
                (
                    latest["event_id"],
                    latest["appraisal_id"],
                    json_dumps(cause_refs),
                    json_dumps(remaining_event_ids),
                    json_dumps(sorted({str(item["event_kind"]) for item in active_rows})),
                    json_dumps(sorted({str(item["source_type"]) for item in active_rows})),
                    max(float(item.get("peak_intensity", 0.0)) for item in emotions),
                    max(float(item.get("intensity", 0.0)) for item in emotions),
                    max(float(item.get("persistence", 0.0)) for item in emotions),
                    str(emotions[-1].get("resolution_state") or "rising"),
                    now,
                    row["episode_id"],
                ),
            )
            self._replace_episode_source_events_locked(str(row["episode_id"]), remaining_event_ids)
            rebuilt += 1
        if targets:
            self._insert_trace_locked(
                stage="episode_refresh",
                status="applied",
                details={
                    "reason": reason,
                    "target_count": len(targets),
                    "rebuilt": rebuilt,
                    "deactivated": deactivated,
                },
            )
        return {"target_count": len(targets), "rebuilt": rebuilt, "deactivated": deactivated}

    def _insert_trace_locked(
        self,
        *,
        stage: str,
        status: str,
        details: Mapping[str, Any],
        event_id: str | None = None,
        appraisal_id: str | None = None,
        snapshot_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        trace_id = new_id("atrace")
        self._conn.execute(
            """
            INSERT INTO affect_trace (
                trace_id, sora_id, correlation_id, event_id, appraisal_id,
                snapshot_id, stage, status, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                self.sora_id,
                correlation_id,
                event_id,
                appraisal_id,
                snapshot_id,
                stage,
                status,
                json_dumps(dict(details)),
                utc_now(),
            ),
        )
        return trace_id

    def _event_from_row(self, row: sqlite3.Row, *, include_raw_text: bool = False) -> JsonDict:
        payload = {
            "event_id": row["event_id"],
            "sora_id": row["sora_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "source_revision": row["source_revision"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "actor_user_id": row["actor_user_id"],
            "target_kind": row["target_kind"],
            "target_ref": row["target_ref"],
            "relationship_ref": row["relationship_ref"],
            "conversation_scope": row["conversation_scope"],
            "source_confidence": float(row["source_confidence"]),
            "event_kind": row["event_kind"],
            "payload": json_loads(row["payload_json"], {}),
            "evidence_refs": json_loads(row["evidence_refs_json"], []),
            "content_hash": row["content_hash"],
            "sensitivity": row["sensitivity"],
            "correlation_id": row["correlation_id"],
            "semantic_status": row["semantic_status"],
            "semantic_attempts": int(row["semantic_attempts"]),
            "semantic_error": row["semantic_error"],
            "semantic_next_attempt_at": row["semantic_next_attempt_at"],
            "retention_expires_at": row["retention_expires_at"],
            "processed_at": row["processed_at"],
        }
        if include_raw_text:
            payload["untrusted_text"] = (
                self._payload_protector.unprotect(row["raw_text"])
                if row["raw_text"]
                else None
            )
        return payload

    @staticmethod
    def _appraisal_from_row(row: sqlite3.Row) -> AffectAppraisal:
        factors = json_loads(row["factors_json"], {})
        return AffectAppraisal(
            appraisal_id=row["appraisal_id"],
            event_id=row["event_id"],
            sora_id=row["sora_id"],
            lane=row["lane"],
            revision=int(row["revision"]),
            emotion_type=row["emotion_type"],
            target_kind=row["target_kind"],
            target_ref=row["target_ref"],
            vector=json_loads(row["vector_json"], {}),
            strength=float(row["strength"]),
            confidence=float(row["confidence"]),
            persona_sensitivity=float(factors.get("persona_sensitivity", 0.0)),
            relationship_modifier=float(factors.get("relationship_modifier", 0.0)),
            goal_importance=float(factors.get("goal_importance", 0.0)),
            novelty_modifier=float(factors.get("novelty_modifier", 0.0)),
            capacity_modifier=float(factors.get("capacity_modifier", 0.0)),
            source_confidence=float(factors.get("source_confidence", 0.0)),
            evidence_refs=tuple(json_loads(row["evidence_refs_json"], [])),
            reason_codes=tuple(json_loads(row["reason_codes_json"], [])),
            occurred_at=row["occurred_at"],
            effective_at=row["effective_at"],
            provider_trace=json_loads(row["provider_trace_json"], {}),
            supersedes_appraisal_id=row["supersedes_appraisal_id"],
        )

    @staticmethod
    def _contribution_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "contribution_id": row["contribution_id"],
            "sora_id": row["sora_id"],
            "event_id": row["event_id"],
            "appraisal_id": row["appraisal_id"],
            "effective_at": row["effective_at"],
            "core_delta": json_loads(row["core_delta_json"], {}),
            "mood_delta": json_loads(row["mood_delta_json"], {}),
            "capacity_cost": json_loads(row["capacity_cost_json"], {}),
            "need_delta": json_loads(row["need_delta_json"], {}),
            "emotion": json_loads(row["emotion_json"], {}),
            "active": bool(row["active"]),
            "deactivation_reason": row["deactivation_reason"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> JsonDict:
        payload = {
            "episode_id": row["episode_id"],
            "event_id": row["event_id"],
            "appraisal_id": row["appraisal_id"],
            "emotion_type": row["emotion_type"],
            "target_kind": row["target_kind"],
            "target_ref": row["target_ref"],
            "relationship_ref": row["relationship_ref"],
            "conversation_scope": row["conversation_scope"],
            "cause_refs": json_loads(row["cause_refs_json"], []),
            "source_event_ids": json_loads(row["source_event_ids_json"], [row["event_id"]]),
            "source_event_kinds": json_loads(row["source_event_kinds_json"], []),
            "source_types": json_loads(row["source_types_json"], []),
            "peak_intensity": float(row["peak_intensity"]),
            "current_intensity": float(row["current_intensity"]),
            "persistence": float(row["persistence"]),
            "resolution_state": row["resolution_state"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
            "active": bool(row["active"]),
            "memory_proposal_id": row["memory_proposal_id"],
        }
        if "source_event_kind" in row.keys():
            payload["source_event_kind"] = row["source_event_kind"]
            payload["source_type"] = row["source_type"]
            payload["source_sensitivity"] = row["source_sensitivity"]
        return payload

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "event_key": row["event_key"],
            "payload_hash": row["payload_hash"],
            "payload": json_loads(row["payload_json"], {}),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "max_attempts": int(row["max_attempts"]),
            "next_attempt_at": row["next_attempt_at"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
        }


def _opaque_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
