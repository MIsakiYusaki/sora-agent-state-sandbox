from __future__ import annotations


AFFECT_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS affect_instance (
    sora_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    active_snapshot_id TEXT NOT NULL,
    state_revision INTEGER NOT NULL DEFAULT 0,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affect_stimulus_event (
    event_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    actor_user_id TEXT,
    target_kind TEXT NOT NULL,
    target_ref TEXT,
    relationship_ref TEXT,
    conversation_scope TEXT NOT NULL,
    source_confidence REAL NOT NULL,
    event_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    raw_text TEXT,
    content_hash TEXT,
    sensitivity TEXT NOT NULL,
    correlation_id TEXT,
    semantic_status TEXT NOT NULL DEFAULT 'pending',
    semantic_attempts INTEGER NOT NULL DEFAULT 0,
    semantic_error TEXT,
    semantic_next_attempt_at TEXT,
    retention_expires_at TEXT,
    processed_at TEXT,
    UNIQUE(sora_id, source_ref, source_revision),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_event_semantic
ON affect_stimulus_event(sora_id, semantic_status, occurred_at);

CREATE INDEX IF NOT EXISTS idx_affect_event_target
ON affect_stimulus_event(sora_id, target_ref, occurred_at);

CREATE INDEX IF NOT EXISTS idx_affect_event_relationship
ON affect_stimulus_event(sora_id, relationship_ref, occurred_at);

CREATE INDEX IF NOT EXISTS idx_affect_event_source_ref
ON affect_stimulus_event(sora_id, source_ref);

CREATE TABLE IF NOT EXISTS affect_event_evidence_ref (
    sora_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(sora_id, ref_hash, event_id),
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id) ON DELETE CASCADE,
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_event_evidence_lookup
ON affect_event_evidence_ref(sora_id, ref_hash, event_id);

CREATE TABLE IF NOT EXISTS affect_appraisal (
    appraisal_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    sora_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    revision INTEGER NOT NULL,
    emotion_type TEXT,
    target_kind TEXT NOT NULL,
    target_ref TEXT,
    vector_json TEXT NOT NULL,
    strength REAL NOT NULL,
    confidence REAL NOT NULL,
    factors_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    provider_trace_json TEXT NOT NULL,
    supersedes_appraisal_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    deactivation_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, lane, revision),
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_appraisal_replay
ON affect_appraisal(sora_id, active, effective_at, appraisal_id);

CREATE TABLE IF NOT EXISTS affect_appraisal_evidence_ref (
    sora_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    appraisal_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(sora_id, ref_hash, appraisal_id),
    FOREIGN KEY(appraisal_id) REFERENCES affect_appraisal(appraisal_id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id) ON DELETE CASCADE,
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_appraisal_evidence_lookup
ON affect_appraisal_evidence_ref(sora_id, ref_hash, appraisal_id);

CREATE TABLE IF NOT EXISTS affect_contribution (
    contribution_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    appraisal_id TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    core_delta_json TEXT NOT NULL,
    mood_delta_json TEXT NOT NULL,
    capacity_cost_json TEXT NOT NULL,
    need_delta_json TEXT NOT NULL,
    emotion_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    deactivation_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id),
    FOREIGN KEY(appraisal_id) REFERENCES affect_appraisal(appraisal_id),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_contribution_replay
ON affect_contribution(sora_id, active, effective_at, contribution_id);

CREATE TABLE IF NOT EXISTS affect_emotion_episode (
    episode_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    appraisal_id TEXT NOT NULL,
    emotion_type TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_ref TEXT,
    relationship_ref TEXT,
    conversation_scope TEXT NOT NULL,
    cause_refs_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_kinds_json TEXT NOT NULL DEFAULT '[]',
    source_types_json TEXT NOT NULL DEFAULT '[]',
    peak_intensity REAL NOT NULL,
    current_intensity REAL NOT NULL,
    persistence REAL NOT NULL,
    resolution_state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    memory_proposal_id TEXT,
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id),
    FOREIGN KEY(appraisal_id) REFERENCES affect_appraisal(appraisal_id),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_episode_target
ON affect_emotion_episode(sora_id, target_ref, active, updated_at);

CREATE TABLE IF NOT EXISTS affect_episode_source_event (
    sora_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(sora_id, episode_id, event_id),
    FOREIGN KEY(episode_id) REFERENCES affect_emotion_episode(episode_id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES affect_stimulus_event(event_id) ON DELETE CASCADE,
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_episode_source_lookup
ON affect_episode_source_event(sora_id, event_id, episode_id);

CREATE TABLE IF NOT EXISTS affect_core_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    state_revision INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    source_contribution_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sora_id, state_revision),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE TABLE IF NOT EXISTS affect_outbox (
    event_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(event_key, payload_hash),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_outbox_pending
ON affect_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS affect_trace (
    trace_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    correlation_id TEXT,
    event_id TEXT,
    appraisal_id TEXT,
    snapshot_id TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_trace_correlation
ON affect_trace(correlation_id, created_at);

CREATE TABLE IF NOT EXISTS affect_metric (
    metric_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    labels_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_metric_name
ON affect_metric(name, created_at);

CREATE TABLE IF NOT EXISTS affect_control (
    sora_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    processing_paused INTEGER NOT NULL DEFAULT 0,
    expression_permission TEXT NOT NULL DEFAULT 'implicit',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(sora_id, user_id),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE TABLE IF NOT EXISTS affect_forget_tombstone (
    sora_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(sora_id, ref_hash),
    FOREIGN KEY(sora_id) REFERENCES affect_instance(sora_id)
);

CREATE INDEX IF NOT EXISTS idx_affect_forget_created
ON affect_forget_tombstone(sora_id, created_at);
"""
