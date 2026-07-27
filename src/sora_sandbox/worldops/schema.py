from __future__ import annotations


WORLDOPS_CLIENT_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS worldops_identity (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sora_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    active_bundle_id TEXT,
    active_bundle_version TEXT,
    active_release_sequence INTEGER NOT NULL DEFAULT 0,
    minimum_accepted_sequence INTEGER NOT NULL DEFAULT 0,
    revocation_watermark INTEGER NOT NULL DEFAULT 0,
    state_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, user_id, sora_id, timeline_id)
);

CREATE TABLE IF NOT EXISTS worldops_trusted_key (
    key_id TEXT PRIMARY KEY,
    algorithm TEXT NOT NULL,
    public_key BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    revoked_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS worldops_bundle_registry (
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    world_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    cohort TEXT NOT NULL,
    release_sequence INTEGER NOT NULL,
    minimum_accepted_sequence INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    revocation_list_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    status TEXT NOT NULL,
    verified_at TEXT,
    activated_at TEXT,
    rejected_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(bundle_id, version),
    UNIQUE(world_id, channel, cohort, release_sequence),
    FOREIGN KEY(key_id) REFERENCES worldops_trusted_key(key_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_bundle_active
ON worldops_bundle_registry(world_id, channel, cohort, status, release_sequence);

CREATE TABLE IF NOT EXISTS worldops_timeline (
    timeline_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sora_id TEXT NOT NULL,
    world_id TEXT NOT NULL,
    parent_timeline_id TEXT,
    fork_revision INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    world_clock REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, user_id, sora_id, timeline_id)
);

CREATE TABLE IF NOT EXISTS worldops_event_instance (
    instance_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    bundle_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    selected_choice_id TEXT,
    next_wakeup_at TEXT,
    activated_at TEXT,
    completed_at TEXT,
    archived_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(timeline_id, event_id, definition_version),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_event_state
ON worldops_event_instance(timeline_id, state, next_wakeup_at);

CREATE TABLE IF NOT EXISTS worldops_choice_ledger (
    choice_ledger_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    interpretation_id TEXT NOT NULL,
    choice_id TEXT,
    interpretation_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(timeline_id, input_id),
    UNIQUE(interpretation_id),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id),
    FOREIGN KEY(instance_id) REFERENCES worldops_event_instance(instance_id)
);

CREATE TABLE IF NOT EXISTS worldops_task_evidence (
    evidence_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(instance_id, task_id, input_id, evidence_type),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id),
    FOREIGN KEY(instance_id) REFERENCES worldops_event_instance(instance_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_task_evidence
ON worldops_task_evidence(instance_id, task_id, accepted, consumed);

CREATE TABLE IF NOT EXISTS worldops_timeline_fact (
    fact_revision_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_value_json TEXT NOT NULL,
    source_instance_id TEXT,
    source_transition_id TEXT,
    valid_from_revision INTEGER NOT NULL,
    valid_to_revision INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(timeline_id, fact_key, valid_from_revision),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_fact_active
ON worldops_timeline_fact(timeline_id, fact_key, valid_to_revision);

CREATE TABLE IF NOT EXISTS worldops_transition_ledger (
    transition_id TEXT PRIMARY KEY,
    origin_action_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    event_revision INTEGER NOT NULL,
    timeline_revision INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_ref TEXT,
    choice_id TEXT,
    effects_json TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    bundle_id TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(instance_id, event_revision),
    UNIQUE(timeline_id, origin_action_id),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id),
    FOREIGN KEY(instance_id) REFERENCES worldops_event_instance(instance_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_transition_replay
ON worldops_transition_ledger(timeline_id, timeline_revision, transition_id);

CREATE TABLE IF NOT EXISTS worldops_clock_ledger (
    clock_event_id TEXT PRIMARY KEY,
    origin_action_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    timeline_revision INTEGER NOT NULL,
    delta REAL NOT NULL,
    world_clock REAL NOT NULL,
    reason TEXT NOT NULL,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(timeline_id, timeline_revision),
    UNIQUE(timeline_id, origin_action_id),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_clock_replay
ON worldops_clock_ledger(timeline_id, timeline_revision);

CREATE TABLE IF NOT EXISTS worldops_outbox (
    event_id TEXT PRIMARY KEY,
    timeline_id TEXT,
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
    UNIQUE(event_key, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_worldops_outbox_pending
ON worldops_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS worldops_consumer_effect (
    source_event_id TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_event_id, effect_key),
    FOREIGN KEY(source_event_id) REFERENCES worldops_outbox(event_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_consumer_effect_status
ON worldops_consumer_effect(status, updated_at);

CREATE TABLE IF NOT EXISTS worldops_checkpoint (
    consumer TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    last_event_id TEXT,
    processed_count INTEGER NOT NULL DEFAULT 0,
    watermark TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(consumer, partition_key)
);

CREATE TABLE IF NOT EXISTS worldops_trace (
    trace_id TEXT PRIMARY KEY,
    timeline_id TEXT,
    correlation_id TEXT,
    causation_id TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    bundle_id TEXT,
    event_instance_id TEXT,
    details_json TEXT NOT NULL,
    latency_ms REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_worldops_trace_correlation
ON worldops_trace(correlation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_worldops_trace_created
ON worldops_trace(created_at);

CREATE TABLE IF NOT EXISTS worldops_metric (
    metric_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    labels_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_worldops_metric_name
ON worldops_metric(name, created_at);

CREATE TABLE IF NOT EXISTS worldops_dead_letter (
    dead_letter_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    error_code TEXT NOT NULL,
    error TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_history_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_event_id, source_kind)
);

CREATE INDEX IF NOT EXISTS idx_worldops_dead_letter_status
ON worldops_dead_letter(status, created_at);

CREATE TABLE IF NOT EXISTS worldops_control (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sora_id TEXT NOT NULL,
    ambient_enabled INTEGER NOT NULL DEFAULT 1,
    story_mode INTEGER NOT NULL DEFAULT 0,
    contribution_enabled INTEGER NOT NULL DEFAULT 0,
    attribution_allowed INTEGER NOT NULL DEFAULT 0,
    expression_permission TEXT NOT NULL DEFAULT 'implicit',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, user_id, sora_id)
);

CREATE TABLE IF NOT EXISTS worldops_contribution_receipt (
    proposal_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    receipt_token_hash TEXT NOT NULL,
    withdrawal_token_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_summary TEXT,
    server_revision TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_contribution_receipt_timeline
ON worldops_contribution_receipt(timeline_id, updated_at);

CREATE TABLE IF NOT EXISTS worldops_sync_conflict (
    conflict_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    incoming_snapshot_checksum TEXT NOT NULL,
    common_action_count INTEGER NOT NULL,
    local_action_ids_json TEXT NOT NULL,
    incoming_action_ids_json TEXT NOT NULL,
    conflicting_event_ids_json TEXT NOT NULL,
    incoming_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    resolved_timeline_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE INDEX IF NOT EXISTS idx_worldops_sync_conflict_status
ON worldops_sync_conflict(timeline_id, status, created_at);

CREATE TABLE IF NOT EXISTS worldops_memory_projection (
    source_memory_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);

CREATE TABLE IF NOT EXISTS worldops_forget_tombstone (
    timeline_id TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(timeline_id, ref_hash),
    FOREIGN KEY(timeline_id) REFERENCES worldops_timeline(timeline_id)
);
"""


WORLDOPS_SERVER_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS canon_object_revision (
    object_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(object_id, revision, branch)
);

CREATE TABLE IF NOT EXISTS canon_statement_ledger (
    statement_revision_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    statement_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    supersedes_revision_id TEXT,
    valid_from_release INTEGER NOT NULL,
    valid_to_release INTEGER,
    change_set_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canon_statement_active
ON canon_statement_ledger(world_id, statement_id, valid_to_release);

CREATE TABLE IF NOT EXISTS canon_change_set (
    change_set_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    target_revision INTEGER NOT NULL,
    changes_json TEXT NOT NULL,
    impact_report_json TEXT NOT NULL,
    migration_plan_json TEXT NOT NULL,
    rollback_plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS content_release (
    release_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    release_sequence INTEGER NOT NULL,
    canon_version TEXT NOT NULL,
    channel TEXT NOT NULL,
    cohort TEXT NOT NULL,
    status TEXT NOT NULL,
    baseline_id TEXT,
    approvals_json TEXT NOT NULL,
    migration_plan_json TEXT NOT NULL,
    rollback_plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    revoked_at TEXT,
    UNIQUE(world_id, channel, cohort, release_sequence)
);

CREATE TABLE IF NOT EXISTS content_bundle (
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    release_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(bundle_id, version),
    FOREIGN KEY(release_id) REFERENCES content_release(release_id)
);

CREATE TABLE IF NOT EXISTS narrative_contribution (
    proposal_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    pseudonymous_subject_id TEXT NOT NULL,
    source_event_id TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    privacy_json TEXT NOT NULL,
    risk_json TEXT NOT NULL,
    cluster_key TEXT,
    status TEXT NOT NULL,
    receipt_token_hash TEXT NOT NULL,
    withdrawal_token_hash TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    withdrawn_at TEXT,
    UNIQUE(proposal_id, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_narrative_contribution_review
ON narrative_contribution(status, world_id, submitted_at);

CREATE TABLE IF NOT EXISTS editorial_decision (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    editor_id TEXT NOT NULL,
    target_change_set_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES narrative_contribution(proposal_id)
);

CREATE TABLE IF NOT EXISTS release_audit (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    correlation_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_release_audit_target
ON release_audit(target_type, target_id, created_at);
"""
