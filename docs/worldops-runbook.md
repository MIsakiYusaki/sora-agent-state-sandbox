# WorldOps Production Runbook

## Scope

This runbook covers the signed canon service, client personal timeline, outbox consumers, device sync, contribution capabilities, and quality gates.

## Critical Alerts

### sqlite_integrity

1. Stop the WorldOps outbox worker.
2. Export the timeline, trace, dead-letter, and active signed manifest metadata.
3. Restore the database from the last verified backup into a separate path.
4. Run `rebuild(dry_run=True)` and compare the action-set digest before activation.
5. Never overwrite the damaged database until the export is retained.

### rebuild_parity

1. Freeze timeline mutations and retain the current database.
2. Export transition, clock, choice, and task-evidence ledgers.
3. Identify the first revision mismatch.
4. Repair through a reviewed migration; do not directly rewrite canonical state rows.
5. Re-run the WorldOps eval suite and the full Module 10 release gate.

## Warning Alerts

### open_dead_letters_zero

Inspect the source envelope and attempt history. Requeue only retryable downstream failures. Contract, authorization, and privacy failures require review or discard.

### incomplete_consumer_effects_zero

Check whether the downstream Memory or contribution operation committed before the response was lost. Consumer-effect idempotency keys must be checked before replay.

### unresolved_sync_conflicts_zero

Present the conflict to the user. Allowed resolutions are `keep_local` and `fork_local`; WorldOps must not silently choose a branch.

### write_queue_below_high_watermark

Pause low-priority maintenance, inspect slow writes and outbox consumers, then scale the service or migrate the state store if sustained contention continues.

## Deployment Requirements

- Terminate TLS at a managed ingress and enforce authenticated transport for device sync.
- Keep bundle download public only when the deployment intends public distribution; signatures remain mandatory.
- Protect anonymous contribution endpoints with ingress rate limiting and abuse controls.
- Store signing keys in KMS/HSM. Clients receive public trust material only.
- On non-Windows clients, inject an OS keyring or managed portable key provider for capability secrets.
- Export structured logs and Prometheus metrics to the deployment observability stack.
