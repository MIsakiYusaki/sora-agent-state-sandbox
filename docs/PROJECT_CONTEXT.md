# Project Sora Context

用户最新顺序（2026-11-08）：**B3陪伴控制与真实领域治理 → B4叙事体验 → B2记忆效果与自动维护 → B1历史 → B5完整调试补齐 → B6发布扩大覆盖**。此前“下一批B1/F10/F11”计划已由此明确替换，编号和既有实现证据保留。

Last refreshed: 2026-11-08

Context revision: 35

This file is the compact, repository-local context for every new Project Sora task. It is intentionally more stable than a conversation summary. Verify volatile implementation details against current code, Git status, tests, and the canonical PRDs before making claims.

The detailed engineering source is `docs/context/PROJECT_LEDGER.md`. It contains the per-system plan, implementation and audit status, runtime paths, unresolved work, release criteria and maintenance policy. Machine-readable indexes live in `docs/context/catalog/`. The complete component-to-PRD reading index is `docs/PRD_INDEX.md`, including all ten Memory modules, six companion production designs and internal Runtime component ownership. Design availability does not change implementation or release status.

Every task must read this compact snapshot. A task that changes, reviews or plans a system must also read the corresponding section in the detailed ledger and its catalog record before making claims.

r10验收基线的父提交：`37756f3e3d20d6ff2464f039ed7616a3f12c2431`。2026-11-08用户明确授权归档当前代码到GitHub；推送前已fetch核对origin/main与此父提交一致。本次源码快照包含此前累计工作区实现，具体checkout提交以`git log -1`为准。安装/测试状态仍按各自证据区分，不因Git归档而成为正式发布。可在线阅读的快照与功能台账见`docs/releases/2026-11-08-r10.md`、`docs/DESKTOP_FUNCTIONAL_STATUS.md`；本机outputs、诊断/记忆导出和安装程序不提交。

New-task entry: `docs/START_HERE.md`. The authoritative current conversation wiring, exact execution sequence, asynchronous boundaries and integration gaps are maintained in `docs/architecture/CONVERSATION_DATAFLOW.md`. Other architecture diagrams are summaries or historical designs, not independent current dataflow specifications.

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
- LLM extraction with ADD/UPDATE/MERGE/DELETE mutations plus NOOP/REVIEW control outcomes,
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

Memory writes are deferred from the response hot path through the committed-turn outbox. The default Memory consumer passes the complete committed event to `CommittedTurnIngestion`, preserving source event, conversation, runtime turn, correlation and owner identity. A durable receipt, events, episode, rule evidence/candidates and extraction job are written atomically; Gate admission resumes without repeating ingestion. `SoraRuntimeStack` starts the managed `MemoryExtractionScheduler` after domain adapters are registered. It waits for an extraction route, recovers expired leases, checkpoints validated LLM results, and fences canceled/stale work before persistence. It stops before dependent domain stores close. See `docs/MEMORY_BACKGROUND_INGESTION.md` for contracts, operational settings and verification. Default periodic lifecycle heat/expiry/tombstone maintenance is managed by MemorySystem with persisted pause controls (Nov8 r9). Full semantic consolidation remains separate. Canonical mutation transactions now enqueue durable index/projection/listener work. A managed Memory delivery worker retries after commit and recovers after real domain owners register; it is not a separate indexing service. Periodic maintenance does not automatically approve semantic consolidation.

Current user delivery is `provider_buffered_then_validated_client_stream`: complete generation and deterministic constraints precede client deltas; atomic journal/outbox commit precedes the final committed event. Do not promise raw provider-token streaming or a global maximum of three LLM calls: optional Memory planner/rerank/compression and retries have their own invocation paths.

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

11月8日r10已安装：B1历史核心补齐，正文搜索/多页续查、旧会话实际最近活动排序、准确分页末尾、预览与对话阅读锚点、独立历史读取错误、同请求去重和A→B→A迟到回包隔离。普通列表只解密标题/末条摘要，不创建明文正文索引。Python692及前端154项通过，7个亮暗/紧凑/错误与滚动夹具通过；实际构建/安装Gateway验证105段会话、400条加密消息八页与重启；实际安装窗口完成正文搜索→原会话预览→继续原会话→加载前文并保留位置。包28,962,452字节，SHA256 c492940c8e5090f080b95a1d3822c57beb59d0f5cad4fdbb874378f34e403994，未签名。Host仅有预期UNK→NSS三字节打包标记差异，其他字节一致，Gateway符合manifest。无外部模型请求或日常数据修改；未重测新的真实模型续聊回合。证据 outputs/history-core-20261108/VERIFICATION.md。下一批B5优先补用户可用的主动陪伴/后台回应及深度记忆整理，所需真实调试处置随能力补齐；B4正式内容服务与发布依赖仍单列。

