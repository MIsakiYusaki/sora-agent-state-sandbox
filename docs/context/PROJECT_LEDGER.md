# Project Sora Engineering Ledger

Last refreshed: 2026-08-31
Ledger revision: 1
Verified Git baseline: `a77ea7a` (`Implement model provider and BYOK system`)
Runtime scope: Windows-first, local-first, text-first companion

## 1. Purpose and Authority

This is the detailed engineering ledger for Project Sora. It exists because a compact context snapshot cannot carry the full set of plans, boundaries, delivered systems, audit history, current gaps and next actions accumulated across many tasks.

The authority order is:

1. explicit user decision and current canonical PRD,
2. current repository code, schema, tests and release artifacts,
3. accepted ADRs and this ledger,
4. completed workstream records and Git history,
5. audit reports,
6. conversation history.

An audit report describes the state at the time of audit. A later fix may make a finding historical. A conversation proposal is not canonical until it is reflected in a PRD, ADR, ledger entry or implementation.

This ledger records effective engineering facts. It deliberately does not copy every chat sentence. Rejected options are retained only when they explain an important boundary or future migration.

## 2. Product Contract

Project Sora is not a generic productivity assistant. It is a long-term AI companion designed to preserve continuity across conversations and shared experiences while keeping the user in control.

### 2.1 Current release objective

Run the complete text-input to text-output path in a Windows desktop client:

```text
User text
  -> trusted desktop host
  -> identity-scoped local gateway
  -> conversation runtime
  -> fast interpretation and explicit controls
  -> memory/companion context assembly
  -> model routing and response generation
  -> streaming desktop response
  -> atomic turn commit
  -> asynchronous memory and subsystem work
```

The release must provide conversation history, memory/privacy controls, BYOK/provider configuration, interruption controls, recovery, diagnostics, packaging, updates and rollback.

### 2.2 Companion behavior contract

- Sora may remember user-confirmed facts, preferences and shared experiences through governed memory.
- Sora may develop bounded persona continuity, short-lived affect and user-specific relationship continuity through separate canonical systems.
- Sora may participate in authored WorldOps narratives, but unexperienced authored content is not a shared memory.
- User choices and explicit controls outrank inferred state.
- Unknown is a valid state; the system must not pretend to know whether the user is busy, lonely, attached or expecting initiative.
- `love`, `dependency`, `loyalty`, `possessiveness` and related constructs may be understood in language but are not optimization targets.
- Relationship strength, retention pressure or affect must never silently increase interruption, persuasion or boundary crossing.

### 2.3 Product-general rules

The product is for arbitrary users. Rules must not hardcode the developer's own examples, projects, pets, vocabulary, schedules or relationships. Domain-specific behavior belongs in configuration, authored content, learned evidence or user controls.

## 3. Architecture Ownership

### 3.1 Runtime topology

```text
React Desktop WebView
  -> Tauri Rust Trusted Host
       -> capability broker
       -> native credential prompt
       -> sidecar supervision
       -> typed IPC
  -> Live Text Conversation Gateway
       -> identity and profile binding
       -> idempotency and conflict detection
       -> REST/WebSocket event delivery
       -> resume, cancel and recovery
  -> Text Conversation Runtime
       -> Turn Interpreter
       -> Interruptibility
       -> Memory recall plan/retrieval/surface
       -> Persona Frame
       -> Affect Frame
       -> Relationship Frame
       -> WorldOps Frame
       -> Semantic Arbiter and response planning
       -> Model Provider broker
       -> atomic journal commit and post-turn outbox
```

### 3.2 Canonical ownership matrix

| Domain | Owns | Must not own |
| --- | --- | --- |
| Memory | durable user facts, preferences, episodes, evidence, recall and forgetting | Sora personality, relationship truth, narrative canon |
| Persona | bounded Sora-global trait trajectory and expression bias | per-user relationship, short-lived emotion |
| Affect | short-lived internal state, regulation and decay | permanent identity or hidden relationship score |
| Relationship | per-Sora/per-user relational continuity, agreements and repair | response permission or retention optimization |
| WorldOps | authored worlds, events, tasks, choices, personal narrative timelines and editorial contribution flow | arbitrary memory writes or unreviewed main-canon publication |
| Interruptibility | response/expansion/follow-up/topic-shift/initiative/surface permissions | attachment inference or relationship optimization |
| Model Provider | profile, secret reference, capability, route, fallback and invocation metadata | domain truth or user-facing product policy |
| Conversation Runtime | turn orchestration and atomic outcome | subsystem canonical state |
| Gateway | local identity, delivery, streaming, recovery and cancellation | response semantics or domain databases |
| Module 10 | evaluation, trace correlation, safe admin and release governance | bypassing owning stores or policies |

