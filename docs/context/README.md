# Project Context Governance

This directory coordinates durable context across independent Project Sora tasks. Chat history is not a project source of truth.

## Required Task Lifecycle

Run all commands from the repository root.

```powershell
# 1. Validate and read shared status
python scripts/project_context.py status --check

# 2. Claim the narrowest paths before editing
python scripts/project_context.py begin `
  --id ws-client-runtime-inspector `
  --title "Client Runtime Inspector" `
  --owner "codex:<thread-id>" `
  --path apps/desktop `
  --summary "Connect the accepted Developer Mode UI to real debug contracts."

# 3. Publish material progress
python scripts/project_context.py update `
  --id ws-client-runtime-inspector `
  --summary "Typed snapshots implemented; event stream pending." `
  --changed-path apps/desktop/src `
  --verification "Desktop unit tests pass"

# 4. Seal the workstream before completion
python scripts/project_context.py finish `
  --id ws-client-runtime-inspector `
  --summary "Runtime Inspector uses real typed snapshots and audited commands." `
  --changed-path apps/desktop `
  --verification "Desktop tests pass" `
  --verification "Desktop and compact screenshots reviewed"
```

Use the Codex task/thread ID in `--owner` when available. `begin` rejects overlapping active paths by default. `--allow-overlap` is an explicit coordination exception, not a convenience flag.

## Architecture Decisions

Record material architecture decisions as independent, immutable files:

```powershell
python scripts/project_context.py decision `
  --title "Debug UI uses typed Gateway snapshots" `
  --context "The WebView must not read runtime databases directly." `
  --decision "Route inspector data through Gateway and the Rust capability broker." `
  --consequence "Developer Mode remains isolated from canonical stores." `
  --reference docs/PROJECT_CONTEXT.md `
  --workstream-id ws-client-runtime-inspector
```

Do not edit an accepted decision in place. Supersede it with a new decision that references the earlier ID.

## Concurrency and Enforcement

- One task owns one workstream JSON file; unrelated tasks never append to a shared log.
- Creates are exclusive and updates are atomic.
- Active path overlap is rejected before editing and reported by validation.
- Completed workstreams are sealed and require verification evidence.
- CI rejects governed changes without a completed workstream record.
- Architecture-sensitive changes additionally require `PROJECT_CONTEXT.md` or an ADR update.
- `PROJECT_CONTEXT.md` is the curated snapshot; workstreams and ADRs are the audit trail.

Running tasks must re-run `status --check` before expanding scope. Shared files are immediately visible, but they cannot inject new text into a model turn already in progress.
