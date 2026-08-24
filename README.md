# Sora Agent State Sandbox

Python 工程验证原型，用来验证：

```text
Event Bus
-> Dialogue / Behavior / Memory / Persona / WorldOps / Relationship / Initiative 子系统 Signals
-> Interruptibility Frame（回应义务、展开、追问、切题、主动性、状态外显预算）
-> Initiative Window
-> Arbiter
-> DeepSeek-backed Agent Runtime
-> response_plan / expression_event / state_diff
```

第一版采用真实 DeepSeek Chat Completions API。首次运行会询问 DeepSeek API Key，并保存到本机用户配置目录。Windows 下使用 DPAPI 加密保存；非 Windows 环境会保存为权限受限的本地配置文件。

本版参考了旧工程验证 PoC 的可观察性设计，但保留当前纯 Python、少依赖的实现：

- 两级耗时统计：Runtime phases（`event_bus`、`context_frames`、`subsystem_signals`、`initiative_window`、`arbitration`、`response_planner`、`expression_router`、`state_commit`）和 Signal timings（`sig_dialogue`、`sig_memory`、`sig_persona` 等）。
- 可读轨迹面板：展示用户输入、行为状态、召回记忆、Persona 状态、WorldOps 微事件、短期上下文、token 和耗时。
- 短期上下文窗口：保留最近 20 轮 user/Sora 对话，作为稳定上下文注入。
- 行为稳定性：模拟行为只有切换时标记 `changed=true`，被一轮消费后进入沿用状态，避免每轮重复强调场景。
- 输出清理：兜底移除动作/心理旁白式括号表达，保留技术括号内容。
- Prompt 外置：`prompts/*.md` 管自然语言 prompt，`prompts/prompt_registry.json` 管当前启用版本。
- 候选写入闭环：普通对话只产生 `memory_hook` / `growth_event` 候选，必须手动 commit/apply 才会写入长期 Memory 或长期 Persona。
- v0.4 逻辑并行架构：各子系统观察同一轮 Event Bus 并产出标准 Signal，Initiative 判断是否存在主动窗口，Arbiter 决定 selected/background/suppressed。
- v0.5 Origin Template 冷启动：首次状态创建时随机选择一个世界观人物模板，给 Sora 初始生活底色、trait 偏移、WorldOps 入口和主动性 bias。
- v0.6 可插拔子系统：Memory、Persona、WorldOps 等能力统一通过 `SubsystemRegistry` 和 Adapter 契约接入；支持并行求值、超时、单模块降级及 HTTP 服务替换。
- 本地 Memory Runtime：随 `run.py` 在同一进程启动，以 SQLite 为唯一事实源，提供 FTS 召回、evidence、candidate、revision 和 recall trace；不需要另起 HTTP 服务。
- 显式低风险记忆：用户明确说“记住……”时可自动提交；普通推断和 WorldOps memory hook 仍先进入候选。密码、API Key、token 等禁止持久化。
- 主动唤醒：REPL 内置空闲评估计时器，默认 3 分钟无输入时创建 proactive turn；当前 text-first 版本只有有效 activity lease 才允许进入 Initiative 与 Arbiter，否则保持静默。
- Text-first Interruptibility：用户控制优先于 Persona、Relationship、WorldOps、Affect 与模型建议；缺少 Presence/Behavior 证据时保持 `availability=unknown`，不猜用户是否忙碌。明确控制走本地快速路径，只有未决维度才允许调用受限低温 Provider。

## 子系统模块化

核心协议位于：

```text
src/sora_sandbox/contracts.py    # TurnContext、Signal 校验、StateCandidate
src/sora_sandbox/subsystems.py   # Adapter Protocol、Registry、本地/HTTP Adapter
SUBSYSTEM_API.md                 # 独立系统接入协议与示例
```

当前本地实现是默认 Adapter。未来独立开发好的 Memory、Persona 或 WorldOps
可以单独替换对应 Adapter，不需要修改 Event Bus、Arbiter 或 Response Planner。
每轮运行结果的 `context_pack.subsystem_health` 会记录各模块状态；REPL 使用
`/subsystems` 查看最近一轮的延迟和降级原因。