### 3.3 Cross-system mutation rule

Systems exchange typed evidence, projections, frames and outbox events. They do not write each other's database directly. Forget, rollback and rebuild must propagate through typed contracts and remain auditable.

## 4. Hot Path and Asynchronous Path

### 4.1 Hot path

The hot path is responsible only for producing a safe response and an atomic turn outcome.

1. Gateway authenticates local identity and validates idempotency.
2. Runtime opens or resumes a turn journal entry.
3. Explicit user controls are parsed before model assistance.
4. Turn Interpreter resolves bounded semantic fields; thinking is disabled.
5. Interruptibility produces orthogonal permissions.
6. Recall Planner decides whether and how memory should be searched.
7. Hybrid retrieval and Surface Policy produce a budgeted, governed memory context.
8. Persona, Affect, Relationship and WorldOps provide typed frames.
9. Context packer reserves response capacity and excludes forbidden features.
10. Model Provider selects an eligible route by capability, policy and availability.
11. Response streams through the Gateway and may be cancelled.
12. Runtime atomically commits user input, response result, trace references and post-turn outbox.

Fast deterministic behavior is preferred for explicit controls and known state. An LLM is used only where semantic ambiguity requires it.

### 4.2 Asynchronous path

After the visible response is committed:

1. episode and evidence events are sealed,
2. extraction jobs claim eligible episodes,
3. the LLM Extractor proposes ADD/UPDATE/MERGE/NOOP,
4. candidates enter deterministic Gate and possibly quarantine/review/defer,
5. accepted candidates commit transactionally to canonical memory,
6. outboxes update FTS/vector/graph-lite indexes and projection gateways,
7. lifecycle jobs update heat, consolidate, archive and apply tombstones,
8. Persona/Affect/Relationship/WorldOps consume typed events through their own coordinators,
9. traces, metrics, evaluation and release evidence are retained according to policy.

No asynchronous result may rewrite an already-delivered response. Late results apply forward only.

## 5. Memory System Ledger

The ten memory modules are implemented as an engineering baseline. This means their main schemas, services, failure paths and focused tests exist. It does not mean every production deployment or desktop operation is complete.

### 5.1 Module 01: Episode and Evidence

**Purpose:** preserve the factual origin of potential memories before deciding what deserves durable storage.

**Implemented:** conversation event ingestion, episode windows, evidence spans, source references, hashes, visibility/deletion metadata, extraction jobs, idle/window sealing, low-signal prefiltering and deletion propagation.

**Key paths:** `memory_episode_ingestion.py`, `memory_store.py`, `memory_schema.py`.

**Design:** `K5Gvd02F8o00rexLWCzcy0Z2nMa`.

**Still needed:** production retention calibration; desktop evidence export/deletion experience; large-history storage benchmark.

### 5.2 Module 02: LLM Extractor and Four Operations

**Purpose:** turn episodes into evidence-backed candidate operations without blocking the response path.

**Implemented:** provider protocol, real configured LLM path, thinking control, structured schema, evidence span validation, JSON repair and second-pass repair, cost/usage traces, related-memory targeting, async claim/retry/completion, backfill and golden evaluation. Fake providers remain test fixtures only.

**Operations:** ADD, UPDATE, MERGE and NOOP. Novelty/conflict can use deterministic similarity; confidence and importance may use bounded model judgment.

**Key paths:** `memory_extraction.py`, `memory_extractor_provider.py`, `memory_extractor_repair.py`, `memory_extractor_worker.py`, `memory_write_decision.py`.

**Design:** `XBxPdktWaoUJI7xZMBqcloOwnTh`.
**Audit:** `BLeAdZfwzoM9kdxsZFUcRf7Vnce`.

**Still needed:** real-user multilingual calibration, continuous hallucinated-memory negatives, cross-episode gradual-preference evaluation.

### 5.3 Module 03: Gate and Governance

**Purpose:** decide whether a candidate may become canonical memory.

