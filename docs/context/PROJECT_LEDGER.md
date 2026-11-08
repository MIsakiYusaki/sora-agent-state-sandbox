# Project Sora Engineering Ledger

用户最新顺序（2026-11-08）：**B3陪伴控制与真实领域治理 → B4叙事体验 → B2记忆效果与自动维护 → B1历史 → B5完整调试补齐 → B6发布扩大覆盖**。此前“下一批B1/F10/F11”计划已由此明确替换，编号和既有实现证据保留。

Last refreshed: 2026-11-08
Ledger revision: 29
r10 acceptance parent: `37756f3e3d20d6ff2464f039ed7616a3f12c2431`. Nov8 user authorized the current source snapshot for GitHub; origin/main was fetched and matched this parent before publication. Use git log for the actual checkout commit. Portable scope: docs/releases/2026-11-08-r10.md. Raw local exports/evidence and installers remain untracked.
Runtime scope: Windows-first, local-first, text-first companion

11月8日新增r9：B4客户端可从聊天信息参与真实签名故事选项、读取任务/个人时间线及实际共同记忆审核结果；共同经历的证据、候选、Gate入队同事务，WorldOps经历禁止自动提交。B2实现A03语义MERGE（摘要/标签/重要性、来源/基线检查、隐私保留及冲突回滚）、A04实际返回文本同口径预算，以及默认每小时最多100条的热度/到期/遗忘标记维护；记忆与隐私可查看结果并确认暂停/恢复。当前故事来源仍是明确标注的本机开发预览，新的灯笺内容源尚未正式发布，正式内容分发/编审/签名服务仍开放。r9已安装，28,958,485字节，SHA256 4d6aa3c95319be0b7762d808f7faef344265e1cfd367d14560cf3e45796a4384，未签名。验收与边界见 outputs/memory-effectiveness-20261108/VERIFICATION.md；无外部模型请求或日常数据修改。

11月8日r10已安装：B1历史核心补齐，正文搜索/多页续查、旧会话实际最近活动排序、准确分页末尾、预览与对话阅读锚点、独立历史读取错误、同请求去重和A→B→A迟到回包隔离。普通列表只解密标题/末条摘要，不创建明文正文索引。Python692及前端154项通过，7个亮暗/紧凑/错误与滚动夹具通过；实际构建/安装Gateway验证105段会话、400条加密消息八页与重启；实际安装窗口完成正文搜索→原会话预览→继续原会话→加载前文并保留位置。包28,962,452字节，SHA256 c492940c8e5090f080b95a1d3822c57beb59d0f5cad4fdbb874378f34e403994，未签名。Host仅有预期UNK→NSS三字节打包标记差异，其他字节一致，Gateway符合manifest。无外部模型请求或日常数据修改；未重测新的真实模型续聊回合。证据 outputs/history-core-20261108/VERIFICATION.md。下一批B5优先补用户可用的主动陪伴/后台回应及深度记忆整理，所需真实调试处置随能力补齐；B4正式内容服务与发布依赖仍单列。

## 1. Purpose and Authority

The grouped component design directory is `docs/PRD_INDEX.md`. It expands all ten Memory module PRDs, registers the six existing Presence/Expression/Contract/Security/Operations/Release designs, and maps internal Runtime components to their owning PRD sections. This directory refresh changes discoverability, not feature or release acceptance. Canonical designs, research and historical audits retain their distinct catalog types.

October29 full PRD practice refresh: all28 indexed designs/overviews now carry current implementation, operative contracts, source/evidence and remaining acceptance. 110 guarded body updates corrected obsolete claims and labelled old implementation tables/migration baselines as historical; original references, links and architecture resources were retained and read back. Final document revisions are indexed in `catalog/artifacts.json` and [the complete update receipt](../../outputs/prd-refresh-all-20261029/COMPLETION.md). Overall182, unified-dataflow40 and Desktop145 supersede the earlier partial-sync design snapshots. Navigation293 points to context23/ledger17; navigation/brief directory link labels now show dataflow v1.4 (brief134), with architecture boards unchanged in the main body. Presence/multimodal remain planned; F01-F30, Memory encryption coverage, default lifecycle scheduling, full observability, new installed acceptance and release gates remain open. This task changes documents only, not application behavior, test counts, installed binaries or deployment status.

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
       -> HTTP API and SSE delivery in the current Rust Host (other Gateway transports remain separate capabilities)
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

The single maintained current wiring specification is `docs/architecture/CONVERSATION_DATAFLOW.md`. The following is only a summary. Composition belongs to SoraRuntimeStack; per-turn orchestration belongs to ConversationRuntime and AgentRunner, not Gateway.

### 4.1 Hot path

The hot path is responsible only for producing a safe response and an atomic turn outcome.

1. Gateway authenticates local identity and validates idempotency.
2. Runtime opens or resumes a turn journal entry.
3. Explicit user controls are parsed before model assistance.
4. Turn Interpreter resolves bounded semantic fields; thinking is disabled.
5. Interruptibility produces orthogonal permissions.
6. Recall Planner decides whether and how memory should be searched.
7. Hybrid retrieval and Surface Policy produce a budgeted, governed memory context.
8. Persona, Affect, Relationship and WorldOps provide typed frames during context assembly. These are not all parallel LLM appraisals; see the exact pre-read/permission/recall order in the authoritative dataflow.
9. Context packer reserves response capacity and excludes forbidden features.
10. Model Provider selects an eligible route by capability, policy and availability.
11. The provider response is buffered and deterministically constrained before validated client deltas stream through Gateway. Deltas remain provisional and may be cancelled.
12. Runtime atomically commits user input, response result, trace references and post-turn outbox.

Fast deterministic behavior is preferred for explicit controls and known state. An LLM is used only where semantic ambiguity requires it.

### 4.2 Asynchronous path

