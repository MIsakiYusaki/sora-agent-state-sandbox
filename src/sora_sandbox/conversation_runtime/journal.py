from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Sequence

from .contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ClientEvent,
    JsonDict,
    TurnExecutionContext,
    TurnKind,
    TurnStatus,
)
from .security import AtRestCipher


SCHEMA_VERSION = 1


class JournalConflict(RuntimeError):
    pass


class InvalidTurnTransition(RuntimeError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def content_hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


class ConversationJournal:
    """SQLite source of truth for turn execution, client events, and outbox delivery."""

    def __init__(self, path: Path, cipher: AtRestCipher) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        with self.transaction() as connection:
            for table in (
                "runtime_consumer_receipt",
                "runtime_model_call",
                "runtime_outbox",
                "runtime_client_event",
                "runtime_short_turn",
                "runtime_artifact",
                "runtime_execution_lease",
                "runtime_turn",
            ):
                connection.execute(f"DELETE FROM {table}")

    def health(self) -> JsonDict:
        with self._lock:
            journal_mode = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            foreign_keys = int(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
            schema_version = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM runtime_schema_version"
                ).fetchone()[0]
            )
            pending_outbox = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM runtime_outbox WHERE status IN ('pending', 'processing')"
                ).fetchone()[0]
            )
            dead_letters = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM runtime_outbox WHERE status='dead_letter'"
                ).fetchone()[0]
            )
        return {
            "path": str(self.path),
            "journal_mode": journal_mode,
            "foreign_keys": foreign_keys,
            "schema_version": schema_version,
            "pending_outbox": pending_outbox,
            "dead_letters": dead_letters,
        }

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_turn (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    turn_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    input_cipher TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    result_cipher TEXT,
                    result_hash TEXT,
                    error_code TEXT,
                    error_detail_cipher TEXT,
                    deadline_at_ms INTEGER,
                    visible_output_started INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    committed_at_ms INTEGER,
                    revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(conversation_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_turn_conversation_created
                    ON runtime_turn(conversation_id, created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_runtime_turn_status
                    ON runtime_turn(status, updated_at_ms);

                CREATE TABLE IF NOT EXISTS runtime_artifact (
                    artifact_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload_cipher TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE(turn_id, artifact_type, payload_hash)
                );

                CREATE TABLE IF NOT EXISTS runtime_short_turn (
                    history_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content_cipher TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE(conversation_id, sequence),
                    UNIQUE(turn_id, role)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_short_turn_conversation
                    ON runtime_short_turn(conversation_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS runtime_client_event (
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_cipher TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(turn_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS runtime_outbox (
                    event_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_cipher TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at_ms INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_until_ms INTEGER,
                    last_error TEXT,
                    created_at_ms INTEGER NOT NULL,
                    delivered_at_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_outbox_delivery
                    ON runtime_outbox(status, available_at_ms, created_at_ms);

                CREATE TABLE IF NOT EXISTS runtime_consumer_receipt (
                    consumer_name TEXT NOT NULL,
                    event_id TEXT NOT NULL REFERENCES runtime_outbox(event_id) ON DELETE CASCADE,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    processed_at_ms INTEGER NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(consumer_name, event_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_execution_lease (
                    conversation_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL,
                    acquired_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS runtime_model_call (
                    call_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES runtime_turn(turn_id) ON DELETE CASCADE,
                    logical_task TEXT NOT NULL,
                    route TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL,
                    thinking_disabled INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at_ms INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_schema_version(version, applied_at_ms) VALUES (?, ?)",
                (SCHEMA_VERSION, now_ms()),
            )

    def create_turn(
        self,
        *,
        conversation_id: str,
        request_id: str,
        user_input: str,
        turn_kind: TurnKind = TurnKind.REACTIVE,
        branch_id: str = "main",
        deadline_at_ms: int | None = None,
        correlation_id: str | None = None,
        turn_id: str | None = None,
    ) -> tuple[TurnExecutionContext, bool]:
        if not conversation_id.strip() or not request_id.strip():
            raise ValueError("conversation_id and request_id are required")
        created_at = now_ms()
        actual_turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
        actual_correlation_id = correlation_id or f"corr_{uuid.uuid4().hex}"
        associated_data = f"turn-input:{actual_turn_id}"
        encrypted_input = self.cipher.encrypt_text(user_input, associated_data=associated_data)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runtime_turn WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != content_hash(user_input):
                    raise JournalConflict("request_id was already used with different input")
                return self._row_to_context(existing), False
            connection.execute(
                """
                INSERT INTO runtime_turn (
                    turn_id, conversation_id, request_id, correlation_id, branch_id,
                    turn_kind, status, stage, input_cipher, input_hash,
                    deadline_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actual_turn_id,
                    conversation_id,
                    request_id,
                    actual_correlation_id,
                    branch_id,
                    turn_kind.value,
                    TurnStatus.ACCEPTED.value,
                    "accept",
                    encrypted_input,
                    content_hash(user_input),
                    deadline_at_ms,
                    created_at,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (actual_turn_id,)
            ).fetchone()
        return self._row_to_context(row), True

    def get_turn(self, turn_id: str) -> TurnExecutionContext:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"turn not found: {turn_id}")
        return self._row_to_context(row)

    def turn_by_request(
        self, conversation_id: str, request_id: str
    ) -> TurnExecutionContext | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_turn WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
        return self._row_to_context(row) if row is not None else None

    def find_request(self, conversation_id: str, request_id: str) -> TurnExecutionContext | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_turn WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
        return self._row_to_context(row) if row is not None else None

    def read_input(self, turn_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT input_cipher FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"turn not found: {turn_id}")
        return self.cipher.decrypt_text(
            str(row["input_cipher"]), associated_data=f"turn-input:{turn_id}"
        )

    def read_result(self, turn_id: str) -> JsonDict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_cipher FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"turn not found: {turn_id}")
        if row["result_cipher"] is None:
            return None
        return self.cipher.decrypt_json(
            str(row["result_cipher"]), associated_data=f"turn-result:{turn_id}"
        )

    def transition(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        stage: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        visible_output_started: bool | None = None,
    ) -> TurnExecutionContext:
        timestamp = now_ms()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"turn not found: {turn_id}")
            current = TurnStatus(str(row["status"]))
            if status != current and status not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTurnTransition(f"{current.value} -> {status.value} is not allowed")
            detail_cipher = (
                self.cipher.encrypt_text(error_detail, associated_data=f"turn-error:{turn_id}")
                if error_detail
                else row["error_detail_cipher"]
            )
            visible = (
                int(visible_output_started)
                if visible_output_started is not None
                else int(row["visible_output_started"])
            )
            committed_at = timestamp if status == TurnStatus.COMMITTED else row["committed_at_ms"]
            connection.execute(
                """
                UPDATE runtime_turn
                SET status=?, stage=?, error_code=?, error_detail_cipher=?,
                    visible_output_started=?, updated_at_ms=?, committed_at_ms=?, revision=revision+1
                WHERE turn_id=?
                """,
                (
                    status.value,
                    stage,
                    error_code,
                    detail_cipher,
                    visible,
                    timestamp,
                    committed_at,
                    turn_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
        return self._row_to_context(updated)

    def put_artifact(
        self,
        turn_id: str,
        artifact_type: str,
        payload: JsonDict,
        *,
        schema_version: str,
    ) -> str:
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = content_hash(serialized)
        encrypted = self.cipher.encrypt_json(payload, associated_data=f"artifact:{turn_id}:{payload_hash}")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO runtime_artifact (
                    artifact_id, turn_id, artifact_type, schema_version,
                    payload_cipher, payload_hash, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    turn_id,
                    artifact_type,
                    schema_version,
                    encrypted,
                    payload_hash,
                    now_ms(),
                ),
            )
            row = connection.execute(
                "SELECT artifact_id FROM runtime_artifact WHERE turn_id=? AND artifact_type=? AND payload_hash=?",
                (turn_id, artifact_type, payload_hash),
            ).fetchone()
        return str(row["artifact_id"])

    def artifacts(self, turn_id: str, artifact_type: str | None = None) -> list[JsonDict]:
        query = "SELECT * FROM runtime_artifact WHERE turn_id=?"
        params: tuple[Any, ...] = (turn_id,)
        if artifact_type is not None:
            query += " AND artifact_type=?"
            params += (artifact_type,)
        query += " ORDER BY created_at_ms, artifact_id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        result: list[JsonDict] = []
        for row in rows:
            payload = self.cipher.decrypt_json(
                str(row["payload_cipher"]),
                associated_data=f"artifact:{turn_id}:{row['payload_hash']}",
            )
            result.append(
                {
                    "artifact_id": row["artifact_id"],
                    "artifact_type": row["artifact_type"],
                    "schema_version": row["schema_version"],
                    "payload_hash": row["payload_hash"],
                    "payload": payload,
                    "created_at_ms": row["created_at_ms"],
                }
            )
        return result

    def append_client_event(self, turn_id: str, event_type: str, payload: JsonDict) -> ClientEvent:
        timestamp = now_ms()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_digest = content_hash(serialized)
        with self.transaction() as connection:
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_client_event WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()[0]
            )
            encrypted = self.cipher.encrypt_json(
                payload, associated_data=f"client-event:{turn_id}:{sequence}"
            )
            connection.execute(
                """
                INSERT INTO runtime_client_event (
                    turn_id, sequence, event_type, payload_cipher, payload_hash, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (turn_id, sequence, event_type, encrypted, payload_digest, timestamp),
            )
        return ClientEvent(turn_id, sequence, event_type, payload, timestamp)

    def client_events(self, turn_id: str, *, after_sequence: int = 0) -> list[ClientEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM runtime_client_event
                WHERE turn_id=? AND sequence>? ORDER BY sequence
                """,
                (turn_id, max(0, after_sequence)),
            ).fetchall()
        return [
            ClientEvent(
                turn_id=str(row["turn_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                payload=self.cipher.decrypt_json(
                    str(row["payload_cipher"]),
                    associated_data=f"client-event:{turn_id}:{row['sequence']}",
                ),
                created_at_ms=int(row["created_at_ms"]),
            )
            for row in rows
        ]

    def commit_turn(
        self,
        turn_id: str,
        *,
        user_input: str,
        reply: str,
        result: JsonDict,
        outbox_events: Sequence[tuple[str, JsonDict]],
    ) -> TurnExecutionContext:
        """Atomically commits final result, canonical short history, status, and outbox."""
        timestamp = now_ms()
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result_digest = content_hash(serialized)
        result_cipher = self.cipher.encrypt_json(result, associated_data=f"turn-result:{turn_id}")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"turn not found: {turn_id}")
            current = TurnStatus(str(row["status"]))
            if current == TurnStatus.COMMITTED:
                if row["result_hash"] != result_digest:
                    raise JournalConflict("turn already committed with a different result")
                return self._row_to_context(row)
            if TurnStatus.COMMITTED not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTurnTransition(f"{current.value} -> committed is not allowed")
            conversation_id = str(row["conversation_id"])
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_short_turn WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            for offset, (role, content) in enumerate((("user", user_input), ("sora", reply))):
                history_id = f"history_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO runtime_short_turn (
                        history_id, conversation_id, turn_id, sequence, role,
                        content_cipher, content_hash, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        history_id,
                        conversation_id,
                        turn_id,
                        next_sequence + offset,
                        role,
                        self.cipher.encrypt_text(
                            content, associated_data=f"history:{history_id}"
                        ),
                        content_hash(content),
                        timestamp,
                    ),
                )
            for event_type, payload in outbox_events:
                event_id = f"outbox_{uuid.uuid4().hex}"
                idempotency_key = f"{turn_id}:{event_type}"
                payload_serialized = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                payload_digest = content_hash(payload_serialized)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO runtime_outbox (
                        event_id, turn_id, event_type, idempotency_key,
                        payload_cipher, payload_hash, status, available_at_ms, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        event_id,
                        turn_id,
                        event_type,
                        idempotency_key,
                        self.cipher.encrypt_json(
                            payload, associated_data=f"outbox:{idempotency_key}"
                        ),
                        payload_digest,
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE runtime_turn
                SET status='committed', stage='committed', result_cipher=?, result_hash=?,
                    updated_at_ms=?, committed_at_ms=?, revision=revision+1
                WHERE turn_id=?
                """,
                (result_cipher, result_digest, timestamp, timestamp, turn_id),
            )
            committed = connection.execute(
                "SELECT * FROM runtime_turn WHERE turn_id=?", (turn_id,)
            ).fetchone()
        return self._row_to_context(committed)

    def recent_turns(self, conversation_id: str, *, limit: int = 40) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM runtime_short_turn WHERE conversation_id=?
                ORDER BY sequence DESC LIMIT ?
                """,
                (conversation_id, max(1, limit)),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.append(
                {
                    "role": row["role"],
                    "content": self.cipher.decrypt_text(
                        str(row["content_cipher"]),
                        associated_data=f"history:{row['history_id']}",
                    ),
                    "turn_id": row["turn_id"],
                    "sequence": row["sequence"],
                    "created_at_ms": row["created_at_ms"],
                }
            )
        return result

    def import_legacy_history(
        self,
        conversation_id: str,
        turns: Sequence[JsonDict],
    ) -> int:
        """One-time encrypted migration for plaintext history from the legacy state file."""
        bounded = list(turns)[-40:]
        if not bounded:
            return 0
        with self.transaction() as connection:
            existing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_short_turn WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            if existing:
                return 0
            timestamp = now_ms()
            for sequence, item in enumerate(bounded, 1):
                role = str(item.get("role") or "user")
                content = str(item.get("content") or "")
                turn_id = f"legacy_{uuid.uuid4().hex}"
                request_id = f"legacy-history-{sequence}-{content_hash(content)[:16]}"
                result = {"migrated_legacy_history": True, "role": role}
                result_serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    """
                    INSERT INTO runtime_turn (
                        turn_id, conversation_id, request_id, correlation_id, branch_id,
                        turn_kind, status, stage, input_cipher, input_hash,
                        result_cipher, result_hash, visible_output_started,
                        created_at_ms, updated_at_ms, committed_at_ms
                    ) VALUES (?, ?, ?, ?, 'legacy-migration', 'reactive', 'committed',
                              'legacy_history_migration', ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        conversation_id,
                        request_id,
                        f"legacy_{uuid.uuid4().hex}",
                        self.cipher.encrypt_text("", associated_data=f"turn-input:{turn_id}"),
                        content_hash(""),
                        self.cipher.encrypt_json(result, associated_data=f"turn-result:{turn_id}"),
                        content_hash(result_serialized),
                        timestamp + sequence,
                        timestamp + sequence,
                        timestamp + sequence,
                    ),
                )
                history_id = f"history_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO runtime_short_turn (
                        history_id, conversation_id, turn_id, sequence, role,
                        content_cipher, content_hash, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        history_id,
                        conversation_id,
                        turn_id,
                        sequence,
                        role,
                        self.cipher.encrypt_text(content, associated_data=f"history:{history_id}"),
                        content_hash(content),
                        int(item.get("at") or timestamp + sequence),
                    ),
                )
        return len(bounded)

    def acquire_lease(
        self,
        conversation_id: str,
        turn_id: str,
        owner_id: str,
        *,
        ttl_ms: int,
    ) -> bool:
        timestamp = now_ms()
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM runtime_execution_lease WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if current is not None and int(current["expires_at_ms"]) > timestamp:
                if current["turn_id"] != turn_id or current["owner_id"] != owner_id:
                    return False
            connection.execute(
                """
                INSERT INTO runtime_execution_lease (
                    conversation_id, turn_id, owner_id, acquired_at_ms, expires_at_ms, revision
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    turn_id=excluded.turn_id,
                    owner_id=excluded.owner_id,
                    acquired_at_ms=excluded.acquired_at_ms,
                    expires_at_ms=excluded.expires_at_ms,
                    revision=runtime_execution_lease.revision+1
                """,
                (conversation_id, turn_id, owner_id, timestamp, timestamp + max(1000, ttl_ms)),
            )
        return True

    def renew_lease(self, conversation_id: str, turn_id: str, owner_id: str, *, ttl_ms: int) -> bool:
        timestamp = now_ms()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_execution_lease
                SET expires_at_ms=?, revision=revision+1
                WHERE conversation_id=? AND turn_id=? AND owner_id=?
                """,
                (timestamp + max(1000, ttl_ms), conversation_id, turn_id, owner_id),
            )
        return cursor.rowcount == 1

    def release_lease(self, conversation_id: str, turn_id: str, owner_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_execution_lease WHERE conversation_id=? AND turn_id=? AND owner_id=?",
                (conversation_id, turn_id, owner_id),
            )
        return cursor.rowcount == 1

    def recover_incomplete(self, *, stale_before_ms: int) -> list[TurnExecutionContext]:
        statuses = tuple(status.value for status in TurnStatus if status not in TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM runtime_turn WHERE status IN ({placeholders}) AND updated_at_ms<? ORDER BY created_at_ms",
                (*statuses, stale_before_ms),
            ).fetchall()
        return [self._row_to_context(row) for row in rows]

    def record_model_call(
        self,
        turn_id: str,
        *,
        logical_task: str,
        route: str,
        model: str | None,
        status: str,
        thinking_disabled: bool,
        usage: JsonDict | None = None,
        latency_ms: float = 0.0,
        error_code: str | None = None,
    ) -> str:
        call_id = f"mcall_{uuid.uuid4().hex}"
        usage = usage or {}
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_model_call (
                    call_id, turn_id, logical_task, route, model, status,
                    thinking_disabled, prompt_tokens, completion_tokens,
                    latency_ms, error_code, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    turn_id,
                    logical_task,
                    route,
                    model,
                    status,
                    int(thinking_disabled),
                    int(usage.get("prompt_tokens", 0) or 0),
                    int(usage.get("completion_tokens", 0) or 0),
                    float(latency_ms),
                    error_code,
                    now_ms(),
                ),
            )
        return call_id

    def claim_outbox(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        lease_ms: int = 30_000,
    ) -> list[JsonDict]:
        timestamp = now_ms()
        claimed: list[JsonDict] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_outbox
                WHERE (status='pending' OR (status='processing' AND lease_until_ms<?))
                  AND available_at_ms<=?
                ORDER BY created_at_ms LIMIT ?
                """,
                (timestamp, timestamp, max(1, limit)),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE runtime_outbox
                    SET status='processing', lease_owner=?, lease_until_ms=?, attempts=attempts+1
                    WHERE event_id=?
                    """,
                    (worker_id, timestamp + lease_ms, row["event_id"]),
                )
                payload = self.cipher.decrypt_json(
                    str(row["payload_cipher"]),
                    associated_data=f"outbox:{row['idempotency_key']}",
                )
                claimed.append(
                    {
                        "event_id": row["event_id"],
                        "turn_id": row["turn_id"],
                        "event_type": row["event_type"],
                        "idempotency_key": row["idempotency_key"],
                        "payload_hash": row["payload_hash"],
                        "payload": payload,
                        "attempts": int(row["attempts"]) + 1,
                    }
                )
        return claimed

    def consumer_receipt(self, consumer_name: str, event_id: str) -> JsonDict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_consumer_receipt WHERE consumer_name=? AND event_id=?",
                (consumer_name, event_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_consumer(
        self,
        *,
        consumer_name: str,
        event_id: str,
        payload_hash: str,
        status: str = "delivered",
        details: JsonDict | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_consumer_receipt (
                    consumer_name, event_id, payload_hash, status, processed_at_ms, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer_name, event_id) DO UPDATE SET
                    status=excluded.status,
                    processed_at_ms=excluded.processed_at_ms,
                    details_json=excluded.details_json
                """,
                (
                    consumer_name,
                    event_id,
                    payload_hash,
                    status,
                    now_ms(),
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def finalize_outbox(
        self, event_id: str, *, delivered: bool, error: str | None = None
    ) -> str:
        timestamp = now_ms()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM runtime_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outbox event not found: {event_id}")
            attempts = int(row["attempts"])
            if delivered:
                connection.execute(
                    """
                    UPDATE runtime_outbox SET status='delivered', delivered_at_ms=?,
                        lease_owner=NULL, lease_until_ms=NULL, last_error=NULL WHERE event_id=?
                    """,
                    (timestamp, event_id),
                )
                status = "delivered"
            else:
                dead_letter = attempts >= 8
                status = "dead_letter" if dead_letter else "pending"
                delay_ms = min(300_000, 1_000 * (2 ** min(attempts, 8)))
                connection.execute(
                    """
                    UPDATE runtime_outbox SET status=?, available_at_ms=?,
                        lease_owner=NULL, lease_until_ms=NULL, last_error=? WHERE event_id=?
                    """,
                    (
                        status,
                        timestamp + delay_ms,
                        (error or "unknown delivery error")[:500],
                        event_id,
                    ),
                )
        return status

    def purge_expired_content(
        self,
        *,
        raw_content_before_ms: int,
        client_events_before_ms: int,
        delivered_outbox_before_ms: int | None = None,
        dead_letter_before_ms: int | None = None,
    ) -> JsonDict:
        """Deletes recoverable content while retaining hashed audit metadata."""
        with self.transaction() as connection:
            event_count = connection.execute(
                "DELETE FROM runtime_client_event WHERE created_at_ms<?",
                (client_events_before_ms,),
            ).rowcount
            artifact_count = connection.execute(
                "DELETE FROM runtime_artifact WHERE created_at_ms<? AND artifact_type IN ('wave_a', 'wave_b', 'prompt')",
                (raw_content_before_ms,),
            ).rowcount
            delivered_count = 0
            if delivered_outbox_before_ms is not None:
                delivered_count = connection.execute(
                    "DELETE FROM runtime_outbox WHERE status='delivered' AND delivered_at_ms<?",
                    (delivered_outbox_before_ms,),
                ).rowcount
            dead_letter_count = 0
            if dead_letter_before_ms is not None:
                dead_letter_count = connection.execute(
                    "DELETE FROM runtime_outbox WHERE status='dead_letter' AND created_at_ms<?",
                    (dead_letter_before_ms,),
                ).rowcount
        return {
            "client_events_deleted": event_count,
            "artifacts_deleted": artifact_count,
            "delivered_outbox_deleted": delivered_count,
            "dead_letters_deleted": dead_letter_count,
        }

    @staticmethod
    def _row_to_context(row: sqlite3.Row) -> TurnExecutionContext:
        return TurnExecutionContext(
            turn_id=str(row["turn_id"]),
            conversation_id=str(row["conversation_id"]),
            request_id=str(row["request_id"]),
            correlation_id=str(row["correlation_id"]),
            branch_id=str(row["branch_id"]),
            turn_kind=TurnKind(str(row["turn_kind"])),
            status=TurnStatus(str(row["status"])),
            stage=str(row["stage"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            deadline_at_ms=(int(row["deadline_at_ms"]) if row["deadline_at_ms"] is not None else None),
            visible_output_started=bool(row["visible_output_started"]),
            committed_at_ms=(int(row["committed_at_ms"]) if row["committed_at_ms"] is not None else None),
            revision=int(row["revision"]),
        )