11月8日新增r9：B4客户端可从聊天信息参与真实签名故事选项、读取任务/个人时间线及实际共同记忆审核结果；共同经历的证据、候选、Gate入队同事务，WorldOps经历禁止自动提交。B2实现A03语义MERGE（摘要/标签/重要性、来源/基线检查、隐私保留及冲突回滚）、A04实际返回文本同口径预算，以及默认每小时最多100条的热度/到期/遗忘标记维护；记忆与隐私可查看结果并确认暂停/恢复。当前故事来源仍是明确标注的本机开发预览，新的灯笺内容源尚未正式发布，正式内容分发/编审/签名服务仍开放。r9已安装，28,958,485字节，SHA256 4d6aa3c95319be0b7762d808f7faef344265e1cfd367d14560cf3e45796a4384，未签名。验收与边界见 outputs/memory-effectiveness-20261108/VERIFICATION.md；无外部模型请求或日常数据修改。

2026-11-08用户要求优先尽快完成主要功能，执行顺序以[主功能交付优先级](MAIN_FEATURE_PRIORITIES.md)为准：B1历史/对话及实际模型阻断；B2语义MERGE、真实预算和Lifecycle周期调度；B3既有陪伴系统的用户控制及领域治理；B4真实叙事内容链；B5完整worker/失败/安全命令处置与A05增强；B6扩大验收及正式发布。各批附真实时间线/失败定位与匹配安装包；既有r6基线保留，缺陷和发布门禁不因排序消失。此项为计划更新，不是实现完成声明。

11月8日第一批P0记忆治理交付：审核批准/拒绝/延期与候选、正式记忆、投递意图和精确请求审计同事务提交；外部索引/领域投影/监听器在本地提交后投递，监听器持久化重试、过期来源阻断，真实领域注册后恢复。完整Python635项通过，新增16项故障/进程退出/重试一致性用例；实际r6安装窗口三种审核操作通过，待审核3→0且独立审计核对一致。另用合成凭据通过真实Windows凭据库的目标删除/邻居保留验收。r6未签名候选28,899,414字节，SHA256 `0cc8a44b021947817abaef54b4c74ecbd44443e731cb156dde769614789969f5`，已安装、manifest及程序完整性通过。无模型请求或日常数据操作。详见`outputs/memory-governance-20261108/VERIFICATION.md`。跨库为可恢复的至少一次投递，完整领域故障矩阵及其余F01–F30/发布门禁仍开放。

November 7 queue/status follow-up supersedes the earlier F17-native-pending statement. Real installed r4 WebView completed selected dead-letter preview, confirmation, atomic requeue/audit and FTS drain; final event is processed/applied with cumulative attempts2. UI now checks all six derived-queue counters, keeps missing values unknown, distinguishes canonical record reads from aggregate health, and fixes unreadable maintenance alerts. Matching unsigned r5 is28,891,896bytes, SHA256 a7114d84cebbf84e4a6a957162116d7cda202b5e22d0ad5b600f8b3c86de30eb; installed exit0 and integrity matches exact Tauri NSIS marker. Frontend131 and ten explicit browser scenes pass; Python619/Host18 remain unchanged r4 reports, not new reruns. Packaged Gateway checks pass, installed r5 reads the same applied event, and QA Memory/queue/audit metadata is identical across upgrade. Earlier installed privacy/space/notification evidence remains valid within its limits. No new model calls, daily data or real vault deletion. Cross-database consistency, remaining F01-F30 and release gates remain open. Evidence: outputs/desktop-queue-native-20261107/VERIFICATION.md and outputs/desktop-memory-status-20261107/VERIFICATION.md.

