"""Content-free queue rows from the Memory owner; never serialize outbox payloads."""
import hashlib
from typing import Literal

from fastapi import FastAPI, Request

from .contracts import ProtocolModel


class MemoryQueueRow(ProtocolModel):
    id: str
    status: str
    attempts: int | None
    backend: str | None
    target: str | None
    action: str | None
    created_at: str | None
    next_attempt_at: str | None
    error_fingerprint: str | None
    error_code: Literal["adapter_exception", "gateway_exception", "transient", "permanent", "rate_limited", "timeout", "protected"] | None
    revision: str | None = None
    can_requeue: bool = False
    outcome: Literal["applied", "skipped_duplicate", "rejected_policy", "unobserved"] = "unobserved"


class MemoryQueuePage(ProtocolModel):
    schema_version: Literal["DesktopMemoryQueuePage.v1"] = "DesktopMemoryQueuePage.v1"
    kind: Literal["index", "projection"]
    items: list[MemoryQueueRow]
    truncated: bool


def install_memory_queue_routes(app: FastAPI, *, memory, owner):
    @app.get("/v1/debug/memory-queue", response_model=MemoryQueuePage)
    def queue(request: Request, kind: Literal["index", "projection"], status: Literal["all", "pending", "failed", "dead_letter"] = "all"):
        owner(request, "debug.read")
        records = memory.list_queue_records(kind=kind, status=status, limit=101)
        rows = []
        for record in records[:100]:
            snapshot = memory.queue_record_snapshot(kind=kind, event_id=str(record["id"]))
            record = snapshot["record"]
            result = record.get("result") or {}
            outcome = result.get("status")
            if outcome not in {"applied", "skipped_duplicate", "rejected_policy"}:
                outcome = "applied" if result.get("applied") is True else "unobserved"
            attempts = record.get("attempts")
            error = record.get("error") or record.get("last_error")
            target = record.get("target_subsystem") or record.get("projector_name")
            rows.append(MemoryQueueRow(id=str(record["id"]), status=str(record["status"]), attempts=attempts if type(attempts) is int else None,
                backend=record.get("backend") if record.get("backend") in {"fts", "vector", "graph"} else None,
                target=target if target in {"persona", "affect", "relationship", "worldops"} else None,
                action=record.get("action") if record.get("action") in {"upsert", "delete", "forget", "rollback", "rebuild"} else None,
                created_at=record.get("created_at"), next_attempt_at=record.get("next_attempt_at"),
                error_fingerprint=hashlib.sha256(str(error).encode()).hexdigest()[:16] if error else None,
                error_code=record["error_type"] if record.get("error_type") in {"adapter_exception", "gateway_exception", "transient", "permanent", "rate_limited", "timeout"} else "protected" if error else None,
                revision=snapshot["revision"], can_requeue=snapshot["can_requeue"], outcome=outcome))
        return MemoryQueuePage(kind=kind, items=rows, truncated=len(records) > 100)