The current background pipeline is below. Since 2026-09-10 the default composition root starts a managed Memory extraction scheduler. Periodic lifecycle scheduling remains open. After a reactive turn is committed, the four direct consumers are Memory, Affect, Relationship and WorldOps; Persona receives committed-memory projections. Memory receives the complete committed event and preserves conversation/runtime-turn/source-event/correlation/owner identity in a durable receipt. Details and evidence: `docs/MEMORY_BACKGROUND_INGESTION.md`.

1. episode and evidence events are sealed,
2. extraction jobs claim eligible episodes,
3. the managed scheduler waits for a configured provider and runs the LLM Extractor under a recoverable, renewable lease; validated results are checkpointed before materialization,
4. candidates enter deterministic Gate and possibly quarantine/review/defer,
5. accepted candidates commit transactionally to canonical memory,
6. outboxes update FTS/vector/graph-lite indexes and projection gateways; current memory changes may drain these inline,
7. explicitly invoked lifecycle jobs update heat, consolidate, archive and apply tombstones; periodic scheduling is still open,
8. Persona/Affect/Relationship/WorldOps consume typed events through their own coordinators,
9. traces, metrics, evaluation and release evidence are retained according to policy.

No asynchronous result may rewrite an already-delivered response. Late results apply forward only.

## 5. Memory System Ledger

The ten memory modules are implemented as an engineering baseline. This means their main schemas, services, failure paths and focused tests exist. It does not mean every production deployment or desktop operation is complete.

### 5.1 Module 01: Episode and Evidence

**Purpose:** preserve the factual origin of potential memories before deciding what deserves durable storage.

**Implemented:** conversation event ingestion, episode windows, evidence spans, source references, hashes, visibility/deletion metadata, extraction jobs, idle/window sealing, low-signal prefiltering and deletion propagation. The production committed-turn path now uses a durable source receipt and atomic source/evidence/candidate/job ingress; source identity conflicts fail closed and repeated delivery cannot recreate a deleted episode.

**Key paths:** `memory_episode_ingestion.py`, `memory_store.py`, `memory_schema.py`.

**Design:** `K5Gvd02F8o00rexLWCzcy0Z2nMa`.

**Still needed:** production retention calibration; desktop evidence export/deletion experience; large-history storage benchmark.

### 5.2 Module 02: LLM Extractor and Four Operations

**Purpose:** turn episodes into evidence-backed candidate operations without blocking the response path.

**Implemented:** provider protocol, real configured LLM path, thinking control, structured schema, evidence span validation, JSON repair and second-pass repair, cost/usage traces, related-memory targeting, async claim/retry/completion, backfill and golden evaluation. Fake providers remain test fixtures only.

**Operations:** ADD, UPDATE, MERGE and DELETE, plus NOOP/REVIEW control outcomes. Novelty/conflict can use deterministic similarity; confidence and importance may use bounded model judgment.

**Default scheduling (2026-09-10):** `MemoryExtractionScheduler` starts after adapter registration, polls durable jobs, waits without consuming attempts when no route is bound, renews worker leases, fences stale or canceled results, and reuses validated checkpoints after retries. Queue status is exported through Module 10. Memory workers drain before target systems and providers close. The real `deepseek-v4-flash` smoke completed one extraction in 10.45 seconds, retained source identity and routed its candidate to Gate review; this is not a latency SLO or broad model-quality evaluation.

**Key paths:** `memory_extraction.py`, `memory_extractor_provider.py`, `memory_extractor_repair.py`, `memory_extractor_worker.py`, `memory_write_decision.py`.

**Monitoring (2026-09-14):** `memory_job_observability.py` records content-free attempts in the owning Memory database and bridges them into authenticated per-turn debug snapshots. Includes real queue timestamps/retry backoff/checkpoint flags and measured extraction, validation, calibration, checkpoint and combined materialization/Gate phases. Diagnostics fail open; leases, cancellation, retries and canonical writes remain owned by the existing worker. Attempts are read-filtered to seven days and pruned on later writes; interrupted history is not shown as live. Two authorized isolated real-model jobs completed on attempt 1; older pending samples lacked sufficient evidence to identify their cause. Other domain workers and physical Provider calls are not covered by this bridge.

**Candidate verification (2026-09-14):** 478 Python / 28 frontend / 5 Rust tests, production build and unsigned NSIS candidate passed. Packaged sidecar health, debug auth/read and graceful shutdown passed using isolated state with no model calls. Desktop/compact recorded-timeline replay was reviewed without observed overflow or console errors. Candidate SHA-256: `7c0013fade85d7f22eabbbe0f2a4e7a0e991dc1fe65d15140499775c416a5638`. Installed-app E2E, full accessibility, signing/update/rollback and Beta gates remain open. See `outputs/turn-observability/VERIFICATION.md` for the artifact and bounded evidence.

**Design:** `XBxPdktWaoUJI7xZMBqcloOwnTh`.
**Audit:** `BLeAdZfwzoM9kdxsZFUcRf7Vnce`.

**Still needed:** real-user multilingual calibration, continuous hallucinated-memory negatives, cross-episode gradual-preference evaluation.

### 5.3 Module 03: Gate and Governance

**Purpose:** decide whether a candidate may become canonical memory.

**Implemented:** materialize-then-gate boundary, deterministic eligibility, independent quarantine table, review queue, defer and expiry, idempotent candidate hash, conflict routing, sensitivity handling, auto-commit constraints, rollback compatibility and audit traces.

**Key paths:** `memory_gate.py`, `memory_gate_engine.py`, `memory_gate_pipeline.py`, `memory_gate_schema.py`.

**Design:** `XlF7doxxWow4ClxbB9XcCkpVnWd`.
**Audit:** `VhB2dTbFZozsnqxFqSIcKrM3nBg`.

**November8 delivery:** low-sensitivity review UI approve/reject/defer is installed-verified; decisions and exact-request audits are atomic. Post-commit durable delivery/recovery replaces pre-commit external effects. **Still needed:** reviewer SLA, threshold calibration, separately authorized sensitive disclosure and full external fault coverage.

### 5.4 Module 04: Canonical Store and Eventing

