from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.relationship.contracts import RelationshipEvidenceCandidate
from sora_sandbox.relationship.dynamics import RelationshipDynamicsEngine
from sora_sandbox.relationship.store import RelationshipStore
from sora_sandbox.relationship.system import RelationshipSystem


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic Relationship 10k ledger and frame-read benchmark.")
    parser.add_argument("--contributions", type=int, default=10000)
    parser.add_argument("--reads", type=int, default=1000)
    args = parser.parse_args()
    engine = RelationshipDynamicsEngine()
    with tempfile.TemporaryDirectory(prefix="sora-relationship-benchmark-") as directory:
        system = RelationshipSystem(RelationshipStore(Path(directory) / "relationship.db"), start_worker=False)
        try:
            pair = system.pair(user_id="benchmark-user")
            synthetic = []
            started = time.perf_counter()
            dimensions = tuple(sorted(engine.profile["initial_state"]))
            for index in range(args.contributions):
                candidate = RelationshipEvidenceCandidate.from_dict({
                    "candidate_id": f"benchmark-{index}",
                    "pair_id": pair["pair_id"],
                    "direction_scope": "sora_to_user",
                    "dimension": dimensions[index % len(dimensions)],
                    "direction": 1 if index % 7 else -1,
                    "evidence_type": "cross_episode_interaction_pattern",
                    "source_kind": "benchmark",
                    "source_ref": f"benchmark:{index}",
                    "evidence_refs": [f"benchmark:{index}"],
                    "confidence": 0.85,
                    "evidence_strength": 0.80,
                    "source_reliability": 0.90,
                    "direction_certainty": 0.90,
                    "consistency": 0.85,
                })
                synthetic.append({
                    **candidate.to_dict(),
                    "contribution_id": f"benchmark-contribution-{index}",
                    "bounded_delta": 0.001 if candidate.direction > 0 else -0.001,
                    "support": 0.8,
                    "occurred_at": f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                })
            aggregation_started = time.perf_counter()
            snapshot = engine.aggregate_state(
                synthetic,
                agreement=system.store.active_agreement(pair["pair_id"]),
                repair_state="stable",
                previous_snapshot=system.store.active_state_snapshot(pair["pair_id"]),
                relationship_started_at="2025-01-01T00:00:00+00:00",
                generated_at="2026-07-16T00:00:00+00:00",
            )
            aggregation_ms = (time.perf_counter() - aggregation_started) * 1000
            latencies = []
            for _ in range(args.reads):
                read_started = time.perf_counter()
                system.frame(user_id="benchmark-user")
                latencies.append((time.perf_counter() - read_started) * 1000)
            ordered = sorted(latencies)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            payload = {
                "schema_version": "RelationshipBenchmark.v1",
                "contributions": args.contributions,
                "reads": args.reads,
                "fixture_build_ms": round((aggregation_started - started) * 1000, 3),
                "aggregation_ms": round(aggregation_ms, 3),
                "frame_read_mean_ms": round(statistics.mean(latencies), 3),
                "frame_read_p95_ms": round(p95, 3),
                "frame_read_max_ms": round(max(latencies), 3),
                "snapshot_checksum": snapshot["checksum"],
                "slo_met": p95 <= 20.0,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["slo_met"] else 1
        finally:
            system.close()


if __name__ == "__main__":
    raise SystemExit(main())
