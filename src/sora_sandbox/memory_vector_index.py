from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .memory_embedding import (
    EmbeddingProvider,
    cosine_similarity,
    text_hash,
    vector_hash,
)
from .memory_schema import JsonDict, RetrievalHit, json_loads, utc_now


class VectorIndex(Protocol):
    provider: EmbeddingProvider
    backend_name: str
    collection: str
    min_score: float

    def upsert(self, document: JsonDict) -> JsonDict:
        ...

    def delete(self, memory_id: str, *, index_generation: str | None = None) -> JsonDict:
        ...

    def search(
        self,
        query_text: str,
        *,
        owner_user_id: str,
        agent_id: str,
        limit: int,
        index_generation: str = "active",
    ) -> list[RetrievalHit]:
        ...

    def health(self) -> JsonDict:
        ...


@dataclass
class SQLiteVectorIndex:
    conn: sqlite3.Connection
    provider: EmbeddingProvider
    backend_name: str = "vector_local"
    collection: str = "sora_memory"
    min_score: float = 0.14

    def upsert(self, document: JsonDict) -> JsonDict:
        text = str(document.get("text_for_embedding") or "")
        embedding = self.provider.embed_documents([text])[0]
        stored = _store_embedding_and_index_row(
            self.conn,
            provider=self.provider,
            backend_name=self.backend_name,
            collection=self.collection,
            document=document,
            embedding=embedding,
            external_id=_external_id(self.collection, str(document.get("index_generation") or "active"), str(document["memory_id"])),
        )
        stored.update(
            {
                "backend": self.backend_name,
                "action": "upsert",
                "applied": True,
            }
        )
        return stored

    def delete(self, memory_id: str, *, index_generation: str | None = None) -> JsonDict:
        _mark_vector_deleted(
            self.conn,
            memory_id,
            backend_name=self.backend_name,
            index_generation=index_generation,
            delete_embedding=True,
        )
        return {
            "backend": self.backend_name,
            "action": "delete",
            "applied": True,
            "index_generation": index_generation or "*",
        }

    def search(
        self,
        query_text: str,
        *,
        owner_user_id: str,
        agent_id: str,
        limit: int,
        index_generation: str = "active",
    ) -> list[RetrievalHit]:
        query_vector = self.provider.embed_query(query_text)
        rows = self.conn.execute(
            """
            SELECT m.*, e.vector_json, e.vector_hash
            FROM memory_vector_index vi
            JOIN memory_embedding e
              ON e.memory_id = vi.memory_id
             AND e.index_generation = vi.index_generation
            JOIN memory_record m ON m.id = vi.memory_id
            WHERE vi.backend = ?
              AND vi.status = 'active'
              AND vi.index_generation = ?
              AND m.owner_user_id = ?
              AND m.agent_id = ?
              AND m.status = 'active'
              AND m.retrieval_permission = 'allow'
            """,
            (self.backend_name, index_generation, owner_user_id, agent_id),
        ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vector = _loads_vector(row["vector_json"])
            similarity = cosine_similarity(query_vector, vector)
            if similarity < self.min_score:
                continue
            score = _memory_weighted_vector_score(similarity, row)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            RetrievalHit(
                memory_id=row["id"],
                backend=self.backend_name,
                rank=rank,
                score=score,
                row=row,
            )
            for rank, (score, row) in enumerate(scored[:limit], start=1)
        ]

    def health(self) -> JsonDict:
        active = int(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM memory_vector_index
                WHERE backend = ? AND status = 'active'
                """,
                (self.backend_name,),
            ).fetchone()[0]
        )
        embeddings = int(self.conn.execute("SELECT COUNT(*) FROM memory_embedding").fetchone()[0])
        return {
            "backend": self.backend_name,
            "status": "ok",
            "collection": self.collection,
            "embedding_model": self.provider.model,
            "dimension": self.provider.dimension,
            "active_vectors": active,
            "embedding_rows": embeddings,
            "external": False,
        }


@dataclass
class QdrantHttpClient:
    base_url: str
    api_key: str = ""
    timeout_seconds: float = 20.0

    def request(self, method: str, path: str, payload: JsonDict | None = None) -> JsonDict:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network/provider path.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"qdrant HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network/provider path.
            raise RuntimeError(f"qdrant unavailable: {exc.reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive provider path.
            raise RuntimeError(f"qdrant returned non-json response: {body[:200]}") from exc


@dataclass
class QdrantVectorIndex:
    conn: sqlite3.Connection
    provider: EmbeddingProvider
    url: str
    api_key: str = ""
    backend_name: str = "vector_qdrant"
    collection: str = "sora_memory"
    min_score: float = 0.14
    timeout_seconds: float = 20.0
    client: Any | None = None
    _ensured_dimensions: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = QdrantHttpClient(
                self.url,
                api_key=self.api_key,
                timeout_seconds=self.timeout_seconds,
            )

    def upsert(self, document: JsonDict) -> JsonDict:
        text = str(document.get("text_for_embedding") or "")
        embedding = self.provider.embed_documents([text])[0]
        generation = str(document.get("index_generation") or "active")
        memory_id = str(document["memory_id"])
        metadata = document.get("metadata") or {}
        external_id = _external_id(self.collection, generation, memory_id)
        self._ensure_collection(len(embedding))
        self.client.request(
            "PUT",
            f"/collections/{self.collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": external_id,
                        "vector": embedding,
                        "payload": {
                            "memory_id": memory_id,
                            "owner_user_id": str(metadata.get("owner_user_id") or ""),
                            "agent_id": str(metadata.get("agent_id") or ""),
                            "type": str(metadata.get("memory_type") or ""),
                            "tags": metadata.get("tags", []),
                            "status": "active",
                            "index_generation": generation,
                            "payload_hash": str(document.get("payload_hash") or ""),
                        },
                    }
                ]
            },
        )
        stored = _store_embedding_and_index_row(
            self.conn,
            provider=self.provider,
            backend_name=self.backend_name,
            collection=self.collection,
            document=document,
            embedding=embedding,
            external_id=external_id,
        )
        stored.update(
            {
                "backend": self.backend_name,
                "action": "upsert",
                "applied": True,
                "external_id": external_id,
            }
        )
        return stored

    def delete(self, memory_id: str, *, index_generation: str | None = None) -> JsonDict:
        rows = _vector_rows(
            self.conn,
            memory_id,
            backend_name=self.backend_name,
            index_generation=index_generation,
        )
        if rows:
            self.client.request(
                "POST",
                f"/collections/{self.collection}/points/delete?wait=true",
                {"points": [row["external_id"] for row in rows]},
            )
        _mark_vector_deleted(
            self.conn,
            memory_id,
            backend_name=self.backend_name,
            index_generation=index_generation,
            delete_embedding=True,
        )
        return {
            "backend": self.backend_name,
            "action": "delete",
            "applied": True,
            "deleted_external_ids": [row["external_id"] for row in rows],
            "index_generation": index_generation or "*",
        }

    def search(
        self,
        query_text: str,
        *,
        owner_user_id: str,
        agent_id: str,
        limit: int,
        index_generation: str = "active",
    ) -> list[RetrievalHit]:
        query_vector = self.provider.embed_query(query_text)
        response = self.client.request(
            "POST",
            f"/collections/{self.collection}/points/search",
            {
                "vector": query_vector,
                "limit": max(limit * 3, limit),
                "with_payload": True,
                "score_threshold": self.min_score,
                "filter": {
                    "must": [
                        {"key": "owner_user_id", "match": {"value": owner_user_id}},
                        {"key": "agent_id", "match": {"value": agent_id}},
                        {"key": "index_generation", "match": {"value": index_generation}},
                        {"key": "status", "match": {"value": "active"}},
                    ]
                },
            },
        )
        hits = response.get("result", [])
        scored: list[tuple[float, sqlite3.Row]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            memory_id = payload.get("memory_id") or self._memory_id_for_external_id(str(hit.get("id") or ""))
            if not memory_id:
                continue
            row = self._active_memory_row(
                str(memory_id),
                owner_user_id=owner_user_id,
                agent_id=agent_id,
            )
            if row is None:
                continue
            remote_score = float(hit.get("score") or 0.0)
            if remote_score < self.min_score:
                continue
            scored.append((_memory_weighted_vector_score(remote_score, row), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            RetrievalHit(
                memory_id=row["id"],
                backend=self.backend_name,
                rank=rank,
                score=score,
                row=row,
            )
            for rank, (score, row) in enumerate(scored[:limit], start=1)
        ]

    def health(self) -> JsonDict:
        active = int(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM memory_vector_index
                WHERE backend = ? AND status = 'active'
                """,
                (self.backend_name,),
            ).fetchone()[0]
        )
        status = "ok"
        error = None
        try:
            remote = self.client.request("GET", f"/collections/{self.collection}")
        except Exception as exc:  # pragma: no cover - defensive provider path.
            status = "degraded"
            error = str(exc)
            remote = {}
        return {
            "backend": self.backend_name,
            "status": status,
            "collection": self.collection,
            "embedding_model": self.provider.model,
            "dimension": self.provider.dimension,
            "active_vectors": active,
            "embedding_rows": int(self.conn.execute("SELECT COUNT(*) FROM memory_embedding").fetchone()[0]),
            "external": True,
            "remote": remote,
            "error": error,
        }

    def _ensure_collection(self, dimension: int) -> None:
        if dimension in self._ensured_dimensions:
            return
        try:
            self.client.request("GET", f"/collections/{self.collection}")
        except Exception:
            self.client.request(
                "PUT",
                f"/collections/{self.collection}",
                {"vectors": {"size": dimension, "distance": "Cosine"}},
            )
        self._ensured_dimensions.add(dimension)

    def _memory_id_for_external_id(self, external_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT memory_id FROM memory_vector_index
            WHERE backend = ? AND external_id = ? AND status = 'active'
            """,
            (self.backend_name, external_id),
        ).fetchone()
        return str(row["memory_id"]) if row else None

    def _active_memory_row(self, memory_id: str, *, owner_user_id: str, agent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM memory_record
            WHERE id = ?
              AND owner_user_id = ?
              AND agent_id = ?
              AND status = 'active'
              AND retrieval_permission = 'allow'
            """,
            (memory_id, owner_user_id, agent_id),
        ).fetchone()


