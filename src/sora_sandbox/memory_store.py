from __future__ import annotations

import sqlite3
from typing import Any

from .memory_schema import JsonDict, json_loads


MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    content_summary TEXT NOT NULL,
    raw_content TEXT,
    raw_pointer TEXT,
    source_ref TEXT,
    span_json TEXT,
    content_hash TEXT NOT NULL,
    visibility TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    retention_policy TEXT NOT NULL,
    expires_at TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    delete_reason TEXT
);

CREATE TABLE IF NOT EXISTS memory_event (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    episode_id TEXT,
    payload_json TEXT NOT NULL,
    raw_ref TEXT,
    visibility TEXT NOT NULL,
    retention_policy TEXT NOT NULL,
    sensitivity_hint TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    delete_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_event_episode
ON memory_event(episode_id);

CREATE INDEX IF NOT EXISTS idx_memory_event_session
ON memory_event(session_id, created_at);

CREATE TABLE IF NOT EXISTS memory_episode (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    topic_hint TEXT,
    gist TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    sealed_reason TEXT,
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    delete_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_episode_session
ON memory_episode(session_id, updated_at);

CREATE TABLE IF NOT EXISTS extraction_job (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    input_summary TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    available_at TEXT,
    claimed_at TEXT,
    completed_at TEXT,
    error TEXT,
    result_summary TEXT,
    FOREIGN KEY (episode_id) REFERENCES memory_episode(id)
);

CREATE INDEX IF NOT EXISTS idx_extraction_job_episode
ON extraction_job(episode_id);

CREATE INDEX IF NOT EXISTS idx_extraction_job_status
ON extraction_job(status, created_at);

CREATE TABLE IF NOT EXISTS delete_propagation (
    id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    source_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delete_propagation_source
ON delete_propagation(source_ref, created_at);

CREATE INDEX IF NOT EXISTS idx_delete_propagation_target
ON delete_propagation(target_type, target_id);

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
    index_watermark TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
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
    committed_memory_id TEXT,
    operation TEXT NOT NULL DEFAULT 'ADD',
    target_memory_id TEXT,
    importance REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.5,
    specificity REAL NOT NULL DEFAULT 0.5,
    novelty REAL NOT NULL DEFAULT 1.0,
    conflict_score REAL NOT NULL DEFAULT 0.0,
    extraction_job_id TEXT,
    schema_version TEXT NOT NULL DEFAULT 'legacy',
    candidate_hash TEXT,
    gate_decision_id TEXT,
    quarantine_id TEXT,
    deferred_until TEXT,
    trace_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_candidate_hash
ON memory_candidate(candidate_hash);

CREATE TABLE IF NOT EXISTS memory_gate_decision (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_memory_id TEXT,
    policy_version TEXT NOT NULL,
    gate_score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    risk_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES memory_candidate(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_gate_decision_candidate
ON memory_gate_decision(candidate_id, created_at);

CREATE TABLE IF NOT EXISTS memory_review_item (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    gate_decision_id TEXT NOT NULL,
    queue TEXT NOT NULL,
    status TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    reviewer_visible_evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT,
    FOREIGN KEY (candidate_id) REFERENCES memory_candidate(id),
    FOREIGN KEY (gate_decision_id) REFERENCES memory_gate_decision(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_review_item_status
ON memory_review_item(status, created_at);

CREATE TABLE IF NOT EXISTS memory_quarantine (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    risk_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    redacted_summary TEXT NOT NULL,
    encrypted_payload_ref TEXT,
    retention_policy TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    audit_trace_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_quarantine_source
ON memory_quarantine(source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_memory_quarantine_status
ON memory_quarantine(status, created_at);

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

CREATE TABLE IF NOT EXISTS memory_index_outbox (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    payload_json TEXT NOT NULL,
    payload_hash TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    error_type TEXT,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    index_generation TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_index_outbox_status
ON memory_index_outbox(status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_index_outbox_memory
ON memory_index_outbox(memory_id, backend, status);

CREATE INDEX IF NOT EXISTS idx_memory_index_outbox_generation
ON memory_index_outbox(backend, index_generation, status);

CREATE TABLE IF NOT EXISTS memory_index_document (
    memory_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    embedding_input_version TEXT NOT NULL,
    policy_snapshot_version TEXT NOT NULL,
    index_generation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    text_for_fts TEXT NOT NULL,
    text_for_embedding TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, index_generation, schema_version),
    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_index_document_hash
ON memory_index_document(payload_hash);

CREATE TABLE IF NOT EXISTS memory_embedding (
    memory_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    vector_dim INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    vector_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    index_generation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, embedding_model, index_generation),
    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
);

CREATE TABLE IF NOT EXISTS memory_vector_index (
    memory_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    collection TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status TEXT NOT NULL,
    vector_hash TEXT NOT NULL,
    index_generation TEXT NOT NULL,
    index_watermark TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, backend, index_generation),
    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_vector_index_status
ON memory_vector_index(backend, index_generation, status);

CREATE TABLE IF NOT EXISTS memory_graph_edge (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    src_type TEXT NOT NULL,
    src_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    dst_type TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    weight REAL NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    index_generation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_graph_edge_src
ON memory_graph_edge(src_type, src_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_memory_graph_edge_dst
ON memory_graph_edge(dst_type, dst_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_memory_graph_edge_memory
ON memory_graph_edge(memory_id, index_generation);

CREATE TABLE IF NOT EXISTS memory_access_signal (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    trace_id TEXT,
    action TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    usefulness REAL,
    surface_action TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memory_record(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_access_signal_memory
ON memory_access_signal(memory_id, accessed_at);

CREATE TABLE IF NOT EXISTS memory_index_rebuild_run (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    index_generation TEXT NOT NULL,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL,
    processed_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    watermark_memory_id TEXT,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_index_rebuild_status
ON memory_index_rebuild_run(status, created_at);

CREATE TABLE IF NOT EXISTS memory_projector_outbox (
    id TEXT PRIMARY KEY,
    projector_name TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_projector_outbox_status
ON memory_projector_outbox(status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_projector_outbox_memory
ON memory_projector_outbox(memory_id, projector_name, status);

CREATE TABLE IF NOT EXISTS memory_projector_checkpoint (
    projector_name TEXT PRIMARY KEY,
    last_event_id TEXT,
    last_memory_id TEXT,
    status TEXT NOT NULL,
    processed_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_forget_request (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    result_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_forget_request_target
ON memory_forget_request(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS memory_canonical_eval_run (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    failed_cases INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_canonical_eval_case (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_name TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES memory_canonical_eval_run(id)
);

CREATE TABLE IF NOT EXISTS memory_recall_eval_run (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    failed_cases INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_recall_eval_case (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_name TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES memory_recall_eval_run(id)
);

CREATE TABLE IF NOT EXISTS memory_planner_eval_run (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    failed_cases INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_planner_eval_case (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_name TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES memory_planner_eval_run(id)
);

CREATE TABLE IF NOT EXISTS recall_plan_trace (
    plan_id TEXT PRIMARY KEY,
    recall_trace_id TEXT,
    turn_id TEXT,
    session_id TEXT,
    planner_version TEXT NOT NULL,
    planner_mode TEXT NOT NULL,
    decision TEXT NOT NULL,
    query_intent TEXT NOT NULL,
    scene TEXT NOT NULL,
    explicit_recall INTEGER NOT NULL,
    skipped_reason TEXT,
    subqueries_json TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    decision_reasons_json TEXT NOT NULL,
    llm_usage_json TEXT NOT NULL,
    planner_trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_plan_trace_recall
ON recall_plan_trace(recall_trace_id);

CREATE INDEX IF NOT EXISTS idx_recall_plan_trace_decision
ON recall_plan_trace(decision, query_intent, created_at);

CREATE TABLE IF NOT EXISTS recall_plan_cache (
    cache_key TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    query_text TEXT NOT NULL,
    scene TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_plan_cache_status
ON recall_plan_cache(status, expires_at);

CREATE TABLE IF NOT EXISTS recall_trace (
    id TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    backend TEXT NOT NULL,
    surface_action TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    degraded INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    token_budget INTEGER NOT NULL DEFAULT 0,
    token_used INTEGER NOT NULL DEFAULT 0,
    final_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    cost_json TEXT NOT NULL DEFAULT '{}',
    pinned INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_trace_item (
    trace_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    selected INTEGER NOT NULL,
    backend TEXT,
    backend_rank INTEGER,
    backend_score REAL,
    rrf_score REAL,
    rerank_score REAL,
    matched_backends_json TEXT NOT NULL DEFAULT '[]',
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


def init_memory_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MEMORY_SCHEMA_SQL)
    ensure_column(conn, "evidence", "raw_content", "TEXT")
    ensure_column(conn, "evidence", "source_ref", "TEXT")
    ensure_column(conn, "evidence", "span_json", "TEXT")
    ensure_column(conn, "evidence", "content_hash", "TEXT")
    ensure_column(conn, "evidence", "visibility", "TEXT")
    ensure_column(conn, "evidence", "expires_at", "TEXT")
    ensure_column(conn, "evidence", "deleted_at", "TEXT")
    ensure_column(conn, "evidence", "delete_reason", "TEXT")
    ensure_column(conn, "memory_event", "raw_ref", "TEXT")
    ensure_column(conn, "memory_event", "episode_id", "TEXT")
    ensure_column(conn, "memory_event", "deleted_at", "TEXT")
    ensure_column(conn, "memory_event", "delete_reason", "TEXT")
    ensure_column(conn, "memory_episode", "deleted_at", "TEXT")
    ensure_column(conn, "memory_episode", "delete_reason", "TEXT")
    ensure_column(conn, "extraction_job", "error", "TEXT")
    ensure_column(conn, "extraction_job", "priority", "INTEGER DEFAULT 50")
    ensure_column(conn, "extraction_job", "available_at", "TEXT")
    ensure_column(conn, "extraction_job", "claimed_at", "TEXT")
    ensure_column(conn, "extraction_job", "completed_at", "TEXT")
    ensure_column(conn, "extraction_job", "result_summary", "TEXT")
    ensure_column(conn, "memory_candidate", "operation", "TEXT NOT NULL DEFAULT 'ADD'")
    ensure_column(conn, "memory_candidate", "target_memory_id", "TEXT")
    ensure_column(conn, "memory_candidate", "importance", "REAL NOT NULL DEFAULT 0.5")
    ensure_column(conn, "memory_candidate", "stability", "REAL NOT NULL DEFAULT 0.5")
    ensure_column(conn, "memory_candidate", "specificity", "REAL NOT NULL DEFAULT 0.5")
    ensure_column(conn, "memory_candidate", "novelty", "REAL NOT NULL DEFAULT 1.0")
    ensure_column(conn, "memory_candidate", "conflict_score", "REAL NOT NULL DEFAULT 0.0")
    ensure_column(conn, "memory_candidate", "extraction_job_id", "TEXT")
    ensure_column(conn, "memory_candidate", "schema_version", "TEXT NOT NULL DEFAULT 'legacy'")
    ensure_column(conn, "memory_candidate", "candidate_hash", "TEXT")
    ensure_column(conn, "memory_candidate", "gate_decision_id", "TEXT")
    ensure_column(conn, "memory_candidate", "quarantine_id", "TEXT")
    ensure_column(conn, "memory_candidate", "deferred_until", "TEXT")
    ensure_column(conn, "memory_candidate", "trace_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "memory_record", "index_watermark", "TEXT")
    ensure_column(conn, "memory_record", "access_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "memory_record", "last_accessed_at", "TEXT")
    ensure_column(conn, "memory_index_outbox", "max_attempts", "INTEGER NOT NULL DEFAULT 3")
    ensure_column(conn, "memory_index_outbox", "payload_hash", "TEXT")
    ensure_column(conn, "memory_index_outbox", "result_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "memory_index_outbox", "error_type", "TEXT")
    ensure_column(conn, "memory_index_outbox", "last_attempt_at", "TEXT")
    ensure_column(conn, "memory_index_outbox", "next_attempt_at", "TEXT")
    ensure_column(conn, "memory_index_outbox", "index_generation", "TEXT NOT NULL DEFAULT 'active'")
    ensure_column(conn, "recall_trace", "plan_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "recall_trace", "token_budget", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "recall_trace", "token_used", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "recall_trace", "final_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "recall_trace", "item_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "recall_trace", "cost_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "recall_trace", "pinned", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "recall_trace", "expires_at", "TEXT")
    ensure_column(conn, "recall_plan_trace", "recall_trace_id", "TEXT")
    ensure_column(conn, "recall_plan_trace", "turn_id", "TEXT")
    ensure_column(conn, "recall_plan_trace", "session_id", "TEXT")
    ensure_column(conn, "recall_plan_trace", "llm_usage_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "recall_plan_trace", "planner_trace_json", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "recall_trace_item", "backend", "TEXT")
    ensure_column(conn, "recall_trace_item", "backend_rank", "INTEGER")
    ensure_column(conn, "recall_trace_item", "backend_score", "REAL")
    ensure_column(conn, "recall_trace_item", "rrf_score", "REAL")
    ensure_column(conn, "recall_trace_item", "rerank_score", "REAL")
    ensure_column(conn, "recall_trace_item", "matched_backends_json", "TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row["name"] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def evidence_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "id": row["id"],
        "source_type": row["source_type"],
        "content_summary": row["content_summary"],
        "raw_content": row["raw_content"],
        "raw_pointer": row["raw_pointer"],
        "source_ref": row_value(row, "source_ref"),
        "span": json_loads(row_value(row, "span_json", "{}") or "{}", {}),
        "content_hash": row_value(row, "content_hash"),
        "visibility": row_value(row, "visibility", "private") or "private",
        "sensitivity": row["sensitivity"],
        "retention_policy": row["retention_policy"],
        "expires_at": row_value(row, "expires_at"),
        "metadata": json_loads(row["metadata_json"], {}),
        "created_at": row["created_at"],
        "deleted_at": row_value(row, "deleted_at"),
        "delete_reason": row_value(row, "delete_reason"),
    }


def event_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "event_id": row["id"],
        "id": row["id"],
        "source_type": row["source_type"],
        "actor": row["actor"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "episode_id": row["episode_id"],
        "payload": json_loads(row["payload_json"], {}),
        "raw_ref": row["raw_ref"],
        "visibility": row["visibility"],
        "retention_policy": row["retention_policy"],
        "sensitivity_hint": row["sensitivity_hint"],
        "event_hash": row["event_hash"],
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
        "delete_reason": row["delete_reason"],
    }


def episode_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "episode_id": row["id"],
        "id": row["id"],
        "session_id": row["session_id"],
        "status": row["status"],
        "topic_hint": row["topic_hint"],
        "gist": row["gist"],
        "event_ids": json_loads(row["event_ids_json"], []),
        "sealed_reason": row["sealed_reason"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "delete_reason": row["delete_reason"],
    }


def extraction_job_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "job_id": row["id"],
        "id": row["id"],
        "episode_id": row["episode_id"],
        "status": row["status"],
        "reason": row["reason"],
        "attempts": row["attempts"],
        "input_summary": row["input_summary"],
        "event_ids": json_loads(row["event_ids_json"], []),
        "priority": row_value(row, "priority", 50) or 50,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "available_at": row_value(row, "available_at"),
        "claimed_at": row_value(row, "claimed_at"),
        "completed_at": row_value(row, "completed_at"),
        "error": row_value(row, "error"),
        "result_summary": row_value(row, "result_summary"),
    }


def revision_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "id": row["id"],
        "memory_id": row["memory_id"],
        "action": row["action"],
        "before": json_loads(row["before_json"], None),
        "after": json_loads(row["after_json"], None),
        "reason": row["reason"],
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


def gate_decision_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "decision_id": row["id"],
        "id": row["id"],
        "candidate_id": row["candidate_id"],
        "decision": row["decision"],
        "operation": row["operation"],
        "target_memory_id": row["target_memory_id"],
        "policy_version": row["policy_version"],
        "gate_score": row["gate_score"],
        "reasons": json_loads(row["reasons_json"], []),
        "risk": json_loads(row["risk_json"], {}),
        "scores": json_loads(row["scores_json"], {}),
        "trace": json_loads(row["trace_json"], {}),
        "created_at": row["created_at"],
    }


def review_item_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "review_id": row["id"],
        "id": row["id"],
        "candidate_id": row["candidate_id"],
        "gate_decision_id": row["gate_decision_id"],
        "queue": row["queue"],
        "status": row["status"],
        "suggested_action": row["suggested_action"],
        "reviewer_visible_evidence": json_loads(row["reviewer_visible_evidence_json"], []),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
        "resolution": row["resolution"],
    }


def quarantine_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "quarantine_id": row["id"],
        "id": row["id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "risk_type": row["risk_type"],
        "severity": row["severity"],
        "reason": row["reason"],
        "redacted_summary": row["redacted_summary"],
        "encrypted_payload_ref": row["encrypted_payload_ref"],
        "retention_policy": row["retention_policy"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "policy_version": row["policy_version"],
        "audit_trace_id": row["audit_trace_id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
    }


def delete_propagation_from_row(row: sqlite3.Row) -> JsonDict:
    return {
        "id": row["id"],
        "source_ref": row["source_ref"],
        "source_type": row["source_type"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "action": row["action"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "metadata": json_loads(row["metadata_json"], {}),
    }


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