**Implemented:** materialize-then-gate boundary, deterministic eligibility, independent quarantine table, review queue, defer and expiry, idempotent candidate hash, conflict routing, sensitivity handling, auto-commit constraints, rollback compatibility and audit traces.

**Key paths:** `memory_gate.py`, `memory_gate_engine.py`, `memory_gate_pipeline.py`, `memory_gate_schema.py`.

**Design:** `XlF7doxxWow4ClxbB9XcCkpVnWd`.
**Audit:** `VhB2dTbFZozsnqxFqSIcKrM3nBg`.

**Still needed:** reviewer operating SLA, production threshold calibration, review queue desktop/admin surface.

### 5.4 Module 04: Canonical Store and Eventing

**Purpose:** provide the durable source of truth and reliable change propagation.

**Implemented:** canonical records and revisions, evidence links, transaction commit, soft delete, rollback for ADD/UPDATE, replay, FTS synchronization, index/projector outboxes, retry/dead-letter contracts, audits and rebuild support.

**Key paths:** `memory_canonical_store.py`, `memory_index_outbox.py`, `memory_projector.py`.

**Design:** `ZXEXdtB1Vo0KxHx8gPLc4zrfn8M`.
**Audit:** `XgiVdtTkRoyqS4xb0xbcIW1anTg`.

**Still needed:** volume/concurrency benchmark, backup/restore exercise and production recovery objectives.

### 5.5 Module 05: Hybrid Retrieval and Context Packing

**Purpose:** recover the right memories, combine multiple memories and fit governed information inside a response budget.

**Implemented:** IndexDocument versioning, FTS, embedding/vector routes, temporal ranking, graph-lite expansion, RRF fusion, optional cross-encoder and LLM rerank, Qdrant adapter tests, at-least-once idempotent outbox consumer, rebuild/watermark/dead letter, context packing, trace and multi-memory evaluation.

**Key paths:** `memory_hybrid_retrieval.py`, `memory_vector_index.py`, `memory_reranker.py`, `memory_context_packer.py`, `memory_trace.py`.

**Design:** `U588ddMFpoj5fmxgDH5cEYjMnqh`.
**Audit:** `LQW7dNtQqoch3uxtv0ZcfD0RnYb`.

**Still needed:** deploy and benchmark a real production vector service; calibrate rerank cost/latency/quality; integrate trace cleanup with Module 08 as intentionally deferred.

### 5.6 Module 06: Recall Planner

**Purpose:** decide before retrieval what the turn needs instead of blindly searching everything.

**Implemented:** typed plans, memory need classification, subqueries, source selection, temporal scope, token and item budgets, deterministic fast path, bounded provider path, prefetch cache, trace and golden evaluation. Interpreter and Arbiter thinking are disabled for latency.

**Key paths:** `memory_recall_planner.py`, `memory_recall_planner_eval.py`.

**Design:** `AJNodsUmeov55oxDvYace4IQnkb`.
**Audits:** `NnAhdYdzKo94urxg21cc8ixonod`, `KabddXMkroubHgxLsZocQkPQnt0`.

**Still needed:** latency SLO on target hardware and calibration on real query distributions.

### 5.7 Module 07: Surface Policy and Token Budget

**Purpose:** separate “memory may influence the answer” from “Sora may explicitly mention the memory.”

**Implemented:** nine-action taxonomy, disclosure permission, consent flow, user hide/forget controls, cooldown, compression provider with fallback, deterministic priority, relationship-aware but boundary-preserving policy, typed Context Frame, traces and evaluation.

**Key paths:** `memory_surface.py`, `memory_surface_policy.py`, `memory_context_packer.py`.

**Design:** `T5uddHGYboXxsBxFrlOczQtWnxc`.
**Audit:** `FBNrdCXRHoCyLoxKdMDcls64nDb`.

**Still needed:** user-facing explanation wording and multilingual compression-fidelity calibration.

### 5.8 Module 08: Lifecycle, Consolidation and Forgetting

**Purpose:** keep useful memories available without treating storage growth as permanent truth.

**Implemented:** hot/warm/cold/archived tiers, typed half-life, access and usefulness signals, negative-feedback demotion, ANN/coarse cluster planning, complementary merge versus contradiction supersede, archived deep recall, forget priority, tombstones, compaction and deterministic evaluation.

