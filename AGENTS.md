# Project Sora Repository Instructions

## Mandatory Context Load

Before analyzing, planning, or editing this repository, read:

1. `docs/PROJECT_CONTEXT.md`
2. `docs/context/PROJECT_LEDGER.md` and the relevant record in `docs/context/catalog/systems.json` for any system planning, review, implementation, audit, or status claim
3. `README.md`
4. The subsystem document and canonical artifact relevant to the task; artifact IDs and URLs are in `docs/context/catalog/artifacts.json`
5. For desktop work, `apps/desktop-design-prototype/DESIGN_SYSTEM.md`

Then run `python scripts/project_context.py status --check`, inspect `git status`, and read the code that already implements the requested path. Do not infer the repository state from a task title or from an older conversation summary.

Before editing governed paths, register a unique workstream with `python scripts/project_context.py begin`. Use the Codex task/thread ID in `--owner` when available and claim the narrowest accurate paths. Overlap with an active workstream is a coordination failure unless explicitly reviewed.

Before declaring the task complete, record material decisions, finish the workstream with verification evidence, and update `docs/PROJECT_CONTEXT.md` when the curated project snapshot changed. A governed code change without a completed workstream record is incomplete.

## Product Identity

Project Sora is a long-term AI companion product, not a generic assistant dashboard. The system may model companionship, shared history, relationship continuity, persona growth, affect, and narrative participation, but user agency and explicit controls remain authoritative.

Never optimize interruption or initiative using `love`, `dependency`, `loyalty`, `possessiveness`, or similar pressure-producing metrics. Never hardcode rules for one developer or one user; all product behavior must generalize to end users.

## Engineering Boundaries

- Preserve canonical ownership. Memory, Persona, Affect, Relationship, WorldOps, Interruptibility, Model Provider, Conversation Runtime, and Conversation Gateway are separate systems with typed contracts.
- LLMs may propose or interpret where the design permits, but deterministic services own validation, bounded updates, governance, commits, rollback, and rebuild.
- Use real configured model providers in runtime paths. Fake providers are allowed only in explicit unit, fixture, golden-set, or failure-injection tests.
- Do not bypass Gate, policy, consent, idempotency, audit, or rollback paths for convenience.
- Secrets belong only in the OS credential store. Never put API keys in source, SQLite, WebView state, logs, command arguments, screenshots, fixtures, or exported diagnostics.
- Work with existing dirty changes. Never reset, overwrite, or remove unrelated work.

## Desktop Client Boundary

- Production desktop application: `apps/desktop/`
- Production trusted host: `apps/desktop/src-tauri/`
- Executable UI/UX specification: `apps/desktop-design-prototype/`
- Frozen design system: `apps/desktop-design-prototype/DESIGN_SYSTEM.md`
- Screenshot and interaction evidence: `outputs/desktop-design-prototype/`

The prototype is not a second product and must not become the runtime source of truth. It defines the target information architecture, visual language, content, responsive behavior, and states. Production work migrates those decisions into `apps/desktop/` and connects them to real Host/Gateway/Runtime contracts.

Do not redesign the client from scratch, introduce a permanent character-poster column, expose internal policy fields to ordinary users, or build a generic model dashboard. The frozen direction is **Quiet Companion Desk**.

Permanent navigation is:

1. Conversation
2. History
3. Memory and Privacy
4. Settings

Conversation preferences open contextually from the current conversation. Developer Mode is hidden by default and appears only after explicit enablement in Advanced Settings.

## Frontend Workflow

For UI work, follow this order:

1. `frontend-design`: confirm the existing Quiet Companion Desk direction; do not restart art direction.
2. `ui-ux-pro-max`: apply tokens, component rules, responsive behavior, and accessibility.
3. `design-taste-frontend`: audit AI-slop, density, motion, and unnecessary decoration without turning the product into a landing page.
4. Implement, render screenshots, inspect interaction states, and polish.

Normal UI uses user language. Internal frames, scores, schema versions, queue state, traces, token/cost detail, and pipeline stages belong only in Developer Mode. Developer Mode is Chinese-first and may retain necessary identifiers as secondary diagnostic text.

## Verification

Run focused tests while iterating and broaden verification for shared contracts or runtime paths. Desktop visual changes require screenshot review at minimum for 1440x900 light, 1440x900 dark, and 420x560 compact, plus affected error and Developer Mode states. Check console errors, horizontal overflow, focus behavior, keyboard access, contrast, reduced motion, and asset loading.

Do not claim production completion from a static prototype. Distinguish:

- design/prototype acceptance,
- production UI migration,
- real API integration,
- release hardening and Beta readiness.

## Canonical Documents

- Overall architecture: `https://qcnqo2xrtj31.feishu.cn/docx/Ax9FdxrV5oNHqlxW7O4cEebln3e`
- Desktop Shell and Text UX PRD: `https://qcnqo2xrtj31.feishu.cn/wiki/RrFkwF6p9iqtIJkaa9fcELABnld`
- Memory architecture: `https://qcnqo2xrtj31.feishu.cn/docx/O8uDdAggsofRM2xrB5TcuoOhnUu`

If a user says a Feishu document was updated, read the current revision before implementing. For Feishu CLI on this Windows host, use the desktop user security context because credentials are DPAPI-bound. Do not repeatedly request authorization when desktop-context verification succeeds. If authorization is genuinely required, provide only the direct authorization link, never a QR code.

## Context Maintenance

The full lifecycle is defined in `docs/context/README.md`. Workstream records provide concurrent task status, immutable decision records provide ADR history, `docs/PROJECT_CONTEXT.md` is the curated snapshot, and CI enforces their relationship to implementation changes.

`docs/context/PROJECT_LEDGER.md` is the detailed project record. `docs/context/catalog/` is the machine-readable index of systems, artifacts, Git milestones, and open work. The compact snapshot contains only facts every task needs; system-level plans and progress belong in the ledger and catalogs.

When an architectural decision, implementation status, canonical path, audit disposition, release state, or priority materially changes, update the appropriate snapshot, ledger, catalog, ADR, and workstream records in the same change. A future task must be able to recover the project state from the repository without relying on private conversation history.