November6 A01 source fix adds SQLite exception rollback to public candidate commit, covering the complete local batch, revisions and outboxes. Failure injection plus later independent writes and idempotent retry pass. A02 formal release gates now block missing/unverified baseline comparison and retain an open blocker audit event. Seven new cases and full current Python598 pass95.83s. Evidence: outputs/memory-blockers-20261106/VERIFICATION.md. Historical November6 boundary; Nov8 r6 closes checked review/post-commit core and r9 closes A03/A04 plus deterministic lifecycle scheduling. Full A05 semantic consolidation and external fault coverage remain separate.

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

**Nov8 r9:** managed hourly heat/expiry/tombstone batches and persisted confirmed pause/resume are wired. Semantic consolidation remains the explicit owner service; full ANN/local-graph/LLM consolidation, production calibration and trace retention linkage remain open.

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
**Design:** `ZxQidBZGZopHbNxnpw3cXk2Unug` (actual title verified 2026-09-09).
**Audits:** `VOZIdjiHsoaD1kxo5flcLwgCn2e`, `O52idpo2HoZ4gkxR9RScevcunXb`.

**Remaining:** Beta calibration, long-horizon non-escalation monitoring and target-hardware latency measurement.

### 6.4 Relationship

Relationship owns per-Sora/per-user continuity. It models multiple dimensions, agreements, reciprocity style, boundary preferences and repair state rather than one affection number.

The system isolates actor scope, rejects forged evidence, uses explicit confirmation for agreements, supports repair FSM, rebuild, rollback, forget and encrypted export/restore with owner/Sora validation. Relationship values cannot be coupled to response permissions or initiative.

**Implementation:** `src/sora_sandbox/relationship/`.
**Tests/eval:** `test_relationship_system.py`, `evals/relationship`.
**Design:** `MOceduqJroFb9VxkWhfc0gn1nhf` (v3.1).
**Audits:** `HOPudrP1xojbU4xXFescFdKbnMc`, `CaZIdiaHXoUDyPxm7oScEg47n6k`, `D0bfd7aYaoBlY7x1l0Hc3sCCng7` (historical R3, previously misclassified as design).

**Remaining:** Beta calibration, desktop agreement/repair/privacy UX and production operator workflows.

### 6.5 WorldOps Narrative

WorldOps is a canonical authored narrative platform, not a collection of prompt snippets.

Writers author worlds, events, factions, characters, tasks and completion predicates on the service side. Signed bundles are distributed to clients. The client evaluates only authored transitions and records a personal timeline. LLM interpretation is bounded to known authored IDs and cannot invent canon. User choices may be submitted anonymously by default as minimized contribution proposals; publication into main canon always requires editorial review.

**Implementation:** compiler, schema registry, signed bundle transport, server store, content studio, client timeline, deterministic predicate/effect engine, task FSM, device sync/conflict, contribution review, outbox, rebuild, quality and observability under `src/sora_sandbox/worldops/`.

**Tests/eval:** `test_worldops_system.py`, `evals/worldops`.
**Operational guide:** `docs/worldops-runbook.md`.
**Current design:** `OJ4Ada3VSoolFOxspXpcRdrJnte` (WorldOps v2; not Affect).
**Early reference:** `VcJcd4zO3oqsaVxHOn3cehUAn9f`.
**Implementation audit:** `BcjTdXVXvoX3z8xDur4c7W1wnpb`.

**Remaining:** deploy distribution/editorial services and create signed production content packs.

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

The project virtual environment is the authoritative test environment for this area. On 2026-09-02, the focused Gateway and Desktop Contract run completed with 29 passing tests, including authenticated/redacted Developer Mode snapshot coverage.

### 7.3 Model Provider and BYOK

The provider system allows the demo/developer to use a configured key and allows end users to bring their own API key without exposing raw secrets to the WebView.

Implemented elements include provider/profile contracts, endpoint policy, OS keyring secret references, capability probes, broker/router, workload eligibility, fallback, admin service and legacy credential migration. Production UI must not assume a single provider or only `deepseek-chat`; it must represent compatible DeepSeek models, capability discovery and custom OpenAI-compatible endpoints.

**Implementation:** `src/sora_sandbox/model_provider/`, `credentials.py`.
**Tests:** `test_model_provider.py`, `test_app.py`.
**Git milestone:** `a77ea7a`.

**Request observation (2026-09-16):** registry Broker and legacy chat, extraction, embedding, recall planning, rerank and surface HTTP exits now add content-free open/read/close spans to the active turn or Memory-attempt collector. Logical fallback/repair can produce multiple recorded requests; extraction JSON repair is a separate phase. Detached parent context prevents stream yields from reparenting consumer work; early generator close is interrupted. No routing/retry/budget changes. This does not split transport-internal retries/redirects, DNS/TLS, per-request usage or TTFT. Ten focused tests and the authorized isolated two-turn/six-HTTP-call sample verify the path; both Memory jobs completed. Latest serial full regression: 488 passed, zero failures/errors/skips, 76.31 seconds. New unsigned candidate and detailed full-regression history (including one earlier intermittent Interruptibility gate failure whose cause remains unconfirmed) are in `outputs/provider-http-observability/VERIFICATION.md`. Installed-app E2E remains open.

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

The Quiet Companion Desk conversation surface and natural Composer controls are migrated into production without replacing the real Host/Gateway path. History uses the real conversation index, preview and resume contracts. Memory and Privacy uses real governed summary, visibility and forget operations and keeps internal scores out of the normal UI.

Developer Mode now receives authenticated redacted snapshots and current Gateway events across eight sections: per-turn timeline, context frames, model routes, memory pipeline, async work, state systems, eval/release and controlled console. Real runtime journal timestamps, events and model calls drive timeline measurements. Snapshot polling waits two seconds after each read completes; no overlapping polling requests, stale data after failure is labelled, and late responses from previous conversations are ignored. Missing provider tokens, queue watermarks or quality evidence remain unreported instead of fabricated zero/success. Frame existence/version/field/budget metadata and the real command registry are projected, but frame contents and preview/confirm/audit command execution remain incomplete. Profile identity is hashed; database paths, credentials, raw prompts, hidden reasoning and arbitrary execution are excluded.