**Key paths:** `memory_lifecycle.py`, `memory_retention.py`, `memory_lifecycle_eval.py`.

**Design:** `BJTSdsRMSoL05qxjUjLczuSnnlh`.
**Audit:** `Qf3VdE61Co8oQHxDAAsciNCJnXc`.

**Still needed:** production distribution calibration and recall-trace retention linkage.

### 5.9 Module 09: Projection Gateway

**Purpose:** deliver memory-derived events to Persona, Relationship and WorldOps without owning those systems.

**Implemented:** typed projection event, projector routing, idempotency keys, revisions, retry/dead letter, contribution/snapshot lifecycle, rebuild, rollback and forget propagation. The external canonical systems now have their own implementations.

**Key path:** `memory_projector.py`.

**Design:** `MDpsdtnQpoVYQ9xqUwDcbYyZnzP`.
**Audit:** `WIZhdHdeAonzxGx2ARccisQ6n8E`.

**Still needed:** validate independently deployed service transport and operational recovery.

### 5.10 Module 10: Quality, Observability and Admin

**Purpose:** make quality and high-risk operations measurable, replayable and releasable.

**Implemented:** eval registry/adapters, scenario replay, correlation/index concepts, metrics/SLO, safe Admin Action registry, confirmation/cooldown, dry-run, baseline and release gate, quality export and security regression coverage.

**Key paths:** `memory_quality_ops.py`, `memory_observability.py`.

**Design:** `AJfEdFEEEoKUQRxYq5ycAVU8nOX`.
**Audit:** `XAj8dpccWozbI6xNrFjcPTdJn9e`.

**Still needed:** production telemetry store/retention, CI baseline promotion and a complete operator workflow.

## 6. Companion Systems Ledger

### 6.1 Interruptibility

Interruptibility uses an orthogonal frame instead of one vague score. It decides response obligation, expansion, follow-up, topic shift, initiative and state-surface permission independently.

Explicit stop/quiet controls are hard overrides. Persistent forbidden behavior requires explicit user confirmation and a reachable revoke path. Activity-based initiative leases renew on actual user activity. Relationship and attachment-like features are filtered before policy evaluation.

**Implementation:** `interruptibility.py`, `interruptibility_contracts.py`.
**Tests/eval:** `test_interruptibility.py`, `evals/interruptibility`.
**Design:** `Tn2td1lJqoXujOx8xgscnayunKc`.

**Desktop contract:** show natural controls such as “短一点”, “别追问”, “跟着我”, “先别说话”; do not expose the internal permission matrix to normal users.

### 6.2 Persona

Persona is an independent canonical system for Sora-global, bounded long-term traits. It is not a memory projection table and not a per-user relationship score.

LLMs may propose evidence interpretations but never directly calculate or write trait deltas. Contributions pass deterministic support, bounds, inertia and 24h/7d/30d trajectory budgets. Rollback deactivates contributions and rebuilds state. Per-user adaptation does not silently promote into global Persona.

**Implementation:** `src/sora_sandbox/persona/`.
**Tests/eval:** `test_persona_system.py`, `evals/persona`.
**Current design:** `FYKCdChDqoG9r3xYvNlcpjcgnFb`.
**Audit:** `FXqYdXNPFoCgeDxL9ULc7pp3nmc`.

**Remaining:** real-usage trajectory calibration; user-facing growth pause/explanation; production expression integration across brief/deep response modes.

### 6.3 Affect and Internal State

Affect owns short-lived Sora state. It is agent-global with isolated target contexts where required, uses deterministic dynamics and decay, and must not become a hidden permanent personality or relationship store.

The hot path is deterministic and idempotent. Semantic assistance is asynchronous and forward-only. Long offline periods do not produce absence punishment. User distress does not create unbounded mirrored Sora distress. Interruptibility may suppress expression without stopping canonical regulation.

**Implementation:** `src/sora_sandbox/affect/`.
**Tests/eval:** `test_affect_system.py`, `evals/affect`.
**Design:** `OJ4Ada3VSoolFOxspXpcRdrJnte`.
**Audits:** `VOZIdjiHsoaD1kxo5flcLwgCn2e`, `O52idpo2HoZ4gkxR9RScevcunXb`.

**Remaining:** Beta calibration, long-horizon non-escalation monitoring and target-hardware latency measurement.

### 6.4 Relationship