Memory 与其他可替换子系统略有不同：PoC 默认使用同进程的
`MemoryRuntime`，canonical 数据保存在 `.sandbox/memory.db`。HTTP 只作为未来
跨进程部署时的可选适配方式，不是本地运行的前置条件。

## 主动搭话

交互式 REPL 启动后会监听控制台输入活动。默认连续 180 秒没有用户输入时，
Scheduler 发布 `idle_timer` 事件并创建一个无用户文本的 proactive turn：

```text
Idle Timer -> Event Bus -> Subsystem Signals -> Initiative Window
-> Arbiter -> Response Planner -> Sora 主动消息或静默
```

当前生产入口采用 text-first 策略，不根据 Mock Behavior 假装知道用户是否忙碌。
主动消息必须先由用户显式邀请，例如“陪我聊会儿”，由此创建 session-scoped
activity lease。lease 在 15 分钟无用户交互后到期，只有新的合格用户回合会续期；
Sora 输出、后台事件和在线状态不会续期。quiet、response mute、显式撤销、会话结束
或安全策略会关闭 lease。没有有效 lease 时 proactive turn 保持静默且不进入生成阶段。

未来 Presence / Behavior 会作为带 provenance、confidence、TTL 和权限的独立证据
Adapter 接入；在此之前 `availability` 始终是 `unknown`。

快速验证 activity lease：

```text
陪我聊会儿
/simulate-idle 3m
```

## Prompt 管理

Prompt 不再嵌在 Python 代码里。当前文件：

```text
prompts/
  prompt_registry.json      # 当前 prompt 版本和文件索引
  system.md                 # Sora 稳定身份、边界、表达原则
  user_context.md           # 每轮 context_pack 注入模板
  dialogue_control.md       # 问句预算、自然收束、反模板化规则
```

REPL 中可以直接查看当前 prompt：

```text
/prompts
/prompt system
/prompt user_context
/prompt dialogue_control
```

修改 `.md` 后重新跑下一轮即可生效；如果要切换成另一套 prompt，可以改 `prompt_registry.json` 的路径和 `version`。

## 运行

Windows PowerShell 5.1 有时会把 UTF-8 中文输出显示成 mojibake（例如 `鐢ㄦ埛`、`璁板繂`）。这通常是终端输出编码问题，不代表仓库文件已经损坏。运行或查看中文文件前，可以先在当前 PowerShell 会话执行：

```powershell
$utf8 = [System.Text.UTF8Encoding]::new($false); [Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8
```

如果本机允许运行脚本，也可以执行 `. .\scripts\Use-Utf8Console.ps1`。

如果 `Get-Content README.md` 仍然显示乱码，改用项目内的 UTF-8 查看脚本：

```powershell
python scripts/read_utf8.py README.md
```

通用方式：

```powershell
python run.py
```

这条命令会同时启动 Orchestrator、Memory Runtime 和 REPL。进入主界面后输入：

```text
/memory
```

即可进入 Memory Console；输入 `back` 返回 Sora 主界面。

不需要真实 LLM 密钥的本地演示：

```powershell
python run.py --once "你好呀" --trace
```

如果本机没有把 Python 加入 PATH，可以改用你自己的 Python 解释器路径运行。例如本工作区的 Codex 内置 Python：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py
```

开发时默认使用真实 LLM，需要配置 DeepSeek/OpenAI-compatible 密钥：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py
```

单次运行：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py --once "今天我们继续设计 Agent 编排器"
```

单次运行并显示旧 PoC 风格轨迹面板：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py --once "你好呀" --trace
```

## 常用 REPL 命令