November6 source repair closes the two scoped Memory audit blockers. Public candidate commit rolls back local SQLite mutations when any batch step fails, preventing later unrelated writes from persisting a failed batch. Formal release gates block missing/unverified baseline comparisons while ordinary eval remains available to establish an initial baseline. Seven new regression cases and the current full Python suite pass598 in95.83s. Evidence: outputs/memory-blockers-20261106/VERIFICATION.md. Cross-database projection/listener effects, review-approval crash consistency, audit A03/A04/A05 and all unfinished client/export/notification/install work remain open. No new package or real-model request in this fix.

The production Tauri/React app now carries the accepted Quiet Companion Desk conversation surface while preserving the real trusted Host, Gateway and Runtime path. Streaming, cancellation, recovery/conflict states, native credential entry, provider configuration and typed IPC remain intact. Natural conversation controls are read from and written to the session-scoped Interruptibility contracts.

History is a real searchable conversation index with message preview and resume. The UI does not invent branches or expose internal conversation versions while no user-facing branch contract exists. Memory and Privacy reads governed memory summaries and performs real hide, restore and forget operations without showing confidence, sensitivity or lifecycle scores as a dashboard.

Developer Mode consumes an authenticated, `debug.read`-scoped, low-sensitivity `DesktopDebugSnapshot.v1` through Gateway, Tauri and React. Its eight production sections show per-turn timing/events/model calls, context-frame metadata, model routes, memory pipeline, async work, state systems, eval/release evidence and the controlled-command registry. Snapshots refresh two seconds after each completed read without overlapping polling requests; failures label retained snapshots as stale and late results from another conversation are discarded. Unknown metrics stay unknown, not zero or success. Profile identity is hashed. Database paths, credentials, raw prompts, hidden reasoning and arbitrary execution are excluded. Frame contents remain incomplete. Seven maintenance commands now have typed preview/confirmation/execution/audit bridges; this is not unrestricted REPL or complete L3/L4 support.

After the Windows browser ACL was repaired, the production client was reviewed at `1440x900` in light mode across Conversation, History, Memory and Developer Mode, at `1440x900` in dark mode for Conversation, and at `420x560` across the affected compact surfaces. No reviewed page had horizontal overflow. The pass corrected the dark Conversation identity contrast, removed the duplicated compact Memory empty state, aligned Settings copy with the real debug snapshot, and renamed the unsupported "History and branches" action to "Conversation history". The measured dark-mode Conversation identity contrast is 14.2:1. A real message produced seven visible Gateway events, while typed context frames and controlled commands remain accurately marked as pending bridges. The final duplicate console-log/reload pass was blocked by the Codex browser-use quota, so this evidence does not replace the remaining keyboard, screen-reader, reduced-motion and target-hardware accessibility checks.

The September 6-7 repair additionally corrected user-message alignment, compact status-label wrapping, diagnostic tag/scale contrast and the legacy/new CSS token mismatch that made the dark conversation sidebar unreadable. Explicit Mock transport was used only for browser layout verification, not real-provider or installed-app evidence. The checked 1440x900 light/dark and 420x560 layouts had no horizontal overflow. On September 7 the repaired diagnostic status tag measured 8.50:1 and timeline scale 8.65:1; the checked browser console had no warning/error entries. Full screen-reader, reduced-motion and target-hardware verification remain open.

Runtime journal schema v2 preserves missing token usage as NULL, supports input/output aliases, retains explicit new zero values, and migrates ambiguous legacy runtime zeros to unknown. Migration DDL, table replacement and version updates are transactional; injected failure after replacement rolls back and reopening safely retries. New Provider traces and Gemini adaptation also preserve unknown usage. Historical Provider-registry traces and unrelated backend cost estimators were not globally rewritten.

Verification: 22 Vitest tests and the TypeScript/Vite production build passed on September 7; the full Python suite passed 431 tests on September 6, with 27 Gateway tests rerun after the final bridge wording change; 5 Rust Host tests passed and contracts are synchronized. The September 7 NSIS candidate is 28,631,573 bytes, with a 24,860,409-byte Gateway sidecar matching its integrity manifest. Installer SHA-256: `07aacb5c2461e420611db802ef8797a430cd876a6af80a15cdee46990ae62b2b`. It is unsigned and has not received installed-app/real-provider E2E acceptance. Production dist contains no Mock transport markers, prototype seed strings or source maps.

