from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict, json_dumps, utc_now


INDEX_DOCUMENT_SCHEMA_VERSION = "index_doc_v1"
EMBEDDING_INPUT_VERSION = "embedding_input_v1"
POLICY_SNAPSHOT_VERSION = "policy_v1"
ACTIVE_INDEX_GENERATION = "active"


ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}")


def build_index_document(
    memory: JsonDict,
    evidence: list[JsonDict] | None = None,
    *,
    index_generation: str = ACTIVE_INDEX_GENERATION,
) -> JsonDict:
    evidence_rows = evidence or []
    tags = [str(tag) for tag in memory.get("tags", []) if str(tag).strip()]
    evidence_gists = [
        str(item.get("content_summary") or "").strip()
        for item in evidence_rows
        if str(item.get("content_summary") or "").strip()
    ]
    entities = extract_entities(
        [
            str(memory.get("summary") or ""),
            str(memory.get("type") or ""),
            *tags,
            *evidence_gists[:3],
        ]
    )
    policy_snapshot = {
        "sensitivity": memory.get("sensitivity"),
        "retrieval_permission": memory.get("retrieval_permission"),
        "use_permission": memory.get("use_permission"),
        "disclosure_permission": memory.get("disclosure_permission"),
        "retention_policy": memory.get("retention_policy"),
        "status": memory.get("status"),
    }
    text_for_fts = " ".join(
        item
        for item in [
            str(memory.get("summary") or ""),
            str(memory.get("type") or ""),
            " ".join(tags),
            " ".join(entities),
        ]
        if item
    )
    text_for_embedding = "\n".join(
        item
        for item in [
            f"type: {memory.get('type')}",
            f"summary: {memory.get('summary')}",
            f"tags: {', '.join(tags)}" if tags else "",
            f"evidence: {' | '.join(evidence_gists[:4])}" if evidence_gists else "",
        ]
        if item
    )
    metadata = {
        "memory_id": memory.get("id"),
        "owner_user_id": memory.get("owner_user_id"),
        "agent_id": memory.get("agent_id"),
        "memory_type": memory.get("type"),
        "tags": tags,
        "entities": entities,
        "evidence_ids": list(memory.get("evidence_ids", [])),
        "confidence": memory.get("confidence"),
        "importance": memory.get("importance"),
        "access_count": memory.get("access_count", 0),
        "last_accessed_at": memory.get("last_accessed_at"),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }
    payload = {
        "schema_version": INDEX_DOCUMENT_SCHEMA_VERSION,
        "embedding_input_version": EMBEDDING_INPUT_VERSION,
        "policy_snapshot_version": POLICY_SNAPSHOT_VERSION,
        "index_generation": index_generation,
        "text_for_fts": text_for_fts,
        "text_for_embedding": text_for_embedding,
        "metadata": metadata,
        "policy_snapshot": policy_snapshot,
    }
    now = utc_now()
    return {
        "memory_id": str(memory["id"]),
        "schema_version": INDEX_DOCUMENT_SCHEMA_VERSION,
        "embedding_input_version": EMBEDDING_INPUT_VERSION,
        "policy_snapshot_version": POLICY_SNAPSHOT_VERSION,
        "index_generation": index_generation,
        "payload_hash": stable_hash(payload),
        "text_for_fts": text_for_fts,
        "text_for_embedding": text_for_embedding,
        "metadata": metadata,
        "policy_snapshot": policy_snapshot,
        "created_at": now,
        "updated_at": now,
    }


def upsert_index_document_sql() -> str:
    return """
    INSERT INTO memory_index_document (
        memory_id, schema_version, embedding_input_version,
        policy_snapshot_version, index_generation, payload_hash, text_for_fts,
        text_for_embedding, metadata_json, policy_snapshot_json, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(memory_id, index_generation, schema_version) DO UPDATE SET
        embedding_input_version = excluded.embedding_input_version,
        policy_snapshot_version = excluded.policy_snapshot_version,
        payload_hash = excluded.payload_hash,
        text_for_fts = excluded.text_for_fts,
        text_for_embedding = excluded.text_for_embedding,
        metadata_json = excluded.metadata_json,
        policy_snapshot_json = excluded.policy_snapshot_json,
        updated_at = excluded.updated_at
    """


def index_document_params(document: JsonDict) -> tuple[object, ...]:
    return (
        document["memory_id"],
        document["schema_version"],
        document["embedding_input_version"],
        document["policy_snapshot_version"],
        document["index_generation"],
        document["payload_hash"],
        document["text_for_fts"],
        document["text_for_embedding"],
        json_dumps(document["metadata"]),
        json_dumps(document["policy_snapshot"]),
        document["created_at"],
        document["updated_at"],
    )


def stable_hash(value: JsonDict) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def extract_entities(parts: Iterable[str]) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for match in ENTITY_RE.finditer(part):
            entity = match.group(0).lower()
            if entity not in seen:
                seen.add(entity)
                entities.append(entity)
        for token in search_tokens(part):
            if len(token) >= 2 and token not in seen:
                seen.add(token)
                entities.append(token)
        if len(entities) >= 32:
            break
    return entities[:32]
