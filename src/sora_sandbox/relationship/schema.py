from __future__ import annotations


RELATIONSHIP_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS relationship_policy_profile (
    version TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_pair (
    pair_id TEXT PRIMARY KEY,
    sora_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    profile_version TEXT NOT NULL,
    tombstone_salt TEXT NOT NULL,
    active_state_snapshot_id TEXT,
    active_estimate_snapshot_id TEXT,
    active_frame_id TEXT,
    learning_paused INTEGER NOT NULL DEFAULT 0,
    surface_permission TEXT NOT NULL DEFAULT 'implicit',
    generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sora_id, user_id),
    FOREIGN KEY(profile_version) REFERENCES relationship_policy_profile(version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_relationship_pair
ON relationship_pair(sora_id, user_id);

CREATE TABLE IF NOT EXISTS relationship_inbox (
    inbox_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    action TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    quarantine_reason TEXT,
    correlation_id TEXT,
    turn_id TEXT,
    origin_chain_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    sensitivity TEXT NOT NULL DEFAULT 'low',
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(source_system, event_id, source_revision, action),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_inbox_queue
ON relationship_inbox(status, next_attempt_at, received_at);

CREATE INDEX IF NOT EXISTS idx_relationship_inbox_pair
ON relationship_inbox(pair_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS relationship_candidate (
    candidate_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    inbox_id TEXT,
    candidate_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    direction_scope TEXT,
    dimension TEXT,
    direction INTEGER,
    evidence_type TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    subject_key TEXT NOT NULL DEFAULT 'general',
    context_key TEXT NOT NULL DEFAULT 'general',
    episode_ref TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    gate_reason TEXT,
    provider_trace_json TEXT NOT NULL DEFAULT '{}',
    deferred_until TEXT,
    next_attempt_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(pair_id, candidate_hash),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id),
    FOREIGN KEY(inbox_id) REFERENCES relationship_inbox(inbox_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_candidate_queue
ON relationship_candidate(status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS idx_relationship_candidate_pair
ON relationship_candidate(pair_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS relationship_candidate_evidence_ref (
    candidate_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, ref_hash),
    FOREIGN KEY(candidate_id) REFERENCES relationship_candidate(candidate_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relationship_contribution (
    contribution_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    direction_scope TEXT NOT NULL,
    dimension TEXT NOT NULL,
    direction INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    context_key TEXT NOT NULL,
    episode_ref TEXT,
    evidence_refs_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_strength REAL NOT NULL,
    source_reliability REAL NOT NULL,
    direction_certainty REAL NOT NULL,
    consistency REAL NOT NULL,
    novelty_factor REAL NOT NULL,
    conflict_score REAL NOT NULL,
    support REAL NOT NULL,
    raw_delta REAL NOT NULL,
    bounded_delta REAL NOT NULL,
    sensitivity TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id),
    FOREIGN KEY(candidate_id) REFERENCES relationship_candidate(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_contribution_window
ON relationship_contribution(pair_id, direction_scope, dimension, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_relationship_contribution_source
ON relationship_contribution(pair_id, source_ref, occurred_at DESC);

CREATE TABLE IF NOT EXISTS relationship_contribution_status_event (
    status_event_id TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    source_ref TEXT,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT,
    schema_version TEXT NOT NULL,
    FOREIGN KEY(contribution_id) REFERENCES relationship_contribution(contribution_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_status_latest
ON relationship_contribution_status_event(contribution_id, occurred_at DESC, status_event_id DESC);

CREATE TABLE IF NOT EXISTS relationship_state_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    bond_stage TEXT NOT NULL,
    values_json TEXT NOT NULL,
    agreement_revision INTEGER NOT NULL,
    repair_state TEXT NOT NULL,
    source_watermark INTEGER NOT NULL,
    source_contribution_count INTEGER NOT NULL,
    episode_count INTEGER NOT NULL,
    relationship_started_at TEXT NOT NULL,
    stage_evidence_json TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    checksum TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    UNIQUE(pair_id, revision),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_state_active
ON relationship_state_snapshot(pair_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS relationship_estimate_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    values_json TEXT NOT NULL,
    source_watermark INTEGER NOT NULL,
    source_contribution_count INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    checksum TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    UNIQUE(pair_id, revision),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_estimate_active
ON relationship_estimate_snapshot(pair_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS relationship_agreement (
    agreement_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    consent_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    supersedes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(pair_id, revision),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_agreement_active
ON relationship_agreement(pair_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS relationship_agreement_amendment_candidate (
    candidate_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    source_repair_episode_id TEXT,
    operation TEXT NOT NULL,
    term_path TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    confirmation_token_hash TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_amendment_pending
ON relationship_agreement_amendment_candidate(pair_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS relationship_repair_episode (
    repair_episode_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger_ref TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    affected_dimensions_json TEXT NOT NULL,
    responsibility_json TEXT NOT NULL,
    requested_repair_json TEXT NOT NULL,
    repair_actions_json TEXT NOT NULL,
    user_outcome TEXT,
    repeated_violation_key TEXT,
    repeated_violation_count INTEGER NOT NULL DEFAULT 0,
    policy_severity TEXT NOT NULL DEFAULT 'normal',
    state_history_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_repair_open
ON relationship_repair_episode(pair_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS relationship_frame (
    frame_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    state_snapshot_id TEXT NOT NULL,
    estimate_snapshot_id TEXT NOT NULL,
    state_snapshot_version INTEGER NOT NULL,
    estimate_snapshot_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    frame_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    invalidated_at TEXT,
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id),
    FOREIGN KEY(state_snapshot_id) REFERENCES relationship_state_snapshot(snapshot_id),
    FOREIGN KEY(estimate_snapshot_id) REFERENCES relationship_estimate_snapshot(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_frame_active
ON relationship_frame(pair_id, status, generated_at DESC);

CREATE TABLE IF NOT EXISTS relationship_outbox (
    event_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
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
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_outbox_delivery
ON relationship_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS relationship_tombstone (
    pair_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(pair_id, ref_hash),
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_tombstone_created
ON relationship_tombstone(pair_id, created_at);

CREATE TABLE IF NOT EXISTS relationship_control_receipt (
    receipt_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    control_type TEXT NOT NULL,
    control_value_json TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);

CREATE TABLE IF NOT EXISTS relationship_audit (
    audit_id TEXT PRIMARY KEY,
    pair_id TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    correlation_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationship_audit_pair
ON relationship_audit(pair_id, created_at DESC);

CREATE TABLE IF NOT EXISTS relationship_metric (
    metric_id TEXT PRIMARY KEY,
    pair_id TEXT,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    labels_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationship_metric_name
ON relationship_metric(name, created_at DESC);

CREATE TABLE IF NOT EXISTS relationship_dead_letter_resolution (
    event_id TEXT PRIMARY KEY,
    resolution TEXT NOT NULL,
    export_ref TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES relationship_outbox(event_id)
);

CREATE TABLE IF NOT EXISTS relationship_rebuild_run (
    run_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    old_state_checksum TEXT,
    shadow_state_checksum TEXT,
    old_estimate_checksum TEXT,
    shadow_estimate_checksum TEXT,
    parity INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(pair_id) REFERENCES relationship_pair(pair_id)
);
"""