```text
/help
/state
/trace
/trace-json
/state-diff
/interruptibility status
/interruptibility set follow_up none --scope session
/interruptibility confirm <token>
/interruptibility revoke all
/interruptibility traces 20
/signals
/subsystems
/arbitration
/initiative
/proactive
/simulate-idle 3m
/set-idle-threshold 3
/origin
/origins
/set-origin origin_echo_market_wanderer
/reroll-origin
/show-history
/clear-history
/prompts
/prompt system
/memory
/memories
/memory-candidates
/memory-health
/recall-traces
/memory-hooks
/commit-memory-hook <id>
/reject-memory-hook <id>
/add-memory 用户喜欢先讨论方案，再进入实现 --type preference --tags workflow,agent
/search-memory 方案
/persona
/traits
/growth-events
/apply-growth-event <id>
/reject-growth-event <id>
/persona-health
/persona-worker --limit 20
/persona-contributions
/persona-rollback <contribution_id>
/persona-rebuild
/persona-forget-source <source_ref>
/persona-growth pause|resume
/persona-self-reference silent|implicit|explicit
/set-mood 平静
/set-energy 低
/worldops-events
/activate-world-event evt_starport_lamp_001
/start-micro-quest evt_starport_lamp_001
/complete-micro-quest evt_starport_lamp_001 --choice 写一盏送回星港的灯
/behavior
/set-behavior focus_work
/export-state
/exit
```

## 设计红线

- SQLite canonical store 是长期记忆的唯一事实源，`state.json` 只保留兼容调试投影。
- 普通对话不得直接写长期记忆；必须先形成 candidate。
- 用户明确要求记住的低敏偏好、目标和项目原则可以自动提交。
- 密码、验证码、API Key、token、私钥等禁止写入 evidence、candidate 或 canonical memory。
- LLM 不直接修改 Persona 长期状态。
- Memory Hook 必须手动 `/commit-memory-hook` 才写入长期 Memory。
- Growth Event 必须手动 `/apply-growth-event` 才修改长期 Persona trait。
- WorldOps 未被用户参与的事件不写共同经历。
- WorldOps 可以在非工作陪伴场景提供主动话题候选，但必须通过 Initiative Window 和 Arbiter。
- 主动发起话题必须短、低打扰，并在用户不接时自然收回。
- `response_obligation=forbidden` 只能来自白名单显式 response mute；persistent mute 和 persistent state-surface consent 必须二次确认，resume 始终可达。
- `love`、`dependency`、`loyalty`、`possessiveness` 及派生压力字段在 Interruptibility Evidence Aggregator 入口删除，不得进入规则、模型 prompt 或 trace value。
- 下游 Recall、Arbiter、Response Planner 和 Expression Router 只能收紧 Interruptibility Frame，不能自行放宽。
- Persona 手动调参必须标记为 debug override。
- 短期 Session 优先级高于长期记忆和 WorldOps。
- 每轮输出保留 trace 和 state_diff。

## 测试

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests
```

真实 DeepSeek 的脱敏 Interruptibility E2E：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/eval_interruptibility_live.py --output evals/reports/interruptibility/interruptibility_live_eval.json
```

## Persona System

Persona is an independent canonical subsystem backed by `.sandbox/persona.db`.
It receives committed `persona_delta` and `profile` memories through the Module
09 `ProjectionEvent.v1` boundary. Projection delivery is fast and idempotent;
real DeepSeek candidate evaluation happens on the asynchronous Persona worker.

The model may propose a target trait and evidence scores, but it never computes
or writes a trait delta. `persona_growth_policy.v1` applies the deterministic
Gate, multiplicative support, trait bounds, inertia, and 24h/7d/30d budgets.
Committed changes are append-only contributions. Rollback and Forget deactivate
contributions and rebuild snapshots instead of writing old values back.