Runtime schema v2 performs nullable token migration atomically, including table replacement and schema-version writes. Tests inject interruption after replacement, verify rollback, reopen/retry, preserve nonzero legacy counts and distinguish new explicit zero from unknown. Runtime and new Provider traces recognize input/output usage aliases without overriding a primary explicit zero. Gemini no longer supplies fabricated missing counts. This is not a global rewrite of historical Provider registry traces or all backend cost helpers.

The sealed workstream `ws-desktop-production-v2` owns the main implementation. The follow-up `ws-desktop-visual-validation-v1` used the repaired browser harness to review the current production UI at `1440x900` and `420x560` in the affected light and dark states. It corrected dark Conversation identity contrast, compact Memory empty-state duplication, stale debug-snapshot wording and an unsupported branch label. The reviewed pages had no horizontal overflow; dark identity text measured 14.2:1; and a real message exposed seven current Gateway events in Developer Mode without fabricating typed-frame or command bridges. The final duplicate console-log/reload pass was prevented by the Codex browser-use quota, and keyboard, screen-reader, reduced-motion and target-hardware accessibility verification remain open.

The September 6-7 follow-up corrected production user-message alignment, compact status labels, type readability, diagnostic contrast and the explicit-theme/legacy-token mismatch affecting the dark conversation sidebar. Browser checks used the production UI with explicit Mock transport for layout only, not as proof of real provider calls. Checked light/dark 1440x900 and compact 420x560 surfaces had no horizontal overflow. Final September 7 contrast measurements were 8.50:1 for the diagnostic status tag and 8.65:1 for the timeline scale. No warning/error entries were returned by the checked preview console. Full assistive-technology, reduced-motion and target-hardware coverage is still open.

The 2026-09-07 NSIS candidate passed build and manifest verification. It is 28,631,573 bytes with SHA-256 `07aacb5c2461e420611db802ef8797a430cd876a6af80a15cdee46990ae62b2b`; the Gateway sidecar is 24,860,409 bytes. The installer remains unsigned and installed-app/real-provider E2E acceptance has not been performed. The release dist has no source maps, Mock transport markers or prototype seed strings. Source-only unused prototype/fixture code is not a production capability.

### 8.5 Installed acceptance update, September 20-21

The actual installed WebView, Rust Host and packaged Gateway were exercised with authorized DeepSeek synthetic conversations in an isolated profile, not browser fixtures or a standalone sidecar smoke. Windows extended-path profile I/O fixes the reproduced atomic-save MAX_PATH failure without changing Windows policy or canonical filenames. Identity-scoped Gateway bindings now supply the canonical runtime conversation ID to Memory timeline lookup; ownership/turn/correlation filters remain intact. Installed UI confirms completed jobs and nested extraction/HTTP/validation/Gate phases.

The September 20 unsigned candidate is `outputs/installed-e2e-20260917/Project-Sora-0.1.0-20260920-acceptance-x64-setup.exe`, 28,674,523 bytes, SHA-256 `a915c60b59e16f9350db10883b5e7148d38961860e0dc2c3a840485be4851c52`. Actual NSIS upgrade exited 0 and program hashes match. Full Python regression: 500 passed, no failures/errors/skips, 86.968 seconds. Focused evidence preserves failures before both fixes and 33/41 passing tests afterward. No frontend source changes were made in these fixes.

Installed long-root state ultimately contains six committed turns, twelve canonical messages and five completed first-attempt Memory jobs. Upgrade and subsequent application launch restore existing history. Killing only the verified idle Gateway server was recovered on the next Host request without a new generation or history duplicate; health was 200 and unauthenticated debug 401 before and after. This is not proactive monitoring, mid-turn crash recovery or soak.

Full acceptance remains open: four UI cancellation attempts yielded committed outcomes, not a verified pre-commit cancellation. Last long reply visibly ends mid-sentence while committed, with response completion_tokens=1200; finish reason and output-validation handling require diagnosis. Do not increase token limits to hide this finding. Context-frame metadata, routes, queue watermarks, Persona health, eval registry and controlled-command registry are real; frame-content/command execution bridges are incomplete, some worker states are unreported, and no eval run is present in this profile. Eval list `error` is unlabelled default severity, not a run outcome. Light/dark/maximized screenshots are bounded checks, not complete accessibility acceptance. All exact IDs, screenshots, failures and limits: `outputs/installed-e2e-20260917/ACCEPTANCE.md`. No daily conversation DB was opened or credentials exported.

September 22-23 follow-up: `RoutedLLMClient` validates stream completion before exposing buffered content and retains usage on failure; Responses incomplete terminals now propagate. Normal stop/end-turn reasons pass, incomplete/missing/conflicting/unrecognized terminals fail closed, and EOF cancellation is checked. Non-stream compatibility delegates are outside the strict stream contract. 519 Python tests passed (77.839 seconds). The actual installed DeepSeek failures `turn_30aa960024bb4492a185fa107bc03fe6` and `turn_79b443a04a4f412aae9dc7a763b7c1bb` have no response deltas, canonical history or outbox; both retained 1200 completion tokens as failure usage. This does not prove the historical failed sample's original finish reason, nor does it make all 1200-token replies failures.

The September 22 frontend correction maps active `turn.failed` to safe local Chinese copy, marks the user turn incomplete and restores the submitted draft only if no newer draft exists; it ignores duplicate/unrelated failure terminals. 33 frontend tests and NSIS build passed. Real installed UI shows the failed marker. A late stop-position click after failure resubmitted the restored synthetic text, so the complete alert/draft recovery interaction is not yet accepted. No automatic retry was added. Esc-to-cancel in the composer and visible no-output cancellation status are implemented on September 23; 41 frontend tests pass, installed verification pending. Current artifacts and exact results are in `outputs/installed-e2e-20260917/ACCEPTANCE.md`. Release and operational bridge gaps above remain open.

