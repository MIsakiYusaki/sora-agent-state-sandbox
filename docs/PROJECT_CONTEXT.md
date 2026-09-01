# Project Sora Context

Last refreshed: 2026-08-31

Context revision: 3

This file is the compact, repository-local context for every new Project Sora task. It is intentionally more stable than a conversation summary. Verify volatile implementation details against current code, Git status, tests, and the canonical PRDs before making claims.

The detailed engineering source is `docs/context/PROJECT_LEDGER.md`. It contains the per-system plan, implementation and audit status, runtime paths, unresolved work, release criteria and maintenance policy. Machine-readable indexes live in `docs/context/catalog/`.

Every task must read this compact snapshot. A task that changes, reviews or plans a system must also read the corresponding section in the detailed ledger and its catalog record before making claims.

Current verified repository baseline for the ledger: `a77ea7a` (`Implement model provider and BYOK system`). Later code and workstream records win when this baseline is stale.

## 1. Product Mission

Project Sora is a Windows-first, local-first, text-first AI companion. It is designed for long-term continuity rather than one-shot assistance. The product should feel like an ongoing private relationship with a coherent companion, while keeping user agency, privacy, consent, reversibility, and explicit controls above every inferred state.

The product is for general users. Do not encode the developer's personal examples, vocabulary, relationships, pets, projects, or preferences as product rules.

Current first-release scope:

- text input and text output,
- conversation continuity and history,
- memory and privacy controls,
- model provider and BYOK configuration,
- interruption and conversation preferences,
- recovery and diagnostics,
- install, update, rollback, and release quality.

Explicitly deferred from the text-first release:

- desktop perception,
- camera and visual input,
- voice,
- always-on Live2D/3D presence,
- multimodal behavior inference.

## 2. System Shape

The runtime is a composition of typed systems, not one `memory_runtime` or one monolithic agent:

```text
Desktop WebView
  -> Rust Trusted Host
  -> Conversation Gateway
  -> Conversation Runtime
       -> Turn Interpreter
       -> Interruptibility
       -> Memory
       -> Persona
       -> Affect and Internal State
       -> Relationship
       -> WorldOps Narrative
       -> Initiative and Semantic Arbiter
       -> Response Planning and Expression
       -> Module 10 Eval/Admin/Trace
       -> Model Provider Runtime
```

Canonical ownership matters:

- Memory owns durable user facts, preferences, episodes, evidence, candidates, lifecycle, recall, and surface policy.
- Persona owns Sora's bounded long-term traits and growth contributions.
- Affect owns short-lived internal state and regulated decay.
- Relationship owns user-specific relational continuity and repair state.
- WorldOps owns narrative content packs, events, tasks, choices, and projection contracts.
- Interruptibility owns whether and how the system may respond, expand, follow up, shift topic, or initiate.
- Model Provider owns provider profiles, credentials, capability probes, workload routing, fallbacks, and invocation records.
- Conversation Gateway owns the local identity-scoped API, streaming, idempotency, recovery, cancellation, and client delivery.
- Conversation Runtime coordinates a turn but does not absorb subsystem ownership.
- Module 10 owns unified evaluation, release gates, observability, safe admin actions, and audit.

## 3. Implemented Platform Baseline

The repository contains implemented and tested systems for:

- the ten-module memory architecture,
- LLM extraction and four-operation memory decisions,
- Gate, quarantine, canonical commit, rollback, replay, and governance,
- lexical/vector/temporal retrieval, RRF fusion, reranking, context packing, and traces,
- recall planning and surface policy,
- lifecycle heat, consolidation, archival, forgetting, and tombstones,
- external projection contracts,
- Module 10 evaluation, observability, admin, and release gates,
- text-first Interruptibility,
- Persona,
- Affect and Internal State,
- Relationship,
- WorldOps Narrative,
- Text Conversation Runtime and E2E contracts,
- Live Text Conversation Gateway,
- Model Provider/BYOK with OS credential storage and workload routing,
- a Tauri/React production desktop baseline.

Do not assume that "implemented" means every user-facing desktop control is wired. Domain backends and desktop surfaces have different completion states.

## 4. LLM Policy

Runtime paths use real configured providers. DeepSeek is the current local development provider, but the architecture is provider-neutral.

- Turn Interpreter and Semantic Arbiter have thinking disabled for latency.
- Do not silently add arbitrary timeouts or token ceilings unless the relevant design is updated.
- Use fast deterministic/rule paths when the answer is known from explicit controls or typed state.
- Use bounded LLM resolution only for genuinely ambiguous semantic work.
- LLM output is a proposal, not canonical truth.
- Fake providers are test fixtures only and must never leak into normal runtime configuration.

The model catalog and UI must not be hardcoded to only `deepseek-chat`. Current intended DeepSeek choices include V4 Flash, V4 Pro, Chat, and supported custom compatible endpoints. Capability probing, not display text alone, determines workload eligibility.

