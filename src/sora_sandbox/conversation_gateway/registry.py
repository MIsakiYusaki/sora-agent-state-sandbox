from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ConversationBinding:
    profile_id: str
    subject_id: str
    agent_id: str
    external_conversation_id: str
    runtime_conversation_id: str
    created_at_ms: int


@dataclass(frozen=True)
class TurnBinding:
    turn_id: str
    profile_id: str
    subject_id: str
    agent_id: str
    client_instance_id: str
    external_conversation_id: str
    runtime_conversation_id: str
    request_id: str
    client_message_id: str | None
    input_hash: str
    correlation_id: str
    protocol_version: str
    created_at_ms: int
    updated_at_ms: int


class RegistryConflict(RuntimeError):
    pass


class GatewayRegistry:
    """Durable ownership and idempotency index without message content."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def ensure_conversation(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        external_conversation_id: str,
    ) -> ConversationBinding:
        runtime_id = self.runtime_conversation_id(
            profile_id=profile_id,
            subject_id=subject_id,
            agent_id=agent_id,
            external_conversation_id=external_conversation_id,
        )
        created = now_ms()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO gateway_conversation (
                    profile_id, subject_id, agent_id, external_conversation_id,
                    runtime_conversation_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    subject_id,
                    agent_id,
                    external_conversation_id,
                    runtime_id,
                    created,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM gateway_conversation
                WHERE profile_id=? AND subject_id=? AND agent_id=?
                  AND external_conversation_id=?
                """,
                (profile_id, subject_id, agent_id, external_conversation_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation binding could not be created")
        return self._conversation_from_row(row)

    def conversation_for_identity(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        external_conversation_id: str,
    ) -> ConversationBinding | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM gateway_conversation
                WHERE profile_id=? AND subject_id=? AND agent_id=?
                  AND external_conversation_id=?
                """,
                (profile_id, subject_id, agent_id, external_conversation_id),
            ).fetchone()
        return self._conversation_from_row(row) if row is not None else None

    def list_conversations_for_identity(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConversationBinding]:
        """Return only conversations owned by the verified local identity."""
        bounded = max(1, min(int(limit), 201))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM gateway_conversation
                WHERE profile_id=? AND subject_id=? AND agent_id=?
                ORDER BY created_at_ms DESC, external_conversation_id DESC
                LIMIT ? OFFSET ?
                """,
                (profile_id, subject_id, agent_id, bounded, max(0, int(offset))),
            ).fetchall()
        return [self._conversation_from_row(row) for row in rows]

    def all_conversations_for_identity(self, *, profile_id: str, subject_id: str, agent_id: str) -> list[ConversationBinding]:
        """Ownership metadata only; message bodies never enter this registry."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gateway_conversation WHERE profile_id=? AND subject_id=? AND agent_id=?",
                (profile_id, subject_id, agent_id),
            ).fetchall()
        return [self._conversation_from_row(row) for row in rows]

    def bind_turn(
        self,
        *,
        turn_id: str,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        client_instance_id: str,
        external_conversation_id: str,
        runtime_conversation_id: str,
        request_id: str,
        client_message_id: str | None,
        input_hash: str,
        correlation_id: str,
        protocol_version: str,
    ) -> TurnBinding:
        timestamp = now_ms()
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM gateway_turn_binding
                WHERE profile_id=? AND subject_id=? AND agent_id=?
                  AND external_conversation_id=? AND request_id=?
                """,
                (profile_id, subject_id, agent_id, external_conversation_id, request_id),
            ).fetchone()
            if existing is not None:
                if str(existing["input_hash"]) != input_hash:
                    raise RegistryConflict("request_id was reused with different input")
                if str(existing["turn_id"]) != turn_id:
                    raise RegistryConflict("request_id resolved to a different runtime turn")
                return self._turn_from_row(existing)
            connection.execute(
                """
                INSERT INTO gateway_turn_binding (
                    turn_id, profile_id, subject_id, agent_id, client_instance_id,
                    external_conversation_id, runtime_conversation_id, request_id,
                    client_message_id, input_hash, correlation_id, protocol_version,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    profile_id,
                    subject_id,
                    agent_id,
                    client_instance_id,
                    external_conversation_id,
                    runtime_conversation_id,
                    request_id,
                    client_message_id,
                    input_hash,
                    correlation_id,
                    protocol_version,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM gateway_turn_binding WHERE turn_id=?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("turn binding could not be created")
        return self._turn_from_row(row)

    def turn_for_request(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        external_conversation_id: str,
        request_id: str,
    ) -> TurnBinding | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM gateway_turn_binding
                WHERE profile_id=? AND subject_id=? AND agent_id=?
                  AND external_conversation_id=? AND request_id=?
                """,
                (profile_id, subject_id, agent_id, external_conversation_id, request_id),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def turn_for_identity(
        self,
        *,
        turn_id: str,
        profile_id: str,
        subject_id: str,
        agent_id: str,
    ) -> TurnBinding | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM gateway_turn_binding
                WHERE turn_id=? AND profile_id=? AND subject_id=? AND agent_id=?
                """,
                (turn_id, profile_id, subject_id, agent_id),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def acknowledge(self, capability_id: str, turn_id: str, sequence: int) -> None:
        timestamp = now_ms()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO gateway_event_ack (
                    capability_id, turn_id, last_sequence, updated_at_ms
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(capability_id, turn_id) DO UPDATE SET
                    last_sequence=MAX(gateway_event_ack.last_sequence, excluded.last_sequence),
                    updated_at_ms=excluded.updated_at_ms
                """,
                (capability_id, turn_id, max(1, int(sequence)), timestamp),
            )

    def health(self) -> dict[str, object]:
        with self._lock:
            integrity = str(self._conn.execute("PRAGMA quick_check").fetchone()[0])
            mode = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            conversations = int(
                self._conn.execute("SELECT COUNT(*) FROM gateway_conversation").fetchone()[0]
            )
            turns = int(
                self._conn.execute("SELECT COUNT(*) FROM gateway_turn_binding").fetchone()[0]
            )
        return {
            "status": "ok" if integrity == "ok" else "degraded",
            "integrity": integrity,
            "journal_mode": mode,
            "schema_version": 2,
            "conversations": conversations,
            "turns": turns,
        }

    def maintain(self, *, ack_before_ms: int) -> dict[str, int]:
        with self.transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM gateway_event_ack WHERE updated_at_ms<?",
                (ack_before_ms,),
            ).rowcount
        return {"event_acks_deleted": int(deleted)}

    @staticmethod
    def runtime_conversation_id(
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        external_conversation_id: str,
    ) -> str:
        material = "\x1f".join(
            (profile_id, subject_id, agent_id, external_conversation_id)
        )
        return "conv_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_conversation (
                    profile_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    external_conversation_id TEXT NOT NULL,
                    runtime_conversation_id TEXT NOT NULL UNIQUE,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (
                        profile_id, subject_id, agent_id, external_conversation_id
                    )
                );
                CREATE TABLE IF NOT EXISTS gateway_turn_binding (
                    turn_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    client_instance_id TEXT NOT NULL,
                    external_conversation_id TEXT NOT NULL,
                    runtime_conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    client_message_id TEXT,
                    input_hash TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE (
                        profile_id, subject_id, agent_id,
                        external_conversation_id, request_id
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_turn_conversation
                    ON gateway_turn_binding (
                        profile_id, subject_id, agent_id, external_conversation_id,
                        created_at_ms
                    );
                CREATE TABLE IF NOT EXISTS gateway_event_ack (
                    capability_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (capability_id, turn_id)
                );
                """
            )
            columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(gateway_turn_binding)"
                ).fetchall()
            }
            if "client_message_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE gateway_turn_binding ADD COLUMN client_message_id TEXT"
                )

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> ConversationBinding:
        return ConversationBinding(
            profile_id=str(row["profile_id"]),
            subject_id=str(row["subject_id"]),
            agent_id=str(row["agent_id"]),
            external_conversation_id=str(row["external_conversation_id"]),
            runtime_conversation_id=str(row["runtime_conversation_id"]),
            created_at_ms=int(row["created_at_ms"]),
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> TurnBinding:
        return TurnBinding(
            turn_id=str(row["turn_id"]),
            profile_id=str(row["profile_id"]),
            subject_id=str(row["subject_id"]),
            agent_id=str(row["agent_id"]),
            client_instance_id=str(row["client_instance_id"]),
            external_conversation_id=str(row["external_conversation_id"]),
            runtime_conversation_id=str(row["runtime_conversation_id"]),
            request_id=str(row["request_id"]),
            client_message_id=(
                str(row["client_message_id"])
                if row["client_message_id"] is not None
                else None
            ),
            input_hash=str(row["input_hash"]),
            correlation_id=str(row["correlation_id"]),
            protocol_version=str(row["protocol_version"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )
