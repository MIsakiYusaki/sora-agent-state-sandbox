from __future__ import annotations

import sqlite3

from .contracts import PERSONA_SCHEMA_VERSION


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS persona_schema_migration (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_definition (
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','rolled_back')),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    PRIMARY KEY(agent_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_definition_active
ON persona_definition(agent_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS persona_trait_definition (
    agent_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    trait_key TEXT NOT NULL,
    initial_value REAL NOT NULL,
    min_bound REAL NOT NULL,
    max_bound REAL NOT NULL,
    inertia REAL NOT NULL,
    per_event_limit REAL NOT NULL,
    daily_limit REAL NOT NULL,
    weekly_limit REAL NOT NULL,
    monthly_limit REAL NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY(agent_id, definition_version, trait_key),
    FOREIGN KEY(agent_id, definition_version) REFERENCES persona_definition(agent_id, version)
);

CREATE TABLE IF NOT EXISTS persona_inbox (
    event_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    source_ref TEXT,
    status TEXT NOT NULL CHECK(status IN ('accepted','duplicate','tombstoned','rejected_policy')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_persona_inbox_source ON persona_inbox(source_ref);

CREATE TABLE IF NOT EXISTS persona_candidate (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT,
    target_scope TEXT,
    target_key TEXT,
    direction INTEGER,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','evaluating','pending_review','approved','committed','rejected','quarantined','forgotten')),
    candidate_json TEXT NOT NULL DEFAULT '{}',
    source_payload_json TEXT NOT NULL,
    gate_reason TEXT,
    error_type TEXT,
    error TEXT,
    model_trace_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(agent_id, idempotency_key, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_persona_candidate_status ON persona_candidate(status, created_at);
CREATE INDEX IF NOT EXISTS idx_persona_candidate_source ON persona_candidate(source_ref);

CREATE TABLE IF NOT EXISTS persona_contribution (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    user_id TEXT,
    target_scope TEXT NOT NULL,
    target_key TEXT NOT NULL,
    delta REAL NOT NULL,
    support REAL NOT NULL,
    source_ref TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    deactivated_at TEXT,
    deactivation_reason TEXT,
    FOREIGN KEY(candidate_id) REFERENCES persona_candidate(id)
);
CREATE INDEX IF NOT EXISTS idx_persona_contribution_rebuild
ON persona_contribution(agent_id, target_scope, target_key, active, created_at);
CREATE INDEX IF NOT EXISTS idx_persona_contribution_source
ON persona_contribution(source_ref, active);

CREATE TABLE IF NOT EXISTS persona_snapshot (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_version INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    trait_values_json TEXT NOT NULL,
    source_contribution_count INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    generation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agent_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_snapshot_active
ON persona_snapshot(agent_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS persona_user_adaptation (
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    trait_key TEXT NOT NULL,
    value REAL NOT NULL,
    version INTEGER NOT NULL,
    evidence_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, user_id, trait_key)
);

CREATE TABLE IF NOT EXISTS persona_frame (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT,
    snapshot_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    frame_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    stale_until TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES persona_snapshot(id)
);
CREATE INDEX IF NOT EXISTS idx_persona_frame_lookup
ON persona_frame(agent_id, user_id, status, created_at);

CREATE TABLE IF NOT EXISTS persona_control (
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    growth_paused INTEGER NOT NULL DEFAULT 0,
    self_reference_permission TEXT NOT NULL DEFAULT 'implicit'
        CHECK(self_reference_permission IN ('silent','implicit','explicit')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, user_id)
);

CREATE TABLE IF NOT EXISTS persona_reflection (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    reflection_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','redacted')),
    model_trace_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES persona_snapshot(id)
);

CREATE TABLE IF NOT EXISTS persona_tombstone (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    source_ref_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE(agent_id, source_ref_hash)
);

CREATE TABLE IF NOT EXISTS persona_outbox (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_ref TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','delivered','dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT,
    UNIQUE(event_type, aggregate_ref, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_persona_outbox_pending ON persona_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS persona_rebuild_run (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_checksum TEXT,
    after_checksum TEXT,
    parity_ok INTEGER,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS persona_audit (
    id TEXT PRIMARY KEY,
    correlation_id TEXT,
    agent_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_audit_action ON persona_audit(action, created_at);
CREATE INDEX IF NOT EXISTS idx_persona_audit_correlation ON persona_audit(correlation_id);

CREATE TABLE IF NOT EXISTS persona_metric (
    id TEXT PRIMARY KEY,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    labels_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_metric_name ON persona_metric(metric_name, created_at);
"""


def initialize_schema(conn: sqlite3.Connection, *, now: str, checksum: str) -> None:
    conn.executescript(DDL)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > PERSONA_SCHEMA_VERSION:
        raise RuntimeError(
            f"persona database schema {current} is newer than supported {PERSONA_SCHEMA_VERSION}"
        )
    if current < PERSONA_SCHEMA_VERSION:
        conn.execute(
            "INSERT OR IGNORE INTO persona_schema_migration(version, applied_at, checksum) VALUES (?, ?, ?)",
            (PERSONA_SCHEMA_VERSION, now, checksum),
        )
        conn.execute(f"PRAGMA user_version={PERSONA_SCHEMA_VERSION}")
    conn.commit()