Relationship owns per-Sora/per-user continuity. It models multiple dimensions, agreements, reciprocity style, boundary preferences and repair state rather than one affection number.

The system isolates actor scope, rejects forged evidence, uses explicit confirmation for agreements, supports repair FSM, rebuild, rollback, forget and encrypted export/restore with owner/Sora validation. Relationship values cannot be coupled to response permissions or initiative.

**Implementation:** `src/sora_sandbox/relationship/`.
**Tests/eval:** `test_relationship_system.py`, `evals/relationship`.
**Design:** `D0bfd7aYaoBlY7x1l0Hc3sCCng7`.
**Audits:** `HOPudrP1xojbU4xXFescFdKbnMc`, `CaZIdiaHXoUDyPxm7oScEg47n6k`.

**Remaining:** Beta calibration, desktop agreement/repair/privacy UX and production operator workflows.

### 6.5 WorldOps Narrative

WorldOps is a canonical authored narrative platform, not a collection of prompt snippets.

Writers author worlds, events, factions, characters, tasks and completion predicates on the service side. Signed bundles are distributed to clients. The client evaluates only authored transitions and records a personal timeline. LLM interpretation is bounded to known authored IDs and cannot invent canon. User choices may be submitted anonymously by default as minimized contribution proposals; publication into main canon always requires editorial review.

**Implementation:** compiler, schema registry, signed bundle transport, server store, content studio, client timeline, deterministic predicate/effect engine, task FSM, device sync/conflict, contribution review, outbox, rebuild, quality and observability under `src/sora_sandbox/worldops/`.

**Tests/eval:** `test_worldops_system.py`, `evals/worldops`.
**Operational guide:** `docs/worldops-runbook.md`.
**Early reference:** `VcJcd4zO3oqsaVxHOn3cehUAn9f`.
**Implementation audit:** `BcjTdXVXvoX3z8xDur4c7W1wnpb`.

**Remaining:** confirm the current canonical Feishu design link, deploy distribution/editorial services and create signed production content packs.

## 7. Conversation and Provider Platform

### 7.1 Text Conversation Runtime

The runtime is the turn coordinator. It owns turn state, context budget, streaming lifecycle, atomic journal commit, cancellation and post-turn event dispatch, but not subsystem canonical data.

Implemented behavior includes request replay, execution lease, illegal-state rejection, encrypted local history, key rotation selection, response-token reservation, per-workload route budget, restricted disclosure, stream resume/cancel, atomic commit and idempotent post-turn consumers.

**Implementation:** `src/sora_sandbox/conversation_runtime/`.
**Canonical local doc:** `docs/TEXT_CONVERSATION_RUNTIME.md`.
**Tests:** `test_conversation_runtime.py`.
**Audits:** `DbFZdBqmNopRQExsUcgcjdw4nEg`, `L2btdGlZ8ojkODxwC2Xcvoe0n6d`.

### 7.2 Live Text Conversation Gateway

The Gateway is an identity-scoped local delivery service between the desktop trusted host and runtime. It owns API validation, event sequencing, idempotency, registry conflicts, streaming/reconnect, cancellation, recovery, observability and supervision.

**Implementation:** `src/sora_sandbox/conversation_gateway/`.
**Canonical local doc:** `docs/CONVERSATION_GATEWAY.md`.
**Tests:** `test_conversation_gateway.py`.
**Audits:** `EhcPdTUf2o1G8bxFXg5ceWt8nTh`, `VpDkdaGasotqB0xTggMcXiFFn8g`.

**Current environment issue:** the system Python used during this refresh does not have `fastapi`, so Gateway and Desktop Contract test modules could not be imported during collection. This is a tooling/dependency gap, not evidence that their code passed or failed.

### 7.3 Model Provider and BYOK

The provider system allows the demo/developer to use a configured key and allows end users to bring their own API key without exposing raw secrets to the WebView.

Implemented elements include provider/profile contracts, endpoint policy, OS keyring secret references, capability probes, broker/router, workload eligibility, fallback, admin service and legacy credential migration. Production UI must not assume a single provider or only `deepseek-chat`; it must represent compatible DeepSeek models, capability discovery and custom OpenAI-compatible endpoints.

**Implementation:** `src/sora_sandbox/model_provider/`, `credentials.py`.
**Tests:** `test_model_provider.py`, `test_app.py`.
**Git milestone:** `a77ea7a`.