September 14 monitoring update: the local production client now reads real nested execution spans, pre-generation L0 signal/decision evidence, post-turn consumer attempts and owner/conversation/turn/correlation-scoped Memory extraction jobs. Memory attempts expose queue timestamps, retry backoff, checkpoint availability and measured provider/validation/Gate phases. Observations never change canonical scheduling or retry results. Content-free attempt metadata is stored in Memory's own database, filtered to seven days and physically pruned on the next observation write; it is not the encrypted conversation artifact store. Missing or interrupted evidence stays unknown. Identical duplicated interpretation is removed only from the generation projection to fix the observed second-turn control-budget overflow; independent controls remain enforced.

September 14 verification supersedes the older counts above: 478 Python tests, 28 frontend tests, 5 Rust Host tests, TypeScript/Vite and NSIS build passed. An authorized isolated real DeepSeek sample committed both turns and completed both Memory jobs on their first attempt. Packaged sidecar startup, unauthenticated debug rejection, authenticated debug snapshot and graceful shutdown passed without model calls. The new unsigned candidate is `outputs/turn-observability/Project-Sora-0.1.0-20260914-monitoring-x64-setup.exe` (28,670,034 bytes; SHA-256 `7c0013fade85d7f22eabbbe0f2a4e7a0e991dc1fe65d15140499775c416a5638`). Browser review covered real recorded timeline replay at 1440x900 light/dark and 420x560, keyboard expansion/focus and injected error state. Other replay-harness panels remain explicit layout fixtures. No reviewed overflow or console error was observed. This is not installed Host/WebView real-provider E2E. See `outputs/turn-observability/CONVERSATION_WALKTHROUGH.md` and `VERIFICATION.md`.

September 16 monitoring update: actual HTTP open/read/close lifetimes now join the existing turn or Memory-attempt collector at the registry Broker and legacy chat/extraction/embedding/recall/rerank/surface exits. Extraction JSON repair is a separate parent phase. Stream scopes do not leak their parent across generator yields, and early close is interrupted. The authorized isolated two-turn sample recorded six completed HTTP calls and both Memory jobs completed on attempt 1. These are client request lifetimes, not wire-only latency, library-internal redirects/retries, provider TTFT or WebView paint. The frontend layout is unchanged. The unsigned candidate is `outputs/provider-http-observability/Project-Sora-0.1.0-20260916-monitoring-x64-setup.exe` (28,677,453 bytes; SHA-256 `202b1c8e4f2821fff80067cdcd74edf2e5810fe7a4a70b151320556ef9640faa`). Package build, 28 frontend/5 Host tests and isolated packaged Gateway smoke passed. Ten focused request-observation tests pass. One full regression recorded an intermittent Interruptibility gate failure; the original report is preserved, the threshold was not changed and its exact cause was not captured. See `outputs/provider-http-observability/VERIFICATION.md` for all runs and limitations.

Latest September 16 serial full regression: 488 passed, zero failures/errors/skips, 76.31 seconds (`outputs/provider-http-observability/pytest-serial-20260916.xml`). This successful rerun does not establish the cause or resolution of the earlier intermittent gate failure.

September 20-21 installed acceptance: the actual installed WebView/Rust Host/packaged Gateway/DeepSeek chain now has isolated long-path conversation, persisted-history restart, Memory job/attempt/HTTP timeline, and idle Gateway crash recovery evidence. Two installed defects were fixed: Windows profile atomic writes beyond MAX_PATH and external-to-runtime conversation identity mapping for Memory timeline association. The current unsigned installed candidate is `outputs/installed-e2e-20260917/Project-Sora-0.1.0-20260920-acceptance-x64-setup.exe` (28,674,523 bytes; SHA-256 `a915c60b59e16f9350db10883b5e7148d38961860e0dc2c3a840485be4851c52`). September 20 Python full regression: 500 passed, no failures/errors/skips, 86.968 seconds. The final isolated sample contains six committed turns, twelve history rows and five completed first-attempt Memory jobs. Recovery was idle and triggered by the next Host request, not a mid-turn or soak test. UI cancellation still has no valid cancelled sample. A new acceptance failure is a long reply ending mid-sentence while marked committed, with completion_tokens=1200; finish-reason/validation handling needs diagnosis, not an arbitrary budget increase. See `outputs/installed-e2e-20260917/ACCEPTANCE.md` for exact evidence, failed attempts and open gates. Daily data and credentials were not exported; native credential-dialog entry was not tested.

