"""Durable, content-free listener intents, delivered after canonical commit."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .memory_schema import json_dumps, new_id, utc_now

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


def memory_change_revision(memory: dict) -> str:
    # Index watermarks/access heat may change during delivery; they are not a
    # new canonical source. Include privacy, content and evidence identity.
    keys = ("id", "status", "type", "summary", "sensitivity", "evidence_ids",
            "retrieval_permission", "use_permission", "disclosure_permission",
            "surface_permission", "supersedes_id", "updated_at")
    return hashlib.sha256(json_dumps({key: memory.get(key) for key in keys}).encode()).hexdigest()


class MemoryChangeDelivery:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._dispatching = False
        self._requested = False
        self.last_error_code: str | None = None

    def enqueue(self, memory, *, action, reason, source_ref, request=True):
        self._requested = self._requested or request
        for name in self.runtime._memory_change_listeners:
            self.runtime._conn.execute(
                """INSERT INTO memory_change_delivery
                   (id,listener_name,memory_id,source_revision,action,reason,source_ref,status,attempts,created_at)
                   VALUES (?,?,?,?,?,?,?,'pending',0,?)""",
                (new_id("mchange"), name, memory["id"], memory_change_revision(memory), action, reason, source_ref, utc_now()))

    def after_commit(self):
        # A delivery failure must not turn a committed user change into a false
        # failure receipt. Pending work is durable and retried by the worker.
        if self._dispatching or not self._requested:
            return
        self._requested = False
        self._wake.set()
        self.process()

    def after_rollback(self):
        self._requested = False

    def process(self, *, limit=100):
        runtime = self.runtime
        with runtime._lock:
            if runtime._conn.in_transaction or self._dispatching:
                return {"status": "deferred"}
            self._dispatching = True
            try:
                runtime.index_outbox.process_pending(limit=limit)
                runtime.projector_bus.process_pending(limit=limit)
                names = tuple(runtime._memory_change_listeners)
                placeholders = ",".join("?" for _ in names)
                rows = runtime._conn.execute(
                    """SELECT * FROM memory_change_delivery WHERE status='pending'
                       AND (available_at IS NULL OR available_at<=?)
                       AND listener_name IN (""" + placeholders + ") ORDER BY created_at,id LIMIT ?",
                    (utc_now(), *names, limit)).fetchall() if names else []
                for row in rows:
                    listener = runtime._memory_change_listeners.get(row["listener_name"])
                    if listener is None:
                        continue  # Never discard work before its owner registers.
                    # A successful callback means delivered, not proof that the
                    # domain changed state (Affect may legitimately ignore it).
                    outcome = "delivered"
                    error = None
                    try:
                        memory = runtime.get_memory(row["memory_id"])
                        if memory_change_revision(memory) != row["source_revision"]:
                            outcome = "rejected_policy"
                        else:
                            listener(memory, action=row["action"], reason=row["reason"], source_ref=row["source_ref"])
                    except Exception as exc:
                        outcome = "pending"
                        error = type(exc).__name__  # No exception text or private payload.
                    available = (datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** min(row["attempts"], 8)))).isoformat() if error else None
                    with runtime._conn:
                        runtime._conn.execute(
                            """UPDATE memory_change_delivery SET status=?,attempts=attempts+1,
                               error_code=?,available_at=?,processed_at=? WHERE id=?""",
                            (outcome, error, available, None if error else utc_now(), row["id"]))
                self.last_error_code = None
                return {"status": "processed", "listener_count": len(rows)}
            except Exception as exc:
                runtime._conn.rollback()
                self.last_error_code = type(exc).__name__
                return {"status": "retry_pending", "error_code": self.last_error_code}
            finally:
                self._dispatching = False

    def snapshot(self):
        with self.runtime._lock:
            rows = self.runtime._conn.execute("SELECT status,count(*) FROM memory_change_delivery GROUP BY status").fetchall()
            failed = self.runtime._conn.execute("SELECT count(*) FROM memory_change_delivery WHERE status='pending' AND attempts>0").fetchone()[0]
        counts = {row[0]: row[1] for row in rows}
        return {"running": bool(self._thread and self._thread.is_alive()),
                "listeners": counts, "pending": counts.get("pending", 0), "failed": failed,
                "error_code": self.last_error_code}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sora-memory-delivery", daemon=True)
        self._thread.start()

    def stop(self, *, timeout=30.0):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Memory delivery worker did not stop")
            self._thread = None

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.process()
            self._wake.wait(1.0)
