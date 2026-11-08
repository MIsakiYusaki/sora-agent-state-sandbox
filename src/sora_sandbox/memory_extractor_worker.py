from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import TYPE_CHECKING

from .memory_extractor_provider import MemoryExtractorProvider, OpenAICompatibleExtractorProvider
from .memory_extractor_schema import (
    ExtractorValidationError,
    extractor_result_to_dict,
    validate_extractor_result,
)
from .memory_schema import JsonDict
from .memory_job_observability import memory_job_observer
from .turn_observability import observe_call
from .memory_write_decision import calibrate_write_decisions
from .memory_extraction_delivery import (
    ExtractionDelivery, ExtractionLeaseLost, ExtractionSourceUnavailable,
)

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


@dataclass
class AsyncMemoryExtractorWorker:
    runtime: "MemoryRuntime"
    provider: MemoryExtractorProvider | None = None
    worker_id: str = "async_memory_extractor"
    lease_seconds: int = 120

    def __post_init__(self) -> None:
        self.provider = (
            self.provider
            or self.runtime.extractor_provider
            or OpenAICompatibleExtractorProvider.from_env()
        )

    def run_once(self, *, auto_commit: bool = True) -> JsonDict:
        job = self.runtime.claim_next_extraction_job(lease_seconds=self.lease_seconds)
        if not job:
            return {"status": "idle", "job": None}
        return memory_job_observer(self.runtime).run(job, self._run_claimed, auto_commit=auto_commit)

    def _run_claimed(self, job: JsonDict, *, auto_commit: bool) -> JsonDict:
        delivery = ExtractionDelivery(self.runtime, job, self.lease_seconds)
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(max(0.1, self.lease_seconds / 3)):
                try:
                    if not delivery.renew():
                        return
                except Exception:
                    # Any lost lease is fenced again before persistence.
                    return

        thread = threading.Thread(target=heartbeat, name="memory-extractor-lease", daemon=True)
        thread.start()
        try:
            checkpoint = observe_call("memory.extractor.checkpoint_read", delivery.checkpoint)
            input_package = observe_call("memory.extractor.input", self.runtime.build_extraction_input, job["job_id"])
            if checkpoint:
                input_package.update(checkpoint["input"])
                calibrated = observe_call("memory.extractor.validate_checkpoint", validate_extractor_result, checkpoint["result"], input_package)
            else:
                raw_result = observe_call("memory.extractor.provider", self.provider.extract, input_package)
                validated = observe_call("memory.extractor.validate", validate_extractor_result, raw_result, input_package)
                calibrated = validated.__class__(
                    schema_version=validated.schema_version,
                    job_id=validated.job_id,
                    episode_id=validated.episode_id,
                    model=validated.model,
                    candidates=observe_call("memory.extractor.calibrate", calibrate_write_decisions,
                        validated.candidates, input_package.get("related_memories", []),
                    ),
                    contradictions=validated.contradictions,
                    trace=validated.trace,
                )
                observe_call("memory.extractor.checkpoint_save", delivery.save_checkpoint, calibrated, input_package)
            materialized = observe_call("memory.extractor.materialize_and_gate", delivery.materialize,
                calibrated, input_package, auto_commit=auto_commit,
            )
            write_count = len(materialized.get("candidates", []))
            noop_count = len(materialized.get("noop_candidates", []))
            completed = observe_call("memory.extractor.complete", delivery.complete, f"{write_count} candidates materialized, {noop_count} noop")
            return {
                "status": "completed",
                "job": completed,
                "input": {
                    "episode_id": input_package["episode"]["episode_id"],
                    "related_memory_ids": [item["id"] for item in input_package["related_memories"]],
                },
                "result": extractor_result_to_dict(calibrated),
                "materialized": materialized,
            }
        except ExtractionLeaseLost:
            return {"status": "superseded", "job_id": job["job_id"]}
        except Exception as exc:
            retry = not isinstance(exc, (ExtractorValidationError, ExtractionSourceUnavailable))
            error = f"extractor_error:{type(exc).__name__}"
            try:
                failed = delivery.fail(error, retry=retry)
            except ExtractionLeaseLost:
                return {"status": "superseded", "job_id": job["job_id"]}
            return {"status": "retry" if failed["status"] == "pending" else "failed",
                    "job": failed, "error": error}
        finally:
            heartbeat_stop.set()
            thread.join()
