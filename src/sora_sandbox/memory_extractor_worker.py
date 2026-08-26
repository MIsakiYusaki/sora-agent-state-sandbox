from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .memory_extractor_provider import MemoryExtractorProvider, OpenAICompatibleExtractorProvider
from .memory_extractor_schema import (
    ExtractorValidationError,
    extractor_result_to_dict,
    validate_extractor_result,
)
from .memory_schema import JsonDict
from .memory_write_decision import calibrate_write_decisions

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


@dataclass
class AsyncMemoryExtractorWorker:
    runtime: "MemoryRuntime"
    provider: MemoryExtractorProvider | None = None
    worker_id: str = "async_memory_extractor"

    def __post_init__(self) -> None:
        self.provider = (
            self.provider
            or self.runtime.extractor_provider
            or OpenAICompatibleExtractorProvider.from_env()
        )

    def run_once(self, *, auto_commit: bool = True) -> JsonDict:
        job = self.runtime.claim_next_extraction_job()
        if not job:
            return {"status": "idle", "job": None}
        try:
            input_package = self.runtime.build_extraction_input(job["job_id"])
            raw_result = self.provider.extract(input_package)
            validated = validate_extractor_result(raw_result, input_package)
            calibrated = validated.__class__(
                schema_version=validated.schema_version,
                job_id=validated.job_id,
                episode_id=validated.episode_id,
                model=validated.model,
                candidates=calibrate_write_decisions(
                    validated.candidates,
                    input_package.get("related_memories", []),
                ),
                contradictions=validated.contradictions,
                trace=validated.trace,
            )
            materialized = self.runtime.materialize_extractor_result(
                calibrated,
                input_package=input_package,
                auto_commit=auto_commit,
            )
            write_count = len(materialized.get("candidates", []))
            noop_count = len(materialized.get("noop_candidates", []))
            completed = self.runtime.complete_extraction_job(
                job["job_id"],
                result_summary=f"{write_count} candidates materialized, {noop_count} noop",
            )
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
        except ExtractorValidationError as exc:
            failed = self.runtime.fail_extraction_job(
                job["job_id"],
                error=f"extractor_validation_error:{exc}",
                retry=False,
            )
            return {"status": "failed", "job": failed, "error": str(exc)}
        except Exception as exc:
            retry = job.get("attempts", 0) < 3
            failed = self.runtime.fail_extraction_job(
                job["job_id"],
                error=f"extractor_error:{exc}",
                retry=retry,
            )
            return {"status": "retry" if retry else "failed", "job": failed, "error": str(exc)}