September 22-23 update supersedes the truncation diagnosis above: foreground streaming now requires a recognized normal terminal before returning buffered content; incomplete, missing, conflicting or unrecognized terminals fail before response deltas/history/outbox, retaining reported usage. EOF cancellation is checked; non-stream compatibility delegates remain outside this strict stream contract. Full Python regression passed 519 tests. Installed DeepSeek turns `turn_30aa960024bb4492a185fa107bc03fe6` and `turn_79b443a04a4f412aae9dc7a763b7c1bb` failed with `IncompleteModelResponse`, no response delta, no canonical history or outbox for those turns. Normal generation still commits. Budgets/retry routes were not changed. The September 22 failure-feedback candidate adds a safe Chinese alert, failed user marker and non-destructive draft restore; the installed marker is observed, but the alert was not captured before a late stop-position click resubmitted the restored synthetic draft. The subsequent successful turn is a separate request, not automatic retry. Esc stop and no-output cancelled markers are now implemented with 41 frontend tests passing; installed verification is pending. The latest installer, hashes, exact sample counts and open gates are maintained in `outputs/installed-e2e-20260917/ACCEPTANCE.md`. No cancelled sample is verified yet.

September 23 installed follow-up: the keyboard-stop candidate was actually installed (NSIS exit 0; installer SHA256 `64afe6bfd7e72002971aa23b9622ef45d61fc18970a257b1ab7b603423c5bb58`). The safe Chinese incomplete-response alert, failed user marker and restored draft are now captured together in `41-incomplete-alert-draft-20260923*`, superseding the pending alert evidence above. Three Esc attempts still ended as `IncompleteModelResponse`, not verified cancellation. They added no deltas/history/outbox/Memory jobs; these are failure-path assertions, not proof of cancellation. Final isolated metadata records 16 turns (11 committed, 5 failed), 22 history rows and 10 completed first-attempt Memory jobs. Stop blind UI retry attempts; cancellation needs correlatable receive/token/terminal timing. Exact evidence and remaining gates are in `ACCEPTANCE.md`.

September 24 update: Gateway public cancellation and per-turn timeout now preserve the authenticated target turn ID; coordinator identity matching prevents a late old-turn request from cancelling the next turn. Encrypted content-free cancellation receipts distinguish observed status and changed token from the final terminal. Runtime diagnostics expose those receipts, but desktop presentation and Host/WebView timing are not added. 525 tests passed in the full regression, followed by 9 focused tests including three additional cases. The rebuilt candidate `Project-Sora-0.1.0-20260924-cancel-receipt-x64-setup.exe` installed with exit 0 (SHA256 `84a04d54cbb539fc02f9a9ed8cac98ade60b9bc27ff04a5ebbbac63b6dc77873`). Two real UI turns committed and their Memory jobs completed; final isolated sample is 18 turns (13 committed, 5 failed), 26 history rows, 12 completed jobs and no cancellation receipts. In the measured text-observation attempt, the next observation occurred 18.49 seconds after commit; no cancellation was then issued. This proves a missed interaction window for that attempt, not that backend cancellation failed. Valid installed cancellation remains pending timely user input or lower-latency real UI control. This does not establish the cause of September 23 unsuccessful Esc attempts. See `ACCEPTANCE.md`, `adr-turn-specific-cancellation-v1` and `docs/CONVERSATION_GATEWAY.md`.

September 28 supersedes the pending user-assisted cancellation above: the agent autonomously sent Esc through the actual installed UI, without API-only substitution, model/budget changes or artificial delay. `turn_b7ccf024a62542d1bb574c1ee72acbf3` has a generating → cancel_requested receipt, changed token and authoritative cancelled terminal 2874 ms after runtime receipt. No response delta, successful history, outbox, Memory source receipt or extraction job exists for that turn. Actual conversation UI shows stopped and Developer Mode shows its cancelled execution tree. A subsequent real UI turn committed, its Memory job completed first-attempt, and normal restart preserved identical turn/history/outbox/Memory/Provider records without resubmission. Final isolated sample: 21 turns (15 committed, 5 failed, 1 cancelled), 30 history rows, 14 completed jobs. Fresh tests: 41 frontend and 28 cancellation/completion tests passed. Same September 24 installed candidate and hashes; no production code changed this follow-up. Evidence and read-only assertion runner: `outputs/installed-e2e-20260917/ACCEPTANCE.md`. This is one generation-phase Esc sample, not all cancellation races, low-latency automation reliability, mid-turn crash or soak; receipt rendering and Host/WebView timing remain open.