## 5. Memory Position

Memory is asynchronous with respect to the response hot path. The user turn may be recorded as an episode/evidence event, while extraction, candidate materialization, Gate review, consolidation, indexing, and projection proceed through explicit jobs and outbox contracts.

The memory system is not "remember everything":

- raw dialogue may be retained according to policy,
- durable memory requires evidence and governance,
- uncertainty and contradiction remain visible,
- explicit user forget outranks lifecycle policy,
- sensitive content and secrets fail closed,
- ordinary recall has token budgets and surface permissions,
- archived memory requires explicit deep-recall paths.

The user may ask Sora to remember words and shared experiences. Shared episodes are stored only when there is user participation and sufficient evidence; WorldOps content that the user did not participate in must not be written as a shared experience.

## 6. Companion-System Boundaries

Persona, Affect, Relationship, and WorldOps are independent canonical systems connected through typed projection/runtime contracts.

- Persona changes are bounded, inertial, reversible, and evidence-backed. LLMs do not directly edit traits.
- Affect is short-lived and must not become a hidden long-term personality store.
- Relationship is multidimensional and user-specific; it must not be flattened into a single affection score.
- WorldOps content is authored by the team, distributed from the server, and may include worlds, events, factions, characters, tasks, and completion conditions.
- User choices may be interpreted by an LLM and submitted anonymously by default for writer review. Shared experiences enter the main world only after server-side editorial governance.

`love`, `dependency`, `loyalty`, `possessiveness`, attachment pressure, and similar constructs may be understood as natural-language topics. They may not be optimization targets or features that increase interruption, initiative, persuasion, retention pressure, or boundary crossing.

## 7. Desktop Client: Two Sources, One Product

### Production runtime source

`apps/desktop/`

This is the production React application. It connects to real Tauri Host and Gateway contracts. Production implementation, tests, packaging, and release work happen here.

### Executable experience specification

`apps/desktop-design-prototype/`

This is the accepted interactive UI/UX specification. It contains the full target pages, states, responsive behavior, content, and Developer Mode. It is not a competing client and must not receive production-only business logic.

The frozen visual direction is **Quiet Companion Desk / Sora's quiet conversation desk**:

- Ink `#1E2727`
- Porcelain `#FAFCFB`
- Mist `#EDF1F0`
- Jade `#1F7B70`
- Cold Blue `#3679C8`
- Quiet Violet `#7B66B8`
- Warning Coral `#B84D52`
- Segoe UI Variable Text / Microsoft YaHei UI / PingFang SC
- Cascadia Code / Consolas for technical identifiers
- no gradients, generic AI purple, decorative blobs, glassmorphism, or oversized rounded cards
- no permanent large Sora artwork column
- Sora artwork is limited to avatar, First Run, empty-state presence, About, installer, and explicit companion views

Permanent rail:

1. Conversation
2. History
3. Memory and Privacy
4. Settings

Conversation controls are contextual and user-facing:

- `短一点 / 自然 / 认真聊聊`
- `别追问 / 可以问`
- `跟着我 / 可以延伸`
- `自然接上 / 可以明确提起 / 这次不用`
- `先别说话 / 今晚不主动提醒`

Normal UI may show what Sora will do, which user-confirmed past facts were used, what was deliberately not written to memory, and actions such as hide/forget/export. It must not expose policy matrices, internal state scores, relationship optimization metrics, schema versions, queues, or traces.

Developer Mode:

- hidden and disabled by default,
- enabled only from Advanced Settings,
- Chinese-first,
- attached to the same profile, conversation, turn, and correlation context,
- includes turn timeline, context frames, model routes, memory pipeline, async work, state systems, eval/release, and controlled commands,
- forbids arbitrary shell, SQL, Python, eval, filesystem access, and raw secrets.

## 8. Desktop Status at This Refresh

The production app already has a Tauri/React baseline, streaming conversation state, cancellation, history restoration, provider settings, native credential entry, Gateway sidecar supervision, typed IPC, and packaging work.

The design prototype has passed a production build and local visual/interaction review across 18 scenarios, including light, dark, compact, empty, offline, provider-missing, recovering, version conflict, update-ready, safe mode, settings, memory, history, and Developer Mode. Eight core interaction smoke checks passed.

This does not mean the desktop product is released. The main remaining client work is:

1. migrate the accepted prototype components, tokens, content, and state behavior into `apps/desktop/`,
2. connect every page to real Host/Gateway/Runtime contracts,
3. complete history search/pagination/branching and per-conversation drafts,
4. complete user-facing memory/privacy and companion-system controls,
5. implement real Developer Mode snapshot/event/capability/typed-command plumbing,
6. verify production accessibility, target-hardware performance, signing, updates, N-1 migration, rollback, SBOM, canary, and Beta gates.