**Remaining:** finish the desktop catalog/custom endpoint UX, certify provider/model capabilities and catalog the standalone Feishu Provider PRD URL.

## 8. Desktop Product Ledger

### 8.1 Production source and accepted specification

- `apps/desktop/` is the only production desktop application.
- `apps/desktop-design-prototype/` is an executable experience specification.
- The prototype must never become a second product runtime.
- Prototype components may migrate into production only while preserving real Tauri/Gateway contracts.

### 8.2 Frozen visual direction

**Quiet Companion Desk / Sora's quiet conversation desk**

- restrained porcelain/mist surfaces,
- ink typography,
- jade primary interaction,
- cold blue information,
- quiet violet only for secondary companion state,
- coral for warning/error,
- no gradients, glassmorphism, decorative blobs or generic AI purple,
- no full-height Sora poster column,
- artwork only in intentional avatar, onboarding, empty, About, installer or companion-presence moments.

The permanent rail exposes Conversation, History, Memory and Privacy, and Settings. Conversation controls belong near conversation behavior, not as an internal configuration dashboard.

### 8.3 Normal user surface versus Developer Mode

Normal users may see:

- what Sora will do in natural language,
- user-confirmed facts that affected a response,
- what was deliberately not remembered,
- hide, forget, export and consent actions,
- provider health and recoverable errors.

Normal users must not see:

- policy matrices,
- internal relationship/affect/persona scores,
- raw prompts,
- queue names, schema versions, traces or correlation internals,
- dangerous admin actions.

Developer Mode is disabled by default and Chinese-first. It may show turn timeline, typed frames, model routes, memory pipeline, asynchronous work, companion-system snapshots, eval/release status and controlled commands. It must use the same profile/conversation/turn context and must not expose arbitrary shell, SQL, Python, eval, filesystem or raw secrets.

### 8.4 Current desktop state

The production source contains React/Tauri infrastructure, typed transport interfaces, Tauri and mock transports, native credential prompt, sidecar host/supervision work, conversation state, cancellation, provider settings, contract generation and packaging scripts.

The accepted prototype covers conversation, empty/offline/recovery/conflict/update/safe states, history, memory/privacy, settings, provider configuration, controls and Developer Mode in light/dark/compact layouts.

The active workstream `ws-desktop-prototype-fidelity` owns the current migration. Do not edit its claimed paths without coordination.

### 8.5 Desktop release completion criteria

The desktop release is not complete until:

1. every page uses real typed contracts or an explicit unavailable state,
2. streaming, cancellation, reconnect, replay and recovery pass end to end,
3. memory/privacy and provider operations are real and reversible,
4. Developer Mode uses capability-gated snapshots and commands,
5. keyboard, focus, contrast, screen-reader and reduced-motion checks pass,
6. compact `420x560` and standard desktop layouts have no overlap or truncation,
7. target-hardware latency and resource budgets pass,
8. installer, signing, update, N-1 migration and rollback are verified,
9. SBOM, canary and Beta release gates are recorded.

## 9. LLM Workload and Latency Policy

### 9.1 Synchronous candidates

- response generation,
- Turn Interpreter only when deterministic parsing is insufficient,
- Semantic Arbiter only when signals remain ambiguous,
- memory rerank only when configured quality/latency policy permits,
- WorldOps choice interpretation only for active authored ambiguity.

Interpreter and Arbiter thinking are explicitly disabled. Explicit controls should resolve without an LLM.

### 9.2 Asynchronous workloads

- memory extraction and repair,
- consolidation planning,
- Persona proposal interpretation,
- Affect semantic lane,
- Relationship semantic evidence interpretation,
- contribution summarization and offline evaluations.

### 9.3 Provider rules

- Capability determines eligibility; model display names do not.
- Raw API keys stay in OS credential storage and never enter traces or the WebView.
- A missing provider must produce a clear recoverable state.
- Test fakes may be injected explicitly; production runtime must never silently select them.
- Timeout/token policies belong to workload design and must not be added globally without evidence.

## 10. Privacy, Consent and Safety Invariants