September 29 installed follow-up: a real generating-phase Gateway crash exposed a stale turn left generating after Host restart and an English unmarked client error. The repaired Runtime/Gateway/Host/React path now runs lease-fenced stale recovery every ten seconds after the existing five-minute threshold, treats SSE EOF without a terminal as failure, and shows a Chinese incomplete-turn alert while retaining the draft for explicit user choice. The final unsigned candidate is `outputs/installed-e2e-20260917/Project-Sora-0.1.0-20260929-stale-stage-x64-setup.exe` (28,679,294 bytes; SHA256 `1722d9dfcbd6e818b37110a7c5c4898272fb39f9a8e8762fee722b12c137b054`). It was installed with exit 0; actual WebView fault injection in the isolated profile ended as `failed / stale_recovery`, with no success history, outbox or Memory source/job for the interrupted turn. A five-minute no-input minimized-window interval produced no new turn; a different short message was then deliberately submitted and committed. Normal restart preserved all checked metadata. One earlier retry interval produced a separate committed turn of uncertain input provenance and is retained as an unresolved observation, not counted as proof of no auto-retry. Python full regression passed 530 tests; prior frontend 43/Host 5 tests and final package manifest verification passed. The complete evidence and limits are in `ACCEPTANCE.md`; this is a bounded mid-turn recovery sample, not soak or production release.

October 10 recovery hardening: deterministic cross-connection tests reproduced lease renewal or turn refresh between stale selection and failure. The failure write now atomically rechecks terminal status, stale timestamp and live matching lease; lease acquisition validates conversation/turn identity and renewal excludes terminal turns. Full regression passed 533 tests. New unsigned installed candidate `Project-Sora-0.1.0-20261010-atomic-recovery-x64-setup.exe` is 28,667,221 bytes with SHA256 `2b28a2d8a5e6a09b5bc830d2d61a4551409dac970dbd1b69b95e88edeac263b9`; NSIS install exited 0. Explicit no-model failure fixtures verified the actual installed Gateway's startup and periodic recovery, live-lease exclusion, singleton terminal, no successful history/outbox, health/auth and normal shutdown. Archived Sept29 real-UI metadata assertions passed; same-profile upgrade restored identical pre-upgrade records (28 turns, 38 history rows, 15 Memory jobs). New candidate WebView interaction checks remain pending because the Windows UI runner fails initialization with a file-sharing error while validating node_repl.exe, not corrupted sandbox JSON. Full evidence: `outputs/installed-e2e-20260917/ACCEPTANCE.md`; ADR `adr-stale-recovery-atomic-fence-v1`. The fixture service check is not new real-model UI acceptance.

October 11 maintenance bridge: `eval.run`, `eval.replay`, `eval.metrics.collect`, `eval.release_gate`, `trace.index.sync`, `memory.index.drain` and `worldops.rebuild.verify` now invoke real Module 10 adapters. Preview request identity and hash are durable; execution claims once before effects, rechecks revision/actor/role/parameters/impact and never auto-retries uncertain effects. Five-minute native-only sessions bind profile/subject/agent/owner/origin/client and actual Tauri main window. Capability and confirmation internals do not enter WebView; same-profile redacted history is readable across clients, but only the preview's client can execute. Unadapted schemas remain null and L3/L4 destructive/export actions remain disabled. Full Python regression: 543 passed (82.232 s); frontend 52 and Rust Host 7 tests passed; production build and exact light/dark/compact/uncertain fixture rendering passed. Actual packaged and installed Gateway preview/one trace-sync execution/audit/idempotency/revocation/shutdown checks passed with zero model calls. New unsigned installer `outputs/admin-console-20261011/Project-Sora-0.1.0-20261011-admin-bridge-x64-setup.exe` is 28,705,434 bytes, SHA256 `8ab1ef2ef8f3ca4273cccbb84e9ae00889db5937cb28b8cab57c2556f8123ca9`, installed with exit 0. Same isolated profile restart preserved all twelve checked metadata categories (28 turns, 38 history rows, 15 jobs, 165 cumulative provider calls). Native UI runner still fails initialization; no new native GUI command E2E or new real-model conversation is claimed. Evidence and limits: `outputs/admin-console-20261011/VERIFICATION.md`; ADR `adr-desktop-maintenance-commands-v1`.