September 23 installed follow-up supersedes the pending alert/draft evidence: candidate `Project-Sora-0.1.0-20260923-keyboard-stop-x64-setup.exe` (28,676,154 bytes; SHA256 `64afe6bfd7e72002971aa23b9622ef45d61fc18970a257b1ab7b603423c5bb58`) installed with exit 0. Actual UI captured the Chinese incomplete-response alert, failed marker and complete restored draft together (`41-incomplete-alert-draft-20260923*`). Three Esc attempts nevertheless ended failed/IncompleteModelResponse, not cancelled; no associated deltas/history/outbox or added Memory jobs. Final metadata `installed-metadata-1790163932145.json`: 16 turns, 11 committed, 5 failed, 22 history rows, 10 completed first-attempt Memory jobs. These verify failure handling, not cancellation. A temporary thorough-response UI setting was restored to natural. Do not repeat blind paid UI attempts; obtain low-sensitivity cancel receipt/token/terminal timing next. No production default budget, route or retry change. Main acceptance stays open.

September 24 cancellation hardening: code inspection found public turn-specific requests were reduced to conversation-wide runtime cancellation. Gateway now forwards authenticated turn identity for explicit cancellation and stream timeout; coordinator matches under its lock so stale old-turn requests cannot stop newer execution. Runtime encrypted receipt artifacts contain only times, status before/after and token-change boolean, and are available in the content-free diagnostic projection. They are not terminal events or provider-abort timing; no new client delta/history/outbox is written by receipt recording. Internal shutdown compatibility remains conversation-wide. Pre-start durable cancellation, Host/WebView timing and desktop receipt rendering are separate remaining work. 66 focused tests passed; full regression 525 passed (77.98 seconds), followed by 9 final focused tests including three additional cases. New package/installed verification pending; the prior failed Esc samples are not retroactively diagnosed. Decision: `adr-turn-specific-cancellation-v1`.

September 24 package follow-up: candidate `Project-Sora-0.1.0-20260924-cancel-receipt-x64-setup.exe`, 28,675,936 bytes, SHA256 `84a04d54cbb539fc02f9a9ed8cac98ade60b9bc27ff04a5ebbbac63b6dc77873`, actually installed with exit 0. Two installed DeepSeek turns committed in 6326/5150 ms and Memory jobs completed first-attempt. Final metadata has 18 turns, 26 history rows, 12 completed jobs and zero cancellation receipts. Measured UI observations were 23.54 seconds apart; the second came 18.49 seconds after commit, so no stop was issued on that attempt. Valid installed cancellation remains open; user-assisted timely Esc was requested. Receipt/target matching are source-verified, not installed cancellation-approved. Exact evidence: `outputs/installed-e2e-20260917/ACCEPTANCE.md`.

September 28 autonomous acceptance supersedes the previous request for manual Esc: actual installed WebView input reached the existing Host/Gateway cancellation path for `turn_b7ccf024a62542d1bb574c1ee72acbf3`. Receipt observed generating → cancel_requested with changed token; terminal was cancelled/TURN_CANCELLED 2874 ms later. No delta, canonical history, outbox, Memory source receipt or extraction job. Real UI displays stopped and Developer Mode displays cancelled execution evidence. The following UI turn committed in 3137 ms with one first-attempt completed Memory job. Normal exit/restart preserved exact turn/history/outbox/Memory/Provider records and did not resubmit the cancelled request. Final 21 turns (15 committed, 5 failed, 1 cancelled), 30 history rows, 14 completed jobs. Fresh 41 frontend and 28 targeted Python tests pass. Same Sept24 installed binaries; no source change or rebuild needed. Read-only assertions and screenshots 45–49 are in `outputs/installed-e2e-20260917/ACCEPTANCE.md`. One generation-phase Esc sample does not certify all race windows, button cancellation, instant provider abort, mid-turn crashes, soak or release. Tool intervals remained long; the naturally longer arbitration provided this window, not an artificial delay. No user interaction is required to repeat the evidence checks.

September 29 installed mid-turn recovery: generation-stage termination of the identity-verified packaged Gateway server reproduced an English unmarked UI error and a turn left generating beyond the stale threshold after Host restart. Runtime now excludes valid matching leases during recovery, Gateway repeats the original five-minute stale check every ten seconds while running, Host rejects SSE EOF without a terminal, and React marks the specific user turn incomplete with Chinese uncertain-delivery guidance and retained draft. A neutral `stale_recovery` stage applies to both startup and periodic cleanup. Full Python regression passed 530 tests; frontend 43 and Host 5 tests passed for the preceding equivalent UI/Host candidate. The final NSIS candidate `Project-Sora-0.1.0-20260929-stale-stage-x64-setup.exe` (28,679,294 bytes; SHA256 `1722d9dfcbd6e818b37110a7c5c4898272fb39f9a8e8762fee722b12c137b054`) passed package manifest verification and installed with exit 0. A second actual WebView fault sample `turn_326c3c4ad1cb4d32804e3fbc2cc53f7e` recovered after five minutes as failed/stale_recovery without success history, outbox or Memory source/job; during a minimized no-input interval the total turn count and 34 history rows did not increase. An explicitly different follow-up UI message committed with delivered outbox and completed Memory source receipt; normal restart preserved all checked metadata. A prior interval produced another committed turn with unproven input provenance, retained as an unresolved observation. This bounded result does not close repeated crash/soak, all cancellation races, accessibility or release hardening. Exact process/hash/sample evidence is in `outputs/installed-e2e-20260917/ACCEPTANCE.md`.