1. Local-first storage is the default for personal runtime state.
2. Secrets and forbidden payloads fail closed before durable memory.
3. Raw sensitive content is minimized, encrypted where required and redacted from traces.
4. Forget outranks heat, consolidation and projection.
5. Tombstones prevent immediate relearning and have a bounded lifecycle.
6. User participation is required before a WorldOps event becomes a shared episode.
7. Main-canon contributions are anonymous by default and editorially reviewed.
8. Relationship agreements require explicit confirmation and revocation.
9. Quiet/mute controls cannot be overridden by Persona, Affect or Relationship.
10. Export/restore validates owner, Sora instance, integrity and compatible schema.
11. Developer Mode cannot reveal raw secrets or bypass canonical APIs.
12. No system optimizes dependency, jealousy, possessiveness, loyalty pressure or compulsive retention.

## 11. Evaluation and Release Evidence

### 11.1 Existing evaluation assets

- extractor golden and real-provider reports,
- lexical and hybrid RRF recall baselines,
- multi-memory retrieval cases,
- Interruptibility golden/live evaluation,
- Persona trajectory cases,
- Affect golden and trajectory cases,
- Relationship golden/live release metrics,
- WorldOps golden, trajectory and long-horizon quality,
- Conversation Runtime and Gateway golden data,
- Desktop TypeScript unit tests and cross-language golden contracts,
- Module 10 scenario replay, SLO and release gate.

### 11.2 Refresh-time test inventory

`pytest --collect-only -q` discovered 394 tests before collection stopped on two missing-dependency import errors. The current system Python lacks `fastapi`, affecting:

- `tests/test_conversation_gateway.py`
- `tests/test_desktop_contracts.py`

This count is inventory evidence only, not a passing test report. Use the project environment with declared dependencies before release claims.

### 11.3 Required release evidence

Every release-relevant workstream must retain:

- exact source revision,
- environment/dependency lock,
- selected eval suites and baseline,
- pass/fail metrics and accepted exceptions,
- security/privacy checks,
- migration/rollback evidence,
- UI screenshots for visual changes,
- operator or user-facing recovery path.

## 12. Git-Verified Delivery History

The repository history currently verifies this delivery sequence:

1. response orchestrator PoC,
2. proactive interruption gating,
3. turn interpretation and memory foundation,
4. extractor and Gate architecture,
5. production retrieval and audit hardening,
6. Recall Planner and generalized user-domain rules,
7. Surface Policy with real provider defaults,
8. lifecycle and projection gateway,
9. quality operations and fast auxiliary inference,
10. Interruptibility and Persona,
11. Persona lifecycle hardening,
12. Affect,
13. Relationship plus governance and release metrics,
14. WorldOps plus sync and production controls,
15. Text Conversation Runtime,
16. Live Conversation Gateway,
17. Model Provider and BYOK.

The exact commit catalog is in `catalog/milestones.json`. Uncommitted desktop and context work must not be presented as part of `main` until committed.

## 13. Current Program Status

### 13.1 Implemented engineering baseline

- Memory Modules 01-10
- Interruptibility
- Persona
- Affect and Internal State
- Relationship
- WorldOps Narrative
- Text Conversation Runtime
- Live Text Conversation Gateway
- Model Provider/BYOK
- production desktop foundation
- accepted full desktop experience prototype

### 13.2 Integration in progress

- prototype-to-production desktop migration,
- full real-contract page wiring,
- provider/model catalog and custom endpoint UX,
- Developer Mode real snapshot/event/command plumbing,
- Windows release packaging and operations.

### 13.3 Explicitly deferred

- desktop perception,
- camera/vision,
- voice,
- always-on Live2D/3D presence,
- multimodal behavior inference.

## 14. Prioritized Open Work

### P0: text desktop release

1. Finish production desktop migration without losing Host/Gateway integration.
2. Complete real E2E conversation, history, memory/privacy, controls and provider flows.
3. Validate recovery, cancellation, reconnect, conflict and safe mode.
4. Complete installer signing, update, rollback, migration and release gates.

### P1: production quality and services

1. Restore a reproducible Python environment and run the complete suite.
2. Provision/benchmark production vector and telemetry services.
3. Implement real Developer Mode capabilities and audit trail.
4. Calibrate all semantic and lifecycle systems against consented Beta data.
5. Deploy WorldOps content distribution/editorial services and author content packs.

### P2: documentation and operational closure

1. Confirm missing standalone Feishu design URLs.
2. Link memory trace retention to lifecycle policy.
3. Establish baseline promotion, backup/restore and disaster-recovery runbooks.
4. Keep overall architecture and progress documents synchronized with release facts.