Run the complete local suite and real-provider Persona E2E with:

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests
$env:PYTHONPATH="src"
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/eval_persona_live.py
```

## Relationship System

Relationship is a pair-isolated canonical subsystem backed by
`.sandbox/relationship.db`. It owns Sora-to-user relationship state,
epistemically labeled user-to-Sora estimates, mutual Agreement, and explicit
Repair episodes. Memory, Persona, Affect, WorldOps, and Interruptibility remain
separate owners and communicate through typed frames and domain events.

Committed Memory `ProjectionEvent.v1`, `InteractionOutcome.v1`,
`WorldOpsOutcome.v1`, and externally grounded `AffectOutcomeEvidence.v1` enter
an idempotent inbox. Typed evidence is handled deterministically; ambiguous
conversation evidence is processed by an asynchronous real DeepSeek extractor.
The model can only propose evidence candidates and Agreement amendments. Actor
scope, proposal paths, high-intensity semantics, Gate admission, confirmation,
novelty, conflict, bounded deltas, stage hysteresis, snapshots, and frames remain
deterministic. Agreement proposals expire after 15 minutes and never mutate
canonical state without the one-time confirmation challenge.

The response hot path reads a last-known-good `RelationshipFrame.v1` and makes
zero Relationship model calls. Relationship values never grant initiative,
memory disclosure, or permission to override quiet and other user controls.
High-intensity dimensions such as romantic love, attachment, dependency,
loyalty, jealousy, and possessiveness are modeled as state, not product targets.

Operational commands include:

```text
/relationship
/relationship-health
/relationship-worker --limit 20
/relationship-candidates
/relationship-contributions
/relationship-approve <candidate_id>
/relationship-rollback <contribution_id>
/relationship-rebuild
/relationship-learning pause|resume
/relationship-surface silent|implicit|explicit
/relationship-forget-source <source_ref>
/relationship-agreement-propose <term_path> <json_value>
/relationship-agreement-confirm <candidate_id> <token>
/relationship-consent-requests
/relationship-repair-open <trigger_ref> [dimension,...]
/relationship-repair-transition <state> [reason]
/relationship-dead-letters
/relationship-dead-letter-resolve <event_id> <retry|discarded|exported> [reason]
```

Run deterministic, live-provider, and 10k-ledger verification with:

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest tests.test_relationship_system -v
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/eval_relationship_live.py
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/eval_relationship_live.py --category agreement
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/benchmark_relationship.py
```

`FakeLLMClient` remains a deterministic test fixture only. Application bootstrap
attaches the Relationship, Persona, Affect, Interpreter, and Arbiter providers to
the configured real DeepSeek client; the Relationship hot path still performs no
model call.

## 记忆召回评测

生成隔离的模拟记忆库和黄金集：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" evals/generate_recall_dataset.py
```

运行 SQLite FTS 基线：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" evals/run_recall_eval.py
```

报告输出到 `evals/reports/sqlite_fts/`。评测使用
`.sandbox/evals/memory_eval.db`，不会污染日常 `.sandbox/memory.db`。

## Affect & Internal State System v2

Affect is an independent canonical subsystem backed by `.sandbox/affect.db`.
The synchronous lane is deterministic and makes no model call. Ambiguous
semantics are refined asynchronously by `LLMAffectAppraisalProvider` using the
configured real DeepSeek client with thinking disabled, strict JSON validation,
one repair attempt, evidence allowlisting, retry backoff, and dead-letter state.

```text
AffectStimulusEvent.v2
  -> deterministic fast appraisal (response hot path)
  -> append-only contribution + global AffectSnapshot.v2
  -> sanitized AffectFrame.v2 (bands only; no raw values or evidence refs)
  -> optional bounded RecallContextFeatures.v1 (<= 10% relative boost)

encrypted transient text
  -> async semantic proposal
  -> deterministic hysteresis and correction delta
  -> forward-only snapshot update

qualified emotion episode
  -> MemoryCandidateProposal.v1
  -> Module 03 Gate
  -> Module 04 canonical memory
```

Short-lived mood is not long-term memory. Only high-salience, repeated and
persistent, user-requested, relationship repair/rupture, shared milestone, or
WorldOps episodes can produce a governed memory candidate. Shared episode,
relationship, WorldOps, and Persona candidates never auto-commit.