October 10 atomic recovery follow-up: selection and failure were in separate transactions, so deterministic cross-connection tests reproduced renewed leases/refreshed turns being failed. Journal now atomically checks nonterminal status, stale timestamp and live matching lease in the failure write; lease acquisition validates conversation/turn identity and renewal rejects terminal turns. Before-fix regressions failed three cases; full post-fix Python suite passed 533 tests (81.36 s). New unsigned candidate `Project-Sora-0.1.0-20261010-atomic-recovery-x64-setup.exe` is 28,667,221 bytes, SHA256 `2b28a2d8a5e6a09b5bc830d2d61a4551409dac970dbd1b69b95e88edeac263b9`, and actually installed with exit 0. Actual installed Gateway passed explicitly isolated no-model startup/periodic recovery fixtures, live-lease exclusion, singleton failure events, no successful history/outbox, ready/auth/debug checks and graceful exit. Archived Sept29 real-UI assertion runner also passed. Upgrade restored identical current isolated records (28 turns, 38 history rows, 15 Memory jobs) without provider calls; an extra historical committed turn was already present in the Oct10 pre-upgrade baseline. New UI checks are unaccepted because the Windows runner cannot initialize due to file-sharing error 32 validating node_repl.exe. No broader Codex ACL reset was performed. This service fixture check does not certify new WebView/Host model interaction, soak, signing or remaining bridges. Evidence: `outputs/installed-e2e-20260917/ACCEPTANCE.md`; ADR `adr-stale-recovery-atomic-fence-v1`.

October 15 bounded candidate handoff: allowlisted scalar/enum/band/count/reference-hash frame values are captured only during explicitly enabled five-minute observation after the generation seal and read on demand through typed native/Gateway scope checks. Legacy source stages, missing/null/false/zero and permitted-field comparisons remain distinct; raw evidence/free text/reasoning are excluded. The candidate is installed (exit 0), program files backed up, and fixed at `outputs/context-frames-20261011/Project-Sora-0.1.0-20261015-context-frames-x64-setup.exe`, SHA256 `a1dce8d96175aedca5bf8f791ebeeb4a25b095455f5cc9d8cc4527593b26bc8f`, 28,736,896 bytes. Existing 557 Python/56 frontend reports pass; fresh Host 7 and packaged/installed no-model smoke pass. Four frame UI fixture scenes were visually checked; isolated upgrade lists stayed identical (29 turns/40 history/16 Memory jobs/174 provider calls). New native frame/real-model/restart E2E is unaccepted, not substituted by old native evidence or fixtures. Unsigned candidate handoff, not production release. User explicitly requested finite delivery, not another automatic feature expansion. Full evidence and deferred items: `outputs/context-frames-20261011/DELIVERY.md`.

October 28 audit repair supersedes the earlier registry/count-only status for F21: production UI/Transport/Rust/Gateway read safe persisted quality runs, paginated cases, baselines and events. Comparison uses persist_event=False. A separate five-minute main-window native-held capability grants quality read/resolve, not maintenance execute or Memory review resolve. Quality Ops resolves one event using BEGIN IMMEDIATE, full revision check and atomic status/audit commit; injected audit failure rolls back, same actor/revision/resolution is idempotent. Unknown mutation outcomes clear stale actions and require read-only reconciliation. Raw test bodies, message/payload and arbitrary baseline names stay private. Events expose severity, type, source fingerprint and a source-run link only for a real run. Case pages are 100; record lists honestly indicate a recent-100 cap. This records investigation decisions, not automatic fixes or release acceptance. Decision: `adr-desktop-quality-event-resolution-v1`.

The same source batch fixes Frame observation read/mutation races, adds App history late-index/late-body/identity checks, and verifies Provider fallback ordering beyond three entries, expected revision, exact-name removal and unknown configuration/removal states. F16 adds an explicit reject-and-dismiss action for quarantined candidates: Gate owner checks candidate/quarantine revision and atomically writes REJECT audit and both statuses; audit failure rolls back, repeats return the original audit. No protected disclosure, direct release or canonical commit is available. Decision: `adr-memory-quarantine-disposition-v1`. Full Python: 585 passed (94.65s), owner functional tests: 27 included therein; frontend 95, Host 9. Reports/build evidence: `outputs/desktop-functional-repair-20261028/VERIFICATION.md`. Standalone production quality/quarantine component layouts reviewed in 1440x900 light/dark and 420x560/error scenes without document overflow or console warn/error; not native installation evidence or complete accessibility verification. No new package, daily-data mutation or model request. All F01-F30 statuses and remaining exact gaps: `outputs/desktop-functional-repair-20261015/PROGRESS.md`; F18/F19/F22-F25 remain unresolved, other items retain explicit partial limits.

11月8日第一批P0记忆治理交付：审核批准/拒绝/延期与候选、正式记忆、投递意图和精确请求审计同事务提交；外部索引/领域投影/监听器在本地提交后投递，监听器持久化重试、过期来源阻断，真实领域注册后恢复。完整Python635项通过，新增16项故障/进程退出/重试一致性用例；实际r6安装窗口三种审核操作通过，待审核3→0且独立审计核对一致。另用合成凭据通过真实Windows凭据库的目标删除/邻居保留验收。r6未签名候选28,899,414字节，SHA256 `0cc8a44b021947817abaef54b4c74ecbd44443e731cb156dde769614789969f5`，已安装、manifest及程序完整性通过。无模型请求或日常数据操作。详见`outputs/memory-governance-20261108/VERIFICATION.md`。跨库为可恢复的至少一次投递，完整领域故障矩阵及其余F01–F30/发布门禁仍开放。

### 8.6 Desktop release completion criteria