Later October 11 supersedes the native-runner blocker for a bounded sample: the Windows tool recovered. The same installed candidate was exercised through actual WebView input/main-window Host IPC: trace.index.sync preview/explicit confirmation/execution/audit, then one authorized DeepSeek synthetic turn `turn_77c1a108cfe84807bf6caa5d587f3d64` committed in 5203 ms with three foreground routes, one history pair and delivered outbox. Its Memory job completed attempt 1 and the real inspector displayed job/attempt/HTTP/validation/Gate stages. Normal UI exit/restart preserved twelve metadata categories (29 turns, 40 history rows, 16 jobs, 174 cumulative provider invocations). The new client can read the original command audit but cannot execute its preview; no automatic session reopen. Read-only assertions and original native screenshots: `outputs/admin-console-20261011/installed-native-assertions.json` and `VERIFICATION.md`. Fresh frontend 52/Host 7 pass. This is one native command and one normal conversation, not all commands/cancellation/crash/soak/accessibility/release gates.

October 15 bounded delivery: the existing October 11 frame candidate is frozen as `outputs/context-frames-20261011/Project-Sora-0.1.0-20261015-context-frames-x64-setup.exe` (28,736,896 bytes; SHA256 `a1dce8d96175aedca5bf8f791ebeeb4a25b095455f5cc9d8cc4527593b26bc8f`) and installed successfully with previous program files backed up. Typed on-demand allowlisted frame values, source stages, budget and permitted-field comparison are implemented; generation-sealed collection defaults off and explicit `debug.observe` enables it for five minutes. Historical snapshots are not labelled generation input. Existing 557 Python/56 frontend reports pass, seven Host tests were rerun October 15, packaged and installed no-model maintenance smoke pass, and four frame-layout fixture scenes were reviewed without overflow/runtime exceptions. Isolated upgrade records remain identical (29 turns/40 history/16 jobs/174 provider calls). New native GUI frame/real-model/restart acceptance was NOT completed; older admin-candidate native samples do not prove this new path. Full handoff and limits: `outputs/context-frames-20261011/DELIVERY.md`. User requested delivery without further scope expansion; do not automatically start additional worker-monitor features.

October 28 functional-repair follow-up (explicitly authorized after the bounded handoff): the F01-F30 audit remains the finite repair scope. Current source adds Memory review sessions, queue inspection/projection drain, private generation-sealed response basis, revision-checked correction/provenance, persistent defaults/limited quiet controls and Provider route/removal flows. F21 now has a five-minute native-held quality capability, real cases and read-only baseline comparison, and owner-transactional event resolution with revision checks, atomic audit and idempotency. F16 adds explicitly confirmed quarantine reject-and-dismiss, with candidate/quarantine revision check and Gate audit in one transaction; it never releases protected content or commits it to memory. Failed reads and late history/frame/quality/governance responses do not fabricate empty/success states. Full Python regression passes 585, frontend 95 and Host 9; evidence and per-item limits are maintained in `outputs/desktop-functional-repair-20261028/VERIFICATION.md` and `outputs/desktop-functional-repair-20261015/PROGRESS.md`. These are source/isolated tests, with quality/quarantine component browser layout review; no new installer, native installed acceptance or real-model request in this repair batch. F18/F19/F22-F25 and partial per-item gaps remain open. The earlier fixed installer does not contain this repair batch.

October 29 status sync: [DESKTOP_DELIVERY_STATUS.md](DESKTOP_DELIVERY_STATUS.md) separates current source, tests, installed package and finite F01-F30 remainder. F23 Host-owned space creation/confirmed graceful switch/restart has one isolated debug-native sample; managed desktop spaces disable automatic legacy-secret migration. F24 has WinRT/scoped quiet preferences, but prior Setting returned 0x80070490; new NSIS AUMID registration is not installed/verified, MSI equivalent absent. F25 has offline owner-scoped erasure/runtime fencing, not a connected Host/UI deletion/export flow. Fresh frontend107 pass (stale notification-placeholder assertion updated), Host13 pass/1 diagnostic ignored, Python6 focused pass; full Python585 remains the October28 pre-increment baseline. Rust minimum1.89. No new installer or model request. The earlier approval-service quota did not block today's approved tests. Feishu user authorization was renewed and all six overview/status documents were patched and read back: progress81, architecture180, navigation277, dataflow38, Desktop PRD136, Memory151. Scope is current implementation/status, not full PRD audit; original requirements/diagrams remain. Evidence: outputs/client-status-sync-20261029/CLOUD_SYNC.md and cloud-readback-verification.json. Implementation streams stay active.

