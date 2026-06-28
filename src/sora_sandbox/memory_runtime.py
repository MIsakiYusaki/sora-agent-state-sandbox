from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import SEEDS_DIR, workspace_state_dir
from .state import load_jsonl


JsonDict = dict[str, Any]

_EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"(?:请|帮我)?记住[：:，,\s]*(?P<content>.+)", re.IGNORECASE),
    re.compile(r"remember(?:\s+that)?[：:，,\s]*(?P<content>.+)", re.IGNORECASE),
)
_EXPLICIT_RECALL_TERMS = ("记得", "之前", "上次", "回忆", "remember")
_SENSITIVE_TERMS = (
    "健康",
    "病历",
    "诊断",
    "财务",
    "银行卡",
    "收入",
    "住址",
    "身份证",
    "私人关系",
)
_FORBIDDEN_TERMS = (
    "密码",
    "验证码",
    "api key",
    "apikey",
    "access token",
    "secret key",
    "私钥",
    "助记词",
)
_ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SEARCH_STOP_TOKENS = {
    "the",
    "this",
    "that",
    "用户",
    "喜欢",
    "我们",
    "这个",
    "那个",
    "请记",
    "记住",
    "之前",
    "上次",
    "回忆",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _search_tokens(text: str) -> list[str]:
    normalized = text.lower()
    tokens = {match.group(0) for match in _ASCII_WORD_RE.finditer(normalized)}
    cjk = "".join(_CJK_RE.findall(normalized))
    if len(cjk) == 1:
        tokens.add(cjk)
    else:
        tokens.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return sorted(token for token in tokens if token and token not in _SEARCH_STOP_TOKENS)


def _index_text(summary: str, memory_type: str, tags: Iterable[str]) -> str:
    source = " ".join([summary, memory_type, *tags])
    return " ".join(_search_tokens(source))


def _detect_sensitivity(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in _FORBIDDEN_TERMS):
        return "forbidden"
    if any(term in lowered for term in _SENSITIVE_TERMS):
        return "sensitive"
    return "low"


def _classify_memory_type(text: str) -> str:
    if any(term in text for term in ("项目", "架构", "设计", "技术", "原则", "方案")):
        return "project_principle"
    if any(term in text for term in ("目标", "计划", "准备", "接下来")):
        return "goal"
    return "preference"


@dataclass
class MemoryRuntime:
    """Local canonical memory runtime used by the PoC.

    SQLite is the source of truth. The JSON state keeps only a compatibility
    projection for older debug commands and tests.
    """

    path: Path
    owner_user_id: str = "local-user"
    agent_id: str = "sora_default"

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    @classmethod
    def default(cls) -> "MemoryRuntime":
        runtime = cls(workspace_state_dir() / "memory.db")
        runtime.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))
        return runtime

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    content_summary TEXT NOT NULL,
                    raw_pointer TEXT,
                    sensitivity TEXT NOT NULL,
                    retention_policy TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_record (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    supersedes_id TEXT,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT,
                    retention_policy TEXT NOT NULL,
                    retrieval_permission TEXT NOT NULL,
                    use_permission TEXT NOT NULL,
                    disclosure_permission TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (supersedes_id) REFERENCES memory_record(id)
                );

                CREATE TABLE IF NOT EXISTS memory_evidence (
                    memory_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    PRIMARY KEY (memory_id, evidence_id),
                    FOREIGN KEY (memory_id) REFERENCES memory_record(id),
                    FOREIGN KEY (evidence_id) REFERENCES evidence(id)
                );

                CREATE TABLE IF NOT EXISTS memory_candidate (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    supersedes_id TEXT,
                    explicit_user_request INTEGER NOT NULL,
                    local_only INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    committed_memory_id TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_revision (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
                );

                CREATE TABLE IF NOT EXISTS recall_trace (
                    id TEXT PRIMARY KEY,
                    query_text TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    surface_action TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    degraded INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recall_trace_item (
                    trace_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    score REAL NOT NULL,
                    selected INTEGER NOT NULL,
                    filter_reason TEXT,
                    PRIMARY KEY (trace_id, memory_id),
                    FOREIGN KEY (trace_id) REFERENCES recall_trace(id),
                    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    search_text
                );
                """
            )
            self._conn.commit()

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM recall_trace_item;
                DELETE FROM recall_trace;
                DELETE FROM memory_revision;
                DELETE FROM memory_candidate;
                DELETE FROM memory_evidence;
                DELETE FROM memory_fts;
                DELETE FROM memory_record;
                DELETE FROM evidence;
                """
            )
            self._conn.commit()
        self.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))

    def seed_if_empty(self, items: list[JsonDict]) -> None:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM memory_record").fetchone()[0]
        if count:
            return
        for item in items:
            evidence_id = self.add_evidence(
                source_type="seed",
                content_summary=item.get("content", ""),
                sensitivity=item.get("sensitivity", "low"),
                retention_policy="persistent",
                metadata={"seed_id": item.get("id")},
            )
            self._create_memory(
                memory_id=str(item.get("id") or _new_id("mem")),
                memory_type=item.get("type", "preference"),
                summary=item.get("content", ""),
                tags=item.get("tags", []),
                evidence_ids=[evidence_id],
                confidence=1.0,
                importance=0.65,
                sensitivity=item.get("sensitivity", "low"),
                reason="seed_import",
                actor="system_seed",
            )

    def import_legacy_memories(self, items: list[JsonDict]) -> int:
        """Import pre-runtime JSON memories once, preserving their public ids."""
        imported = 0
        for item in items:
            if item.get("_projection") == "memory_runtime":
                continue
            memory_id = str(item.get("id") or _new_id("mem"))
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM memory_record WHERE id = ?",
                    (memory_id,),
                ).fetchone()
            if exists:
                continue
            summary = str(item.get("content") or item.get("summary") or "").strip()
            sensitivity = str(item.get("sensitivity") or _detect_sensitivity(summary))
            if not summary or sensitivity == "forbidden":
                continue
            evidence_id = self.add_evidence(
                source_type="legacy_state_import",
                content_summary=summary,
                sensitivity=sensitivity,
                retention_policy="local_only" if sensitivity == "sensitive" else "persistent",
                metadata={"legacy_memory_id": memory_id},
            )
            self._create_memory(
                memory_id=memory_id,
                memory_type=str(item.get("type") or "preference"),
                summary=summary,
                tags=list(item.get("tags") or []),
                evidence_ids=[evidence_id],
                confidence=float(item.get("confidence", 1.0)),
                importance=float(item.get("importance", 0.65)),
                sensitivity=sensitivity,
                reason="legacy_state_import",
                actor="migration",
                supersedes_id=item.get("supersedes_id"),
            )
            status = str(item.get("status") or "active")
            if status != "active":
                self._set_status(
                    memory_id,
                    status,
                    reason="legacy_state_status_import",
                    actor="migration",
                )
            imported += 1
        return imported

    def add_evidence(
        self,
        *,
        source_type: str,
        content_summary: str,
        raw_pointer: str | None = None,
        sensitivity: str = "low",
        retention_policy: str = "persistent",
        metadata: JsonDict | None = None,
    ) -> str:
        if sensitivity == "forbidden":
            raise ValueError("forbidden content must not be persisted")
        evidence_id = _new_id("ev")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO evidence (
                    id, source_type, content_summary, raw_pointer, sensitivity,
                    retention_policy, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    source_type,
                    content_summary,
                    raw_pointer,
                    sensitivity,
                    retention_policy,
                    _json(metadata or {}),
                    _utc_now(),
                ),
            )
            self._conn.commit()
        return evidence_id

    def propose(
        self,
        *,
        summary: str,
        memory_type: str,
        evidence_ids: list[str],
        tags: list[str] | None = None,
        confidence: float = 0.8,
        sensitivity: str | None = None,
        reason: str,
        explicit_user_request: bool = False,
        supersedes_id: str | None = None,
    ) -> JsonDict:
        cleaned = summary.strip()
        detected = sensitivity or _detect_sensitivity(cleaned)
        if detected == "forbidden":
            return {
                "candidate_id": None,
                "status": "discarded",
                "reason": "forbidden_secret_not_persisted",
            }
        candidate_id = _new_id("mc")
        local_only = detected == "sensitive"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_candidate (
                    id, type, summary, tags_json, status, confidence, sensitivity,
                    reason, evidence_ids_json, supersedes_id, explicit_user_request,
                    local_only, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    memory_type,
                    cleaned,
                    _json(tags or []),
                    confidence,
                    detected,
                    reason,
                    _json(evidence_ids),
                    supersedes_id,
                    int(explicit_user_request),
                    int(local_only),
                    _utc_now(),
                ),
            )
            self._conn.commit()
        candidate = self.get_candidate(candidate_id)
        if self._eligible_for_auto_commit(candidate):
            return self.commit([candidate_id], actor="explicit_user_request")["committed"][0]
        return candidate

    def observe_turn(self, user_input: str, reply: str) -> JsonDict | None:
        if not user_input.strip():
            return None
        explicit_content = None
        for pattern in _EXPLICIT_MEMORY_PATTERNS:
            match = pattern.search(user_input)
            if match:
                explicit_content = match.group("content").strip("。.!！ ")
                break
        if not explicit_content:
            return None
        sensitivity = _detect_sensitivity(explicit_content)
        if sensitivity == "forbidden":
            return {
                "candidate_id": None,
                "status": "discarded",
                "reason": "forbidden_secret_not_persisted",
            }
        evidence_id = self.add_evidence(
            source_type="chat",
            content_summary=explicit_content,
            sensitivity=sensitivity,
            retention_policy="local_only" if sensitivity == "sensitive" else "persistent",
            metadata={"reply_summary": reply[:160], "explicit_user_request": True},
        )
        return self.propose(
            summary=explicit_content,
            memory_type=_classify_memory_type(explicit_content),
            evidence_ids=[evidence_id],
            confidence=1.0,
            sensitivity=sensitivity,
            reason="user_explicitly_requested_memory",
            explicit_user_request=True,
        )

    def observe_response_plan(self, response_plan: JsonDict) -> JsonDict | None:
        hook = response_plan.get("memory_hook_candidate")
        if not hook:
            return None
        summary = hook.get("episode_summary") or hook.get("content")
        if not summary:
            return None
        evidence_id = self.add_evidence(
            source_type=str(hook.get("source", "response_plan")),
            content_summary=summary,
            sensitivity="low",
            metadata={"hook_id": hook.get("candidate_id"), "event_id": hook.get("event_id")},
        )
        return self.propose(
            summary=summary,
            memory_type="worldops_episode"
            if hook.get("source") == "worldops_micro_quest"
            else "episode",
            evidence_ids=[evidence_id],
            tags=["memory_hook", str(hook.get("event_id") or "")],
            confidence=0.8,
            reason="response_plan_memory_hook",
        )

    def _eligible_for_auto_commit(self, candidate: JsonDict) -> bool:
        return bool(
            candidate.get("explicit_user_request")
            and candidate.get("sensitivity") == "low"
            and candidate.get("type") in {"preference", "goal", "project_principle"}
            and not candidate.get("supersedes_id")
        )

    def commit(self, candidate_ids: list[str], *, actor: str = "manual_repl") -> JsonDict:
        committed: list[JsonDict] = []
        with self._lock:
            for candidate_id in candidate_ids:
                candidate = self.get_candidate(candidate_id)
                if candidate["status"] == "committed":
                    committed.append(self.get_memory(candidate["committed_memory_id"]))
                    continue
                if candidate["status"] != "pending":
                    raise ValueError(f"candidate is not pending: {candidate_id}")

                duplicate = self._find_exact_active(candidate["type"], candidate["summary"])
                if duplicate and not candidate.get("supersedes_id"):
                    for evidence_id in candidate["evidence_ids"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                            (duplicate["id"], evidence_id),
                        )
                    self._append_revision(
                        duplicate["id"],
                        action="evidence_merged",
                        before=duplicate,
                        after=duplicate,
                        reason=candidate["reason"],
                        actor=actor,
                    )
                    memory = duplicate
                else:
                    if candidate.get("supersedes_id"):
                        previous = self.get_memory(candidate["supersedes_id"])
                        self._set_status(
                            previous["id"],
                            "superseded",
                            reason=f"superseded_by_candidate:{candidate_id}",
                            actor=actor,
                        )
                    memory = self._create_memory(
                        memory_id=_new_id("mem"),
                        memory_type=candidate["type"],
                        summary=candidate["summary"],
                        tags=candidate["tags"],
                        evidence_ids=candidate["evidence_ids"],
                        confidence=candidate["confidence"],
                        importance=0.65,
                        sensitivity=candidate["sensitivity"],
                        reason=candidate["reason"],
                        actor=actor,
                        supersedes_id=candidate.get("supersedes_id"),
                    )

                now = _utc_now()
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'committed', resolved_at = ?, committed_memory_id = ?
                    WHERE id = ?
                    """,
                    (now, memory["id"], candidate_id),
                )
                committed.append(memory)
            self._conn.commit()
        return {"status": "committed", "committed": committed}

    def reject(self, candidate_id: str, *, reason: str = "manual_reject") -> JsonDict:
        with self._lock:
            candidate = self.get_candidate(candidate_id)
            if candidate["status"] == "committed":
                raise ValueError(f"candidate already committed: {candidate_id}")
            self._conn.execute(
                """
                UPDATE memory_candidate
                SET status = 'rejected', resolved_at = ?, reason = ?
                WHERE id = ?
                """,
                (_utc_now(), reason, candidate_id),
            )
            self._conn.commit()
        return self.get_candidate(candidate_id)

    def revise(self, memory_id: str, new_summary: str, *, reason: str = "manual_revision") -> JsonDict:
        previous = self.get_memory(memory_id)
        sensitivity = _detect_sensitivity(new_summary)
        if sensitivity == "forbidden":
            raise ValueError("forbidden secrets cannot be persisted")
        evidence_id = self.add_evidence(
            source_type="manual_revision",
            content_summary=new_summary,
            sensitivity=sensitivity,
            metadata={"supersedes_id": memory_id},
        )
        candidate = self.propose(
            summary=new_summary,
            memory_type=previous["type"],
            evidence_ids=[evidence_id],
            tags=previous["tags"],
            confidence=1.0,
            sensitivity=sensitivity,
            reason=reason,
            explicit_user_request=True,
            supersedes_id=memory_id,
        )
        return self.commit([candidate["candidate_id"]], actor="manual_revision")["committed"][0]

    def add_manual(
        self,
        summary: str,
        *,
        memory_type: str = "preference",
        tags: list[str] | None = None,
    ) -> JsonDict:
        sensitivity = _detect_sensitivity(summary)
        if sensitivity == "forbidden":
            raise ValueError("forbidden secrets cannot be persisted")
        evidence_id = self.add_evidence(
            source_type="manual_repl",
            content_summary=summary,
            sensitivity=sensitivity,
            retention_policy="local_only" if sensitivity == "sensitive" else "persistent",
        )
        candidate = self.propose(
            summary=summary,
            memory_type=memory_type,
            evidence_ids=[evidence_id],
            tags=tags or [],
            confidence=1.0,
            sensitivity=sensitivity,
            reason="manual_repl",
        )
        return self.commit([candidate["candidate_id"]], actor="manual_repl")["committed"][0]

    def _create_memory(
        self,
        *,
        memory_id: str,
        memory_type: str,
        summary: str,
        tags: list[str],
        evidence_ids: list[str],
        confidence: float,
        importance: float,
        sensitivity: str,
        reason: str,
        actor: str,
        supersedes_id: str | None = None,
    ) -> JsonDict:
        now = _utc_now()
        retention = "local_only" if sensitivity == "sensitive" else "persistent"
        disclosure = "none" if sensitivity == "sensitive" else "gist_only"
        self._conn.execute(
            """
            INSERT INTO memory_record (
                id, owner_user_id, agent_id, type, summary, tags_json, status,
                confidence, importance, sensitivity, supersedes_id, valid_from,
                retention_policy, retrieval_permission, use_permission,
                disclosure_permission, schema_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, 'allow',
                      'allow_implicit', ?, 1, ?, ?)
            """,
            (
                memory_id,
                self.owner_user_id,
                self.agent_id,
                memory_type,
                summary,
                _json(tags),
                confidence,
                importance,
                sensitivity,
                supersedes_id,
                now,
                retention,
                disclosure,
                now,
                now,
            ),
        )
        for evidence_id in evidence_ids:
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
        self._sync_fts(memory_id, summary, memory_type, tags)
        memory = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="created",
            before=None,
            after=memory,
            reason=reason,
            actor=actor,
        )
        self._conn.commit()
        return memory

    def _append_revision(
        self,
        memory_id: str,
        *,
        action: str,
        before: JsonDict | None,
        after: JsonDict | None,
        reason: str,
        actor: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_revision (
                id, memory_id, action, before_json, after_json, reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("rev"),
                memory_id,
                action,
                _json(before) if before is not None else None,
                _json(after) if after is not None else None,
                reason,
                actor,
                _utc_now(),
            ),
        )

    def _sync_fts(self, memory_id: str, summary: str, memory_type: str, tags: list[str]) -> None:
        self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        self._conn.execute(
            "INSERT INTO memory_fts (memory_id, search_text) VALUES (?, ?)",
            (memory_id, _index_text(summary, memory_type, tags)),
        )

    def _remove_from_fts(self, memory_id: str) -> None:
        self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))

    def _find_exact_active(self, memory_type: str, summary: str) -> JsonDict | None:
        row = self._conn.execute(
            """
            SELECT * FROM memory_record
            WHERE owner_user_id = ? AND agent_id = ? AND type = ? AND summary = ?
              AND status = 'active'
            LIMIT 1
            """,
            (self.owner_user_id, self.agent_id, memory_type, summary),
        ).fetchone()
        return self._memory_from_row(row) if row else None

    def _set_status(self, memory_id: str, status: str, *, reason: str, actor: str) -> JsonDict:
        with self._lock:
            before = self.get_memory(memory_id)
            now = _utc_now()
            self._conn.execute(
                "UPDATE memory_record SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, memory_id),
            )
            if status != "active":
                self._remove_from_fts(memory_id)
            after = self.get_memory(memory_id)
            self._append_revision(
                memory_id,
                action=status,
                before=before,
                after=after,
                reason=reason,
                actor=actor,
            )
            self._conn.commit()
        return after

    def disable(self, memory_id: str) -> JsonDict:
        return self._set_status(memory_id, "disabled", reason="manual_disable", actor="manual_repl")

    def delete(self, memory_id: str) -> JsonDict:
        return self._set_status(memory_id, "deleted", reason="manual_delete", actor="manual_repl")

    def restore(self, memory_id: str) -> JsonDict:
        before = self.get_memory(memory_id)
        with self._lock:
            self._conn.execute(
                "UPDATE memory_record SET status = 'active', updated_at = ? WHERE id = ?",
                (_utc_now(), memory_id),
            )
            after = self.get_memory(memory_id)
            self._sync_fts(after["id"], after["summary"], after["type"], after["tags"])
            self._append_revision(
                memory_id,
                action="restored",
                before=before,
                after=after,
                reason="manual_restore",
                actor="manual_repl",
            )
            self._conn.commit()
        return after

    def recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        top_k: int = 3,
    ) -> JsonDict:
        import time

        started = time.perf_counter()
        policy = context_use_policy or {}
        blocked = set(policy.get("blocked_context_sources", []))
        if "memory_recollection" in blocked:
            return self._empty_recollection(
                query_text,
                "memory_recollection_blocked_by_context_policy",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if not query_text.strip():
            return self._empty_recollection(
                query_text,
                "empty_or_proactive_query",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        tokens = _search_tokens(query_text)
        rows: list[sqlite3.Row] = []
        if tokens:
            expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:24])
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT m.*, bm25(memory_fts) AS lexical_rank
                    FROM memory_fts
                    JOIN memory_record m ON m.id = memory_fts.memory_id
                    WHERE memory_fts MATCH ?
                      AND m.owner_user_id = ?
                      AND m.agent_id = ?
                      AND m.status = 'active'
                      AND m.retrieval_permission = 'allow'
                    ORDER BY lexical_rank ASC
                    LIMIT ?
                    """,
                    (expression, self.owner_user_id, self.agent_id, max(top_k * 4, 12)),
                ).fetchall()

        if not rows:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT *, 0.0 AS lexical_rank
                    FROM memory_record
                    WHERE owner_user_id = ? AND agent_id = ? AND status = 'active'
                      AND retrieval_permission = 'allow'
                      AND summary LIKE ?
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (self.owner_user_id, self.agent_id, f"%{query_text.strip()}%", top_k),
                ).fetchall()

        scored: list[tuple[float, JsonDict]] = []
        query_tokens = set(tokens)
        for row in rows:
            memory = self._memory_from_row(row)
            memory_tokens = set(_search_tokens(" ".join([memory["summary"], *memory["tags"]])))
            overlap = len(query_tokens & memory_tokens) / max(len(query_tokens), 1)
            score = (
                0.65 * overlap
                + 0.2 * float(memory["importance"])
                + 0.15 * float(memory["confidence"])
            )
            if memory["sensitivity"] == "sensitive":
                score -= 0.1
            scored.append((score, memory))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        selected = scored[:top_k]

        explicit = any(term.lower() in query_text.lower() for term in _EXPLICIT_RECALL_TERMS)
        surface_action = "gist" if explicit and selected else ("implicit" if selected else "none")
        items = [
            {
                "memory_id": memory["id"],
                "type": memory["type"],
                "gist": memory["summary"],
                "confidence": memory["confidence"],
                "evidence_strength": "strong" if memory["evidence_ids"] else "weak",
                "use_permission": memory["use_permission"],
                "disclosure_permission": memory["disclosure_permission"],
                "score": round(score, 4),
            }
            for score, memory in selected
        ]
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        reason = (
            "explicit recall request with governed gist"
            if surface_action == "gist"
            else "relevant canonical memories used implicitly"
            if items
            else "no sufficiently relevant canonical memory"
        )
        trace_id = self._record_recall_trace(
            query_text,
            surface_action=surface_action,
            reason=reason,
            selected=selected,
            latency_ms=latency_ms,
        )
        return {
            "surface_action": surface_action,
            "items": items,
            "retrieved_ids": [item["memory_id"] for item in items],
            "memory_summaries": [item["gist"] for item in items],
            "evidence_level": "strong" if items else "none",
            "forbidden_disclosures": ["raw_evidence"],
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "sqlite_fts",
            "degraded": False,
            "latency_ms": latency_ms,
        }

    def _empty_recollection(self, query_text: str, reason: str, *, latency_ms: float) -> JsonDict:
        trace_id = self._record_recall_trace(
            query_text,
            surface_action="none",
            reason=reason,
            selected=[],
            latency_ms=round(latency_ms, 2),
        )
        return {
            "surface_action": "none",
            "items": [],
            "retrieved_ids": [],
            "memory_summaries": [],
            "evidence_level": "none",
            "forbidden_disclosures": [],
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "sqlite_fts",
            "degraded": False,
            "latency_ms": round(latency_ms, 2),
        }

    def _record_recall_trace(
        self,
        query_text: str,
        *,
        surface_action: str,
        reason: str,
        selected: list[tuple[float, JsonDict]],
        latency_ms: float,
    ) -> str:
        trace_id = _new_id("recall")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO recall_trace (
                    id, query_text, backend, surface_action, decision_reason,
                    degraded, latency_ms, created_at
                ) VALUES (?, ?, 'sqlite_fts', ?, ?, 0, ?, ?)
                """,
                (trace_id, query_text, surface_action, reason, latency_ms, _utc_now()),
            )
            for rank, (score, memory) in enumerate(selected, start=1):
                self._conn.execute(
                    """
                    INSERT INTO recall_trace_item (
                        trace_id, memory_id, rank, score, selected, filter_reason
                    ) VALUES (?, ?, ?, ?, 1, NULL)
                    """,
                    (trace_id, memory["id"], rank, score),
                )
            self._conn.commit()
        return trace_id

    def search(self, query_text: str, *, top_k: int = 10) -> list[JsonDict]:
        frame = self.recall(query_text, top_k=top_k)
        return [self.get_memory(memory_id) for memory_id in frame["retrieved_ids"]]

    def get_memory(self, memory_id: str | None) -> JsonDict:
        if not memory_id:
            raise ValueError("memory id is required")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_record WHERE id = ?",
                (memory_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"memory not found: {memory_id}")
        return self._memory_from_row(row)

    def list_memories(self, status: str | None = None) -> list[JsonDict]:
        sql = "SELECT * FROM memory_record WHERE owner_user_id = ? AND agent_id = ?"
        params: list[Any] = [self.owner_user_id, self.agent_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_candidate WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"candidate not found: {candidate_id}")
        return self._candidate_from_row(row)

    def list_candidates(self, status: str | None = None) -> list[JsonDict]:
        sql = "SELECT * FROM memory_candidate"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def evidence_for_memory(self, memory_id: str) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.*
                FROM evidence e
                JOIN memory_evidence me ON me.evidence_id = e.id
                WHERE me.memory_id = ?
                ORDER BY e.created_at DESC
                """,
                (memory_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "content_summary": row["content_summary"],
                "raw_pointer": row["raw_pointer"],
                "sensitivity": row["sensitivity"],
                "retention_policy": row["retention_policy"],
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def revisions_for_memory(self, memory_id: str) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_revision WHERE memory_id = ? ORDER BY created_at DESC",
                (memory_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "memory_id": row["memory_id"],
                "action": row["action"],
                "before": _loads(row["before_json"], None),
                "after": _loads(row["after_json"], None),
                "reason": row["reason"],
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_recall_traces(self, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recall_trace ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "query_text": row["query_text"],
                "backend": row["backend"],
                "surface_action": row["surface_action"],
                "decision_reason": row["decision_reason"],
                "degraded": bool(row["degraded"]),
                "latency_ms": row["latency_ms"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def health(self) -> JsonDict:
        with self._lock:
            memory_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_record WHERE status = 'active'"
            ).fetchone()[0]
            pending_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_candidate WHERE status = 'pending'"
            ).fetchone()[0]
        return {
            "status": "ok",
            "mode": "in_process",
            "backend": "sqlite_fts",
            "database": str(self.path),
            "active_memories": memory_count,
            "pending_candidates": pending_count,
        }

    def sync_state_cache(self, state: JsonDict) -> None:
        state.setdefault("memory", {})["items"] = [
            {
                "id": item["id"],
                "type": item["type"],
                "content": item["summary"],
                "tags": item["tags"],
                "status": item["status"],
                "sensitivity": item["sensitivity"],
                "evidence_ids": item["evidence_ids"],
                "created_at": item["created_at"],
                "_projection": "memory_runtime",
            }
            for item in self.list_memories()
        ]
        state["memory"]["runtime"] = self.health()

    def _memory_from_row(self, row: sqlite3.Row) -> JsonDict:
        with self._lock:
            evidence_rows = self._conn.execute(
                "SELECT evidence_id FROM memory_evidence WHERE memory_id = ? ORDER BY evidence_id",
                (row["id"],),
            ).fetchall()
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "summary": row["summary"],
            "content": row["summary"],
            "tags": _loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "sensitivity": row["sensitivity"],
            "evidence_ids": [item["evidence_id"] for item in evidence_rows],
            "supersedes_id": row["supersedes_id"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "retention_policy": row["retention_policy"],
            "retrieval_permission": row["retrieval_permission"],
            "use_permission": row["use_permission"],
            "disclosure_permission": row["disclosure_permission"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "candidate_id": row["id"],
            "id": row["id"],
            "type": row["type"],
            "summary": row["summary"],
            "tags": _loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "sensitivity": row["sensitivity"],
            "reason": row["reason"],
            "evidence_ids": _loads(row["evidence_ids_json"], []),
            "supersedes_id": row["supersedes_id"],
            "explicit_user_request": bool(row["explicit_user_request"]),
            "local_only": bool(row["local_only"]),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "committed_memory_id": row["committed_memory_id"],
        }