November 7 queue/status follow-up supersedes the earlier F17-native-pending statement. Real installed r4 WebView completed selected dead-letter preview, confirmation, atomic requeue/audit and FTS drain; final event is processed/applied with cumulative attempts2. UI now checks all six derived-queue counters, keeps missing values unknown, distinguishes canonical record reads from aggregate health, and fixes unreadable maintenance alerts. Matching unsigned r5 is28,891,896bytes, SHA256 a7114d84cebbf84e4a6a957162116d7cda202b5e22d0ad5b600f8b3c86de30eb; installed exit0 and integrity matches exact Tauri NSIS marker. Frontend131 and ten explicit browser scenes pass; Python619/Host18 remain unchanged r4 reports, not new reruns. Packaged Gateway checks pass, installed r5 reads the same applied event, and QA Memory/queue/audit metadata is identical across upgrade. Earlier installed privacy/space/notification evidence remains valid within its limits. No new model calls, daily data or real vault deletion. Cross-database consistency, remaining F01-F30 and release gates remain open. Evidence: outputs/desktop-queue-native-20261107/VERIFICATION.md and outputs/desktop-memory-status-20261107/VERIFICATION.md.

October 11 maintenance bridge supersedes earlier registry-only status for seven commands: `eval.run`, `eval.replay`, `eval.metrics.collect`, `eval.release_gate`, `trace.index.sync`, `memory.index.drain`, `worldops.rebuild.verify`. Gateway uses registered Module 10 handlers, durable preview identity/hash and one-shot execution claim; unknown effects stay `execution_unknown` with open audit, no automatic retry. Five-minute native sessions bind identity/origin/client and only the actual Tauri main window can open one. Capability/confirmation secrets remain native; same-profile audit reads are redacted and cross-client execution is rejected. Unadapted schemas are null; force commit, destructive/export L3/L4 remain disabled. Full regression 543 Python, 52 frontend, 7 Host tests and NSIS build passed. Exact light/dark/compact/uncertain browser component fixtures passed, not native UI evidence. Both build and installed Gateway passed actual preview/one trace sync/audit/idempotency/revocation/graceful shutdown smoke without model calls. Installer `outputs/admin-console-20261011/Project-Sora-0.1.0-20261011-admin-bridge-x64-setup.exe` is 28,705,434 bytes, SHA256 `8ab1ef2ef8f3ca4273cccbb84e9ae00889db5937cb28b8cab57c2556f8123ca9`; NSIS exit 0 and program hashes match. Same-profile restart kept all twelve metadata categories identical (28 turns, 38 history rows, 15 jobs, 165 cumulative calls). Native UI initialization still fails; no new GUI command or real-model E2E claim. Details: `outputs/admin-console-20261011/VERIFICATION.md`, ADR `adr-desktop-maintenance-commands-v1`. Frame contents, remaining commands, accessibility, soak and release gates below remain open.

October 29 current-source reconciliation: `docs/DESKTOP_DELIVERY_STATUS.md` is the delivery summary and `outputs/desktop-functional-repair-20261015/PROGRESS.md` remains the F01-F30 item ledger. F23 Host-owned catalog/create/CAS/atomic locking, scoped Explorer opening and confirmed graceful Gateway drain/switch/restart are implemented; one isolated debug-native switch is observed, not installed acceptance. Managed desktop starts with `--no-legacy-migration` so empty spaces do not import global legacy secrets. F24 real WinRT/space-scoped preferences and quiet intervals exist; previous notification Setting failed0x80070490, new NSIS AUMID hook is unverified, MSI registration absent. F25 offline identity-derived erasure with runtime lock and owner-scoped Provider/vault purge is backend-only; no connected Host/UI/export flow. Current focused Python6, frontend107 (after correcting stale placeholder assertion), Host13/1ignored pass; Python585 is prior full baseline, no current combined full-run claim. No new package, model request or daily-data mutation; Rust minimum1.89. The prior approval-service quota block is resolved for these retests. After renewed user authorization all six cloud status summaries were updated/read back: progress81, architecture180, navigation277, dataflow38, Desktop PRD136, Memory151;26 exact block writes verified against publication XML, original requirements/resources retained. Evidence: `outputs/client-status-sync-20261029/CLOUD_SYNC.md` and `cloud-readback-verification.json`. Keep implementation streams active; remaining gaps are not waived by documentation completion.

November 6 privacy and content-truth increment connects allowlisted native summary/diagnostic exports and confirmed current-space deletion/recovery across React, Host and offline Gateway helper. Catalog intent precedes drain; exclusive identity locks and a durable sibling fence block pending/completed erasure at startup; credential/data cleanup, notification preference removal, fresh identity selection and persisted completion receipt are reconciled before restart. A separately enabled no-model Host/real Gateway acceptance verifies deletion, neighbour preservation and empty replacement reassembly. The native summary saved by the user exposed three bundled developer example memories. Product initialization/reset now creates no user memories; a product-owned provenance/revision-checked migration reversibly disables untouched examples after adapter registration, preserving edited/restored and ambiguous records. Python610/frontend116/Host17 and production frontend/debug Host builds pass; eight explicit layout/error scenes and32button contrasts reviewed. Native diagnostic-save/cancel, actual WebView deletion/restart, real vault and installed/fault acceptance remain open. No new installer, real-model call, daily-data mutation or cloud write. Evidence and failed-run boundaries are preserved in outputs/desktop-privacy-20261106/VERIFICATION.md. ADRs adr-product-example-memory-isolation-v1 and adr-desktop-privacy-erasure-recovery-v1. Earlier dated F25 backend-only wording is historical.

November 6 F17 follow-up adds memory.event.requeue as the ninth safe maintenance adapter. Owner-scoped rows include exact revision and consumer outcome. A selected failed/dead-letter event is previewed/confirmed through the existing native maintenance session; Memory atomically updates scheduling and audit, preserving payload/idempotency/cumulative attempts and granting three additional attempts. Duplicate revisions return the original audit, worker changes conflict, and uncertain results are reread. Normal product consumers reject stale projection snapshots/privacy policy or index tombstones conflicting with restored canonical memory before applying effects, recording rejected_policy. Queue scheduling and consumer application remain distinct in UI. Full Python617/frontend118 and5explicit queue layout/error/policy scenes pass, production frontend builds. This is source/API/fixture evidence, with matching sidecar/native-installed acceptance and broader cross-database consistency still open. Exact evidence and direct-bus compatibility limit are in outputs/desktop-queue-20261106/VERIFICATION.md; ADR adr-memory-single-event-recovery-v1.

