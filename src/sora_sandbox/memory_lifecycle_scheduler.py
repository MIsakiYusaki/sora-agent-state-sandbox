"""Managed, model-free lifecycle maintenance; canonical owners do the work."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .memory_schema import JsonDict, json_dumps, json_loads, utc_now

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


class MemoryLifecycleScheduler:
    KEY = "memory.lifecycle.scheduler.v1"

    def __init__(self, runtime: MemoryRuntime, *, interval_seconds: float = 3600,
                 startup_delay_seconds: float = 30, poll_seconds: float = 5, batch_limit: int = 100):
        if interval_seconds <= 0 or startup_delay_seconds < 0 or poll_seconds <= 0 or not 1 <= batch_limit <= 200:
            raise ValueError("invalid lifecycle scheduler configuration")
        self.runtime = runtime
        self.interval_seconds, self.startup_delay_seconds = interval_seconds, startup_delay_seconds
        self.poll_seconds, self.batch_limit = poll_seconds, batch_limit
        self._stop, self._wake = threading.Event(), threading.Event()
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._status = "stopped"

    def _load(self) -> JsonDict:
        row = self.runtime._conn.execute("SELECT value_json FROM memory_quality_setting WHERE key=?", (self.KEY,)).fetchone()
        return json_loads(row["value_json"], {}) if row else {"paused": False, "config_revision": 0}

    def _save(self, value: JsonDict) -> None:
        self.runtime._conn.execute(
            """INSERT INTO memory_quality_setting(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (self.KEY, json_dumps(value), utc_now()),
        )

    @staticmethod
    def _revision(value: JsonDict) -> str:
        return hashlib.sha256(json_dumps({"paused": value["paused"], "revision": value["config_revision"]}).encode()).hexdigest()

    def snapshot(self) -> JsonDict:
        with self.runtime._lock:
            value = self._load()
        with self._state_lock:
            running, status = bool(self._thread and self._thread.is_alive()), self._status
        return {"schema_version": "MemoryMaintenance.v1", "paused": value["paused"],
                "revision": self._revision(value), "running": running, "status": status,
                "next_due_at": value.get("next_due_at"), "last_completed_at": value.get("last_completed_at"),
                "last_run_id": value.get("last_run_id"), "last_result": value.get("last_result"),
                "last_error": value.get("last_error"), "interval_seconds": self.interval_seconds,
                "batch_limit": self.batch_limit}

    def set_paused(self, paused: bool, *, expected_revision: str, actor: str) -> JsonDict:
        if type(paused) is not bool:
            raise ValueError("paused must be a boolean")
        # This also waits for any in-flight maintenance transaction. Once the
        # pause receipt returns, no later periodic mutation can slip through.
        with self.runtime._lock, self.runtime._conn:
            value = self._load()
            if expected_revision != self._revision(value):
                raise ValueError("maintenance controls changed; reload")
            value.update(paused=paused, config_revision=value["config_revision"] + 1)
            self._save(value)
            self.runtime.quality_ops.record_quality_event(event_type="memory_maintenance_control", severity="info", status="resolved",
                source_type="memory_lifecycle_scheduler", source_id=self.KEY,
                message="Periodic memory maintenance paused" if paused else "Periodic memory maintenance resumed",
                payload={"paused": paused, "actor": actor, "config_revision": value["config_revision"]}, _commit=False)
        self._wake.set()
        return self.snapshot()

    def run_once(self, *, now: str | None = None) -> JsonDict:
        started = time.perf_counter()
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        now_text = now_dt.isoformat()
        with self.runtime._lock, self.runtime._conn:
            value = self._load()
            if value["paused"]:
                return {"status": "paused"}
            if value.get("next_due_at") and datetime.fromisoformat(value["next_due_at"]) > now_dt:
                return {"status": "waiting"}
            # These existing owners honor explicit deletion, evidence expiry,
            # tombstones and disclosure. There is no LLM or auto-consolidation.
            retention = self.runtime.process_retention(now=now_text, limit=self.batch_limit)
            heat = self.runtime.run_lifecycle(mode="heat_only", now=now_text, limit=self.batch_limit,
                actor="memory_lifecycle_scheduler", least_recently_scored=True)
            tombstones = self.runtime.lifecycle.process_tombstones(now=now_text)
            value.update(last_completed_at=now_text, last_run_id=heat["run_id"], last_error=None,
                         next_due_at=(now_dt + timedelta(seconds=self.interval_seconds)).isoformat(),
                         last_result={"scored_count": heat["summary"]["scored_count"],
                                      "migrated_count": heat["summary"]["migrated_count"],
                                      "expired_evidence_count": retention["expired_evidence_count"],
                                      "expired_memory_count": retention["expired_memory_count"],
                                      "tombstone_deleted_count": tombstones.get("deleted_count", 0),
                                      "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
            self._save(value)
        return {"status": "completed", "run_id": heat["run_id"], **value["last_result"]}

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="memory-lifecycle", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 30) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("memory lifecycle still draining; stores must remain open")

    def _run(self) -> None:
        try:
            with self._state_lock:
                self._status = "starting"
            # Startup delay avoids adding maintenance to bootstrap latency. A
            # wake for controls does not bypass this fixed warmup.
            if self._stop.wait(self.startup_delay_seconds):
                return
            while not self._stop.is_set():
                self._wake.clear()
                wait_seconds = self.poll_seconds
                try:
                    result = self.run_once()
                    with self._state_lock:
                        self._status = result["status"]
                except Exception as exc:
                    wait_seconds = max(self.poll_seconds, 60)
                    with self.runtime._lock, self.runtime._conn:
                        value = self._load()
                        value["last_error"] = type(exc).__name__
                        self._save(value)
                    with self._state_lock:
                        self._status = "error"
                # Errors use this bounded retry wait; success uses persisted due
                # time, so restart cannot run the same completed period twice.
                self._wake.wait(wait_seconds)
        finally:
            with self._state_lock:
                self._status = "stopped"
