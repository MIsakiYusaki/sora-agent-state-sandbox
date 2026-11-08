"""Product compatibility migrations, separate from domain-agnostic Memory rules."""
from .paths import SEEDS_DIR
from .state import load_jsonl


def retire_untouched_example_memories(memory) -> list[str]:
    """Reversibly retire only proven, unedited examples imported by old builds.

    User edits, restored records, real evidence and coincidentally matching text
    are preserved. The normal canonical mutation publishes index/projection
    tombstones and records a revision, so subsequent launches are idempotent.
    """
    retired = []
    examples = load_jsonl(SEEDS_DIR / "memory.seed.jsonl")
    with memory._lock:
        for example in examples:
            try:
                record = memory.get_memory(example["id"])
            except ValueError:
                continue
            if any((
                record["owner_user_id"] != memory.owner_user_id,
                record["agent_id"] != memory.agent_id,
                record["status"] != "active",
                record["summary"] != example["content"],
                record["type"] != example["type"],
                record["tags"] != example["tags"],
                record["sensitivity"] != example["sensitivity"],
            )):
                continue
            revisions = memory.revisions_for_memory(record["id"])
            evidence = memory.evidence_for_memory(record["id"])
            if (len(revisions) != 1
                    or revisions[0]["action"] != "created"
                    or revisions[0]["actor"] != "system_seed"
                    or revisions[0]["reason"] != "seed_import"
                    or any(value != revisions[0]["after"].get(key)
                           for key, value in record.items()
                           if key not in {"index_watermark", "access_count", "last_accessed_at", "lifecycle"})
                    or len(evidence) != 1
                    or evidence[0]["source_type"] != "seed"
                    or evidence[0]["metadata"].get("seed_id") != record["id"]
                    or evidence[0]["deleted_at"] is not None):
                continue
            memory._set_status(
                record["id"], "disabled",
                reason="bundled_example_retired", actor="product_migration",
            )
            retired.append(record["id"])
    return retired