def create_vector_index_from_env(
    conn: sqlite3.Connection,
    embedding_provider: EmbeddingProvider,
) -> VectorIndex:
    backend = os.getenv("SORA_VECTOR_BACKEND", "").strip().lower()
    collection = os.getenv("SORA_VECTOR_COLLECTION", "").strip() or "sora_memory"
    min_score = _env_float("SORA_VECTOR_MIN_SCORE", 0.14)
    if backend in {"qdrant", "vector_qdrant"}:
        url = os.getenv("SORA_VECTOR_URL", "").strip() or os.getenv("QDRANT_URL", "").strip()
        if not url:
            raise RuntimeError("SORA_VECTOR_BACKEND=qdrant requires SORA_VECTOR_URL or QDRANT_URL")
        return QdrantVectorIndex(
            conn,
            embedding_provider,
            url=url,
            api_key=os.getenv("SORA_VECTOR_API_KEY", "").strip() or os.getenv("QDRANT_API_KEY", "").strip(),
            collection=collection,
            min_score=min_score,
            timeout_seconds=_env_float("SORA_VECTOR_TIMEOUT_SECONDS", 20.0),
        )
    return SQLiteVectorIndex(
        conn,
        embedding_provider,
        collection=collection,
        min_score=min_score,
    )


def _store_embedding_and_index_row(
    conn: sqlite3.Connection,
    *,
    provider: EmbeddingProvider,
    backend_name: str,
    collection: str,
    document: JsonDict,
    embedding: list[float],
    external_id: str,
) -> JsonDict:
    now = utc_now()
    text = str(document.get("text_for_embedding") or "")
    input_hash = text_hash(text)
    v_hash = vector_hash(embedding)
    memory_id = str(document["memory_id"])
    generation = str(document.get("index_generation") or "active")
    existing = conn.execute(
        """
        SELECT vector_hash FROM memory_vector_index
        WHERE memory_id = ? AND backend = ? AND index_generation = ?
        """,
        (memory_id, backend_name, generation),
    ).fetchone()
    skipped = bool(existing and existing["vector_hash"] == v_hash)
    conn.execute(
        """
        INSERT INTO memory_embedding (
            memory_id, embedding_model, vector_dim, vector_json, vector_hash,
            input_hash, index_generation, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(memory_id, embedding_model, index_generation) DO UPDATE SET
            vector_dim = excluded.vector_dim,
            vector_json = excluded.vector_json,
            vector_hash = excluded.vector_hash,
            input_hash = excluded.input_hash,
            created_at = excluded.created_at
        """,
        (
            memory_id,
            provider.model,
            len(embedding),
            json.dumps(embedding, separators=(",", ":")),
            v_hash,
            input_hash,
            generation,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO memory_vector_index (
            memory_id, backend, collection, external_id, status, vector_hash,
            index_generation, index_watermark, updated_at
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
        ON CONFLICT(memory_id, backend, index_generation) DO UPDATE SET
            collection = excluded.collection,
            external_id = excluded.external_id,
            status = excluded.status,
            vector_hash = excluded.vector_hash,
            index_watermark = excluded.index_watermark,
            updated_at = excluded.updated_at
        """,
        (
            memory_id,
            backend_name,
            collection,
            external_id,
            v_hash,
            generation,
            now,
            now,
        ),
    )
    return {
        "skipped": skipped,
        "embedding_model": provider.model,
        "vector_dim": len(embedding),
        "vector_hash": v_hash,
        "input_hash": input_hash,
        "index_generation": generation,
    }


def _mark_vector_deleted(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    backend_name: str,
    index_generation: str | None,
    delete_embedding: bool,
) -> None:
    params: list[Any] = [utc_now(), memory_id, backend_name]
    generation_filter = ""
    if index_generation:
        generation_filter = " AND index_generation = ?"
        params.append(index_generation)
    conn.execute(
        f"""
        UPDATE memory_vector_index
        SET status = 'deleted', updated_at = ?
        WHERE memory_id = ? AND backend = ?{generation_filter}
        """,
        params,
    )
    if delete_embedding:
        conn.execute(
            f"""
            DELETE FROM memory_embedding
            WHERE memory_id = ?{ ' AND index_generation = ?' if index_generation else '' }
            """,
            [memory_id, *([index_generation] if index_generation else [])],
        )


def _vector_rows(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    backend_name: str,
    index_generation: str | None,
) -> list[sqlite3.Row]:
    params: list[Any] = [memory_id, backend_name]
    generation_filter = ""
    if index_generation:
        generation_filter = " AND index_generation = ?"
        params.append(index_generation)
    return conn.execute(
        f"""
        SELECT * FROM memory_vector_index
        WHERE memory_id = ? AND backend = ?{generation_filter}
        """,
        params,
    ).fetchall()


def _external_id(collection: str, generation: str, memory_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sora:{collection}:{generation}:{memory_id}"))


def _loads_vector(value: str) -> list[float]:
    loaded = json_loads(value, [])
    if not isinstance(loaded, list):
        return []
    return [float(item) for item in loaded]


def _memory_weighted_vector_score(similarity: float, row: sqlite3.Row) -> float:
    return (
        similarity * 0.78
        + float(row["importance"]) * 0.14
        + float(row["confidence"]) * 0.08
    )


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