Do not claim that the desktop is "not started". Do not claim that the accepted prototype is a production-complete client.

## 9. Important Paths

- `README.md`: runnable repository overview and REPL commands
- `SUBSYSTEM_API.md`: subsystem adapter and external integration contracts
- `docs/TEXT_CONVERSATION_RUNTIME.md`: runtime contract and behavior
- `docs/CONVERSATION_GATEWAY.md`: gateway contract and operations
- `docs/worldops-runbook.md`: WorldOps operation guide
- `apps/desktop/`: production desktop app
- `apps/desktop/src-tauri/`: trusted host
- `apps/desktop-design-prototype/`: accepted UX specification
- `apps/desktop-design-prototype/DESIGN_SYSTEM.md`: frozen design rules
- `contracts/`: cross-language contract artifacts
- `src/sora_sandbox/model_provider/`: provider runtime and BYOK
- `src/sora_sandbox/conversation_runtime/`: turn runtime
- `src/sora_sandbox/conversation_gateway/`: local gateway
- `tests/`: Python system and contract tests
- `evals/`: subsystem and release evaluation assets

## 10. Canonical PRDs

- Overall architecture: https://qcnqo2xrtj31.feishu.cn/docx/Ax9FdxrV5oNHqlxW7O4cEebln3e
- Desktop Shell and Text UX: https://qcnqo2xrtj31.feishu.cn/wiki/RrFkwF6p9iqtIJkaa9fcELABnld
- Memory architecture: https://qcnqo2xrtj31.feishu.cn/docx/O8uDdAggsofRM2xrB5TcuoOhnUu

The Desktop PRD was refreshed to revision 133 on 2026-08-30. A later revision wins. If implementation and PRD differ, identify whether the difference is an intentional implementation advance, an unfinished migration, or a regression before editing.

## 11. Working Protocol for a New Task

Before changing code:

1. Read this file and repository `AGENTS.md`.
2. Run `python scripts/project_context.py status --check`.
3. Inspect `git status` and recent relevant changes. Multiple tasks may share the same checkout.
4. Register a workstream with narrow path ownership before editing.
5. Read the relevant canonical PRD section and local subsystem docs.
6. For desktop work, compare `apps/desktop/` against the accepted prototype rather than inventing a new layout.
7. State the exact production gap being closed.
8. Implement in the owning module and preserve typed boundaries.
9. Record material decisions as immutable ADR files.
10. Run focused tests, then the broader gates required by blast radius.
11. Render and inspect screenshots for any visual change.
12. Finish the workstream with changed paths and verification evidence.
13. Update this context file when the architecture, status, or source-of-truth path materially changes.

When context is uncertain, stop and read the repository or PRD. Do not fill gaps with generic assistant-product assumptions.

## 12. Multi-Task Context Governance

Project Sora uses repository-native coordination rather than shared private chat history:

- `docs/context/context-manifest.json` defines governed paths and context impact rules.
- `docs/context/workstreams/<id>.json` gives each task an independent, atomically updated status record.
- active workstreams claim repository paths; overlapping claims are rejected by default.
- `docs/context/decisions/<id>.json` stores immutable architecture decisions.
- `docs/PROJECT_CONTEXT.md` remains the curated current snapshot.
- `scripts/project_context.py` implements begin, update, finish, decision, validation, status, and Git impact checks.
- `.github/workflows/context-governance.yml` rejects governed changes without a completed workstream and rejects architecture-sensitive changes without an ADR or snapshot update.

This mechanism makes context updates visible to every task through the shared repository while avoiding concurrent writes to one monolithic log. Running tasks still need to re-run `status --check` before changing scope; file visibility does not inject new text into an already executing model turn.

## 13. Detailed Project Ledger

The compact snapshot is not intended to contain the full project history. The maintained engineering record is split by purpose:

- `docs/context/PROJECT_LEDGER.md`: detailed product contract, architecture, per-system delivery status, audits, runtime paths, release evidence, risks and priorities.
- `docs/context/catalog/systems.json`: system ownership, maturity, code, tests, design and remaining work.
- `docs/context/catalog/artifacts.json`: canonical Feishu/local documents, audits, superseded references and verification status.
- `docs/context/catalog/milestones.json`: Git-verified delivery history.
- `docs/context/catalog/open-items.json`: prioritized unfinished, uncertain and explicitly deferred work.

The ledger aims for complete engineering continuity, not verbatim chat retention. The following must be preserved:

1. all effective product and architecture decisions,
2. all active or planned systems and their ownership boundaries,
3. implementation, integration, audit and release status,
4. canonical documents and code/test evidence,
5. known gaps, deferred work and next priorities,
6. the reason when a previous design is superseded.

Temporary brainstorming, duplicated discussion, secrets and raw private conversations are not project context. A conversation-only decision must be promoted into a PRD, ADR, ledger entry or workstream before another task can rely on it.