Transient text uses Windows DPAPI on Windows and standard AES-GCM with a
process-lifetime key on other platforms. Evidence and merged-episode source
lookups use normalized hash indexes, so Forget and source invalidation do not
scan JSON payloads. SQLite keeps one serialized writer while runtime reads use
thread-local query-only WAL connections. Dynamics hardening parameters
(`factor_zero_policy`, mood residual caps, capacity-to-need coupling, and input
limits) live in the versioned Affect profile.

Set `SORA_AFFECT_MODE=active|shadow|disabled` for production rollout. `shadow`
persists and evaluates Affect state for comparison but does not influence the
prompt, Signals, recall, or memory writes. User and operator controls are
available through `/affect-*` commands and the Module 10 admin action registry.

Run deterministic and real-provider verification with:

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest tests.test_affect_system -q
$env:PYTHONPATH="src"
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/eval_affect_live.py
```

## Canonical WorldOps Narrative System

WorldOps now runs as a separate bounded subsystem instead of storing narrative
state in the legacy `state["worldops"]` dictionary. The content service owns
authored canon, immutable Ed25519-signed bundles, approval audit, releases, and
anonymous editorial contributions. Each client owns an isolated personal
timeline, user controls, task evidence, deterministic transition ledger, world
clock, forget tombstones, and a bounded `WorldOpsFrame.v1` for the dialogue
runtime. Production clients receive public trust keys only and never hold the
content signing private key.

`WorldOpsWSGIApplication` exposes the public release, bundle, anonymous
contribution, receipt, and withdrawal endpoints for deployment behind a
production WSGI server or gateway. `WorldOpsHttpDistributionClient` requires
HTTPS outside explicitly opted-in localhost tests, applies request timeouts and
response-size bounds, sends capability tokens only in headers or protected JSON
bodies, and supports release ETags.

Dialogue handling uses deterministic authored-choice matching first. The
bounded semantic provider is called only when an active or invited event is
ambiguous, cannot invent event or choice IDs, and runs with thinking disabled.
Shared-episode memory proposals and narrative contributions are asynchronous.
Outbox consumers use at-least-once delivery, persistent per-effect checkpoints,
stable source IDs, and downstream reconciliation. Anonymous contribution
receipt and withdrawal capabilities are encrypted locally before transport, so
a lost server response remains retryable and withdrawable.

The client rejects unsigned, expired, wrong-channel, wrong-cohort, revoked-key,
or lower-sequence bundles. Rollback is a higher-sequence signed compensating
release. Multi-device snapshots require authenticated transport, exact identity
scope, checksum validation, and non-divergent history. Module 10 registers
WorldOps eval, export, outbox, release refresh, rebuild verification, forget,
dead-letter, and observability-retention actions.

Run the production invariants and full regression suite with:

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest tests.test_worldops_system -q
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests
```

## Text Conversation Runtime

The default application entry now runs through the production text conversation
runtime. It provides encrypted turn journaling, stable request idempotency,
per-conversation execution leases, bounded model routing, provider cancellation,
reconnectable client events, atomic final commit, and asynchronous idempotent
post-turn delivery to Memory, Affect, Relationship, and WorldOps. Generation is
fail-closed behind the packed disclosure/token gate; ciphertext is key-ID aware,
and the packaged Golden Set has an executable trajectory harness.

Operational contracts, API examples, retention, recovery, and release
verification are documented in
[`docs/TEXT_CONVERSATION_RUNTIME.md`](docs/TEXT_CONVERSATION_RUNTIME.md).

## Live Text Conversation Gateway

The desktop-facing production boundary is implemented under
`src/sora_sandbox/conversation_gateway`. It provides scoped local capabilities,
strict versioned contracts, REST/SSE/WebSocket transport, durable request
idempotency, sequence-based reconnect, bounded backpressure, profile-isolated
runtime assembly, a typed desktop client, and a supervised child-process entry.

See [`docs/CONVERSATION_GATEWAY.md`](docs/CONVERSATION_GATEWAY.md) for startup,
API, recovery, security, observability, and release instructions.