This does not mean full installed acceptance or desktop release has passed. Independent non-Memory worker attempts, per-request usage/cost, internal transport retries/TTFT and Host/WebView timing remain incomplete. The main remaining client and release work is:

1. complete keyboard, screen-reader, reduced-motion, final console and target-hardware accessibility verification,
2. verify the new allowlisted context-frame path in the installed native UI; remaining safely adapted commands/L3/L4 and broader native command coverage are still open,
3. finish safe export, whole-space deletion, custom endpoint and other explicitly unavailable user flows,
4. extend installed cancellation race coverage, repeated real-provider/sidecar mid-turn recovery soak and target-hardware performance checks,
5. complete signing, updates, N-1 migration, rollback, SBOM, canary and Beta gates.

Do not claim that the desktop is "not started". Do not claim that an unsigned installer candidate is a production-complete release.

November 6 privacy increment connects safe summary/diagnostic export through a native Save File receipt and the current-space erasure Host/UI/recovery chain. Durable catalog intent and a sibling offline-erasure fence block partially erased identities across Host/Gateway restart. User-saved isolated native summary exposed three developer example memories in fresh spaces. Default/reset/compatibility initialization now starts without user memories, and product composition reversibly retires only proven untouched example imports after adapters register. Edited/restored or ambiguous records are preserved. Current Python610/frontend116/Host17 pass, with a separately enabled real Host/Gateway erasure/reassembly sample and eight explicit fixture layout scenes. The summary save was completed by the user. No real model, daily-data deletion, new installer or installed erasure acceptance is claimed. Exact evidence and remaining limits are in outputs/desktop-privacy-20261106/VERIFICATION.md. F01-F30 and release gates remain open.

November 6 queue follow-up connects the ninth safe maintenance adapter, memory.event.requeue, to a selected failed/dead-letter row with owner/revision checks and atomic scheduling/audit. It preserves payload/idempotency/cumulative attempts and adds three attempts. Normal product projection/index consumers reject superseded snapshots or stale index deletion against current canonical state, and the UI separates scheduling from actual consumer outcomes. Full Python617/frontend118 and five explicit queue preview/error/policy layout scenes pass; TypeScript/Vite build passes. Matching sidecar and installed queue recovery remain pending. Evidence and compatibility boundaries are in outputs/desktop-queue-20261106/VERIFICATION.md. The preceding privacy610/116/17 results remain a dated source baseline.

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

All 28 indexed PRDs/overviews were audited against the current working-tree implementation and refreshed on 2026-10-29. The guarded update corrected 110 body blocks, distinguished historical implementation records from current contracts, and preserved target requirements, references and architecture boards. Per-document changed-text/resource/link readback passed. Current readback revisions include overall182, unified-dataflow40, Desktop145 and Memory154; the complete version table is `outputs/prd-refresh-all-20261029/COMPLETION.md`, with machine-readable results in `verification.json` and the artifact catalog. Earlier Desktop133/134 and partial October29 snapshots remain historical evidence, not the latest design baseline. This documentation synchronization does not complete F01-F30, installed acceptance, encryption coverage, lifecycle scheduling or release gates. If implementation and PRD differ, identify whether the difference is an intentional implementation advance, an unfinished migration, or a regression before editing.

Unified dataflow v1.4 mirror: https://qcnqo2xrtj31.feishu.cn/docx/Wl2cdE1y7oP4zaxIVAycQV2pnKe . Current cross-system wiring is maintained in `docs/architecture/CONVERSATION_DATAFLOW.md`. The September9 publication audit is preserved in `docs/context/baselines/conversation-dataflow-20260909.json`; the historical v1.1 Memory P0 implementation, 462-test regression, real-model smoke and document readback are recorded in `docs/context/baselines/memory-background-ingestion-20260910.json`, not a fresh full-suite result.

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
