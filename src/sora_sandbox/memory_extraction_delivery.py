from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from .memory_episode_ingestion import _plus_seconds
from .memory_extractor_schema import ExtractorResult, extractor_result_to_dict
from .memory_schema import JsonDict, json_dumps, utc_now

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


class ExtractionLeaseLost(RuntimeError):
    pass


class ExtractionSourceUnavailable(RuntimeError):
    pass


@dataclass
class ExtractionDelivery:
    """Fenced database steps around an LLM call, never around the call itself."""

    runtime: MemoryRuntime
    job: JsonDict
    lease_seconds: int = 120

    @contextmanager
    def transaction(self, *, check_source: bool = True) -> Iterator[None]:
        with self.runtime._lock:
            conn = self.runtime._conn
            with conn:
                current = self.runtime.ingestion.get_extraction_job(self.job["job_id"])
                if (current["status"] != "running"
                        or current.get("lease_token") != self.job["lease_token"]
                        or not current.get("lease_expires_at")
                        or current["lease_expires_at"] <= utc_now()):
                    raise ExtractionLeaseLost("extraction claim no longer belongs to this worker")
                if check_source:
                    episode = self.runtime.ingestion.get_episode(current["episode_id"])
                    events = [self.runtime.ingestion.get_event(ref) for ref in current["event_ids"]]
                    if (episode.get("deleted_at") or episode["status"] in {"deleted", "archived"}
                            or not events or any(e.get("deleted_at") for e in events)):
                        raise ExtractionSourceUnavailable("extraction source removed or archived")
                yield

    def renew(self) -> bool:
        try:
            with self.transaction(check_source=False):
                self.runtime._conn.execute(
                    "UPDATE extraction_job SET lease_expires_at = ? WHERE id = ?",
                    (_plus_seconds(utc_now(), self.lease_seconds), self.job["job_id"]),
                )
            return True
        except ExtractionLeaseLost:
            return False

    def checkpoint(self) -> JsonDict | None:
        with self.transaction():
            row = self.runtime._conn.execute(
                "SELECT * FROM memory_extraction_checkpoint WHERE job_id = ?", (self.job["job_id"],),
            ).fetchone()
            return {"input": json.loads(row["input_json"]), "result": json.loads(row["result_json"])} if row else None

    def save_checkpoint(self, result: ExtractorResult, package: JsonDict) -> None:
        # Do not duplicate the raw episode. Persist the related-memory snapshot so
        # a retry validates the same target set, not a fresh stochastic decision.
        context = {"related_memories": package["related_memories"],
                   "source_identity": package.get("source_identity", {})}
        with self.transaction():
            self.runtime._conn.execute(
                """INSERT INTO memory_extraction_checkpoint(job_id, input_json, result_json, created_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO NOTHING""",
                (self.job["job_id"], json_dumps(context), json_dumps(extractor_result_to_dict(result)), utc_now()),
            )

    def materialize(self, result: ExtractorResult, package: JsonDict, *, auto_commit: bool) -> JsonDict:
        with self.transaction():
            materialized = self.runtime.materialize_extractor_result(
                result, input_package=package, auto_commit=False, _commit=False,
            )
        if auto_commit:
            for index, candidate in enumerate(materialized["candidates"]):
                if not candidate.get("candidate_id"):
                    continue
                with self.transaction():
                    gate = self.runtime.run_candidate_gate(candidate["candidate_id"], actor="async_extractor")
                    materialized["candidates"][index] = gate.get("candidate", candidate)
                    materialized["committed"].extend(gate.get("committed", []))
        return materialized

    def complete(self, summary: str) -> JsonDict:
        with self.transaction():
            job = self.runtime.ingestion.complete_extraction_job(self.job["job_id"], result_summary=summary)
            self._clear_checkpoint()
            return job

    def fail(self, error: str, *, retry: bool) -> JsonDict:
        with self.transaction(check_source=False):
            job = self.runtime.ingestion.fail_extraction_job(
                self.job["job_id"], error=error, retry=retry,
                retry_delay_seconds=min(900, 60 * 2 ** max(0, self.job["attempts"] - 1)),
            )
            if job["status"] != "pending":
                self._clear_checkpoint()
            return job

    def _clear_checkpoint(self) -> None:
        self.runtime._conn.execute("DELETE FROM memory_extraction_checkpoint WHERE job_id = ?",
                                   (self.job["job_id"],))