The machine-readable queue is `catalog/open-items.json`.

## 15. Canonical Artifact Map

### 15.1 Overall

- Overall Architecture: `Ax9FdxrV5oNHqlxW7O4cEebln3e`
- Engineering Progress: `QgHydreJHoktmxxO7VWceLd9nue`
- Memory Architecture: `O8uDdAggsofRM2xrB5TcuoOhnUu`
- Project Knowledge Space: `MVPuwrL4XiadlBk7bKlcbzvinCb`
- Desktop PRD: `RrFkwF6p9iqtIJkaa9fcELABnld`

### 15.2 System designs

- Memory 01: `K5Gvd02F8o00rexLWCzcy0Z2nMa`
- Memory 02: `XBxPdktWaoUJI7xZMBqcloOwnTh`
- Memory 03: `XlF7doxxWow4ClxbB9XcCkpVnWd`
- Memory 04: `ZXEXdtB1Vo0KxHx8gPLc4zrfn8M`
- Memory 05: `U588ddMFpoj5fmxgDH5cEYjMnqh`
- Memory 06: `AJNodsUmeov55oxDvYace4IQnkb`
- Memory 07: `T5uddHGYboXxsBxFrlOczQtWnxc`
- Memory 08: `BJTSdsRMSoL05qxjUjLczuSnnlh`
- Memory 09: `MDpsdtnQpoVYQ9xqUwDcbYyZnzP`
- Memory 10: `AJfEdFEEEoKUQRxYq5ycAVU8nOX`
- Interruptibility: `Tn2td1lJqoXujOx8xgscnayunKc`
- Persona: `FYKCdChDqoG9r3xYvNlcpjcgnFb`
- Affect: `OJ4Ada3VSoolFOxspXpcRdrJnte`
- Relationship: `D0bfd7aYaoBlY7x1l0Hc3sCCng7`
- Text Runtime: `docs/TEXT_CONVERSATION_RUNTIME.md`
- Gateway: `docs/CONVERSATION_GATEWAY.md`
- WorldOps operations: `docs/worldops-runbook.md`

Full URLs, audit links, superseded references and verification status are in `catalog/artifacts.json`.

## 16. Context Maintenance Protocol

### 16.1 At task start

1. read repository `AGENTS.md`, compact snapshot and this ledger,
2. run `python scripts/project_context.py status --check`,
3. inspect Git status and active workstreams,
4. claim narrow paths,
5. read the owning system record and canonical artifact,
6. state the exact verified gap.

### 16.2 During work

- update the workstream when scope changes,
- record a new ADR for material architecture decisions,
- do not edit accepted ADRs in place,
- preserve other tasks' dirty files,
- update the system catalog when status or ownership changes,
- add open items when work is intentionally deferred.

### 16.3 At completion

1. run focused and blast-radius tests,
2. render UI changes and inspect required viewports,
3. update code/document/status links,
4. record exact changed paths and verification,
5. seal the workstream,
6. update the compact snapshot only when every new task needs the fact,
7. update this ledger for system-level status or roadmap changes.

### 16.4 Periodic drift audit

At each release candidate or at least monthly:

- compare catalog code paths with repository inventory,
- verify canonical Feishu URLs and known revision numbers,
- compare `main` milestones with claimed implementation status,
- run context validation and impact gate,
- inspect stale active workstreams,
- reconcile open items with current owners,
- mark audit findings historical only after code/test evidence exists.

## 17. What This Ledger Does Not Store

- API keys, tokens or secret material,
- raw private user conversations,
- chain-of-thought or hidden model reasoning,
- every abandoned brainstorming sentence,
- volatile values better obtained from runtime health or Git,
- claims that cannot be tied to code, tests, documents or an explicit decision.

The objective is complete engineering continuity, not indiscriminate transcript retention.

## 18. Known Documentation Uncertainty

Some standalone design documents were created during earlier tasks, but their returned URLs are not present in the current repository history. Until confirmed, the repository-local Runtime/Gateway docs and WorldOps runbook are the canonical operational references. Missing URLs are tracked as open work instead of guessed.

The Feishu documents can be more detailed than this ledger. This ledger records their role, current implementation relationship and unresolved gaps so a new task knows which source to open and what must be verified.