The desktop release is not complete until:

Later October 11 bounded native follow-up: Windows UI tooling recovered and the same installed candidate completed one actual main-window trace.index.sync preview/confirmed execution/audit. A new real UI DeepSeek synthetic turn committed in 5203 ms, three foreground routes and one history pair; its Memory job completed first attempt, and actual inspector exposed measured job/HTTP/validation/Gate stages. Normal UI exit/restart preserved all twelve checked categories (29 turns, 40 history, 16 jobs, 174 cumulative provider calls). Original command audit remains cross-client readable but not executable. Fresh 52 frontend and 7 Host checks pass; read-only `installed-native-assertions.json` and original native screenshots are in `outputs/admin-console-20261011`. This supersedes the runner-blocker statement only for this sample, not all commands, races, soak or accessibility.

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

The repository virtual environment completed 431 Python tests on 2026-09-06, including Gateway, Desktop Contract, runtime, memory, companion-system, Provider, WorldOps and Module 10 coverage. The 27 Gateway tests were rerun after the final bridge-evidence wording fix. On September 7 all 22 Vitest checks and the TypeScript/Vite production build passed; the latest Rust Host run passed 5 tests. Generated desktop contracts are synchronized. Release claims must use this bounded verification evidence, not infer installed-app or real-provider production acceptance.

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

- remaining real-contract page wiring for safe export, whole-space deletion and custom endpoint flows,
- provider/model catalog and custom endpoint UX,
- Developer Mode typed-frame and preview/confirm/audit command bridges,
- remaining keyboard, screen-reader, reduced-motion, final console and target-hardware accessibility verification,
- Windows release packaging and operations.

### 13.3 Explicitly deferred

- desktop perception,
- camera/vision,
- voice,
- always-on Live2D/3D presence,
- multimodal behavior inference.

## 14. Prioritized Open Work

11月8日B3陪伴核心已交付r7：设置中可控制真实Persona成长/表达及Relationship学习/表达、拟定并确认或撤销限定相处约定、处理合法修复状态。开发者状态系统可读真实提案/贡献/来源指纹/审计，专用五分钟原生会话支持精确修订的拒绝及撤销重建，保护内容禁止盲批。Python654、前端138、Host18及2ignored，8个组件视觉场景通过；构建/实际安装Gateway的真实隔离写入与重启通过，实际Host/WebView读取对应保存状态。r7已安装，28,926,239字节，SHA256 ba9b2b82bd9882b453bead9f2c99520ea8bf88f1d5cac7b53080b33dd2bdba94，未签名。无外部模型请求或日常数据修改。证据 outputs/companion-controls-20261108/VERIFICATION.md。接续B4叙事，再B2记忆；完整治理/故障/发布边界保留。

Updated November8 at the user's request to deliver usable main functions quickly. The authoritative execution plan is [MAIN_FEATURE_PRIORITIES.md](../MAIN_FEATURE_PRIORITIES.md); current implementation/evidence remains in the F01-F30 ledger and DESKTOP_DELIVERY_STATUS.md. This planning change adds no implementation or test claims.

| Order | Delivery objective | Scope |
| --- | --- | --- |
| B1 / P0 | Daily conversation and history continuity | F10/F11 search/pagination/scroll continuity; fix actual provider discovery/send blockers first, validate existing F20/F28 routing rather than rewriting it |
| B2 / P0 | Effective and maintained memory | A03 semantic MERGE, A04 truthful packing budget, then managed periodic Lifecycle scheduling; preserve r6 atomic review and post-commit delivery |
| B3 / P0 | Understandable and controllable companion behavior | Existing Persona growth controls/explanation, Relationship agreement/repair/privacy surfaces, F19 real domain contribution/proposal governance and only its required F22 adapters |
| B4 / P0 | One real authored narrative participation loop | Verified signed content pack, event/task/choice, personal timeline and evidence-backed shared experience; content authoring/signing/service dependencies explicit, no fixture content passed off as production |
| B5 / P1 | Complete diagnostics, recovery and memory enhancements | F18/F30 per-worker tasks/attempts/failure coverage, F20 call drilldown, remaining safe F22, F16/F21 installed flows, dedicated protected-content review, F24 background response delivery, A05 full semantic consolidation |
| B6 / pre-release | Expanded validation and release readiness | Full F02/F23/F25 external/path/permission/fault/upgrade matrices, remaining F17 external faults, F24 MSI/notification coverage, accessibility, signing/update/migration/rollback/soak/SBOM/canary |

Each functional increment includes truthful turn/background timing, failure visibility, owner-scoped controls and necessary tests plus matching installer acceptance. B5 completes coverage across owners; it does not defer basic observability from B1-B4. B2 is two deliverable increments, not a prerequisite to finishing all A05 research. No reopening passed, unaffected r6 samples without new evidence. Privacy, identity, data-loss and inability-to-chat regressions preempt the sequence.

Existing formally P0 release blockers remain mandatory before release, but are not the day-to-day feature queue. Aggregate catalog rows must not be scheduled again in addition to their child tasks. Production vector/telemetry infrastructure and consented Beta calibration follow validated product needs; content distribution/editorial deployment is staged behind B4's minimum real content loop. Multimodal presence/voice/vision remains explicitly deferred.

See open-items catalog execution_batch/delivery_track fields for machine-readable scheduling; preserve each item's current status and historical evidence. No new cloud PRD publication or architectural ownership change is implied.

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
- Affect: `ZxQidBZGZopHbNxnpw3cXk2Unug` (title verified 2026-09-09)
- Relationship design: `MOceduqJroFb9VxkWhfc0gn1nhf`; `D0bfd7aYaoBlY7x1l0Hc3sCCng7` is the historical R3 code audit, not a design
- WorldOps design: `OJ4Ada3VSoolFOxspXpcRdrJnte`
- Model Provider/BYOK PRD: `XpWBdVgJKoVbBoxzWFacMxbXng5`
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
