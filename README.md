# Sora Agent State Sandbox

Python 工程验证原型，用来验证：

```text
Event Bus
-> Dialogue / Behavior / Memory / Persona / WorldOps / Relationship / Initiative 子系统 Signals
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
- 主动唤醒：REPL 内置空闲计时器，默认 3 分钟无输入时创建 proactive turn；Initiative 与 Arbiter 可以选择发出短消息或保持静默。

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

## 主动搭话

交互式 REPL 启动后会监听控制台输入活动。默认连续 180 秒没有用户输入时，
Scheduler 发布 `idle_timer` 事件并创建一个无用户文本的 proactive turn：

```text
Idle Timer -> Event Bus -> Subsystem Signals -> Initiative Window
-> Arbiter -> Response Planner -> Sora 主动消息或静默
```

主动消息仍受场景、WorldOps 内容源和 15 分钟最小间隔约束。`focus_work`、
未达到空闲阈值、冷却中或没有适合的话题时，Runtime 会静默且不调用 LLM。

快速验证：

```text
/set-behavior game_companion
/activate-world-event evt_echo_market_postbox_001
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

通用方式：

```powershell
python run.py
```

不需要真实 LLM 密钥的本地演示：

```powershell
python run.py --provider fake --once "你好呀" --trace
```

如果本机没有把 Python 加入 PATH，可以改用你自己的 Python 解释器路径运行。例如本工作区的 Codex 内置 Python：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py
```

开发时可先用 fake provider，不需要密钥：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py --provider fake
```

单次运行：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py --once "今天我们继续设计 Agent 编排器"
```

单次运行并显示旧 PoC 风格轨迹面板：

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run.py --provider fake --once "你好呀" --trace
```

## 常用 REPL 命令

```text
/help
/state
/trace
/trace-json
/state-diff
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
/memories
/memory-hooks
/commit-memory-hook <id>
/reject-memory-hook <id>
/add-memory 用户喜欢先讨论方案，再进入实现 --type preference --tags workflow,agent
/search-memory 方案
/persona
/traits
/set-trait playfulness 0.55 --debug
/growth-events
/apply-growth-event <id>
/reject-growth-event <id>
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

- Agent Runtime 不直接写长期记忆。
- LLM 不直接修改 Persona 长期状态。
- Memory Hook 必须手动 `/commit-memory-hook` 才写入长期 Memory。
- Growth Event 必须手动 `/apply-growth-event` 才修改长期 Persona trait。
- WorldOps 未被用户参与的事件不写共同经历。
- WorldOps 可以在非工作陪伴场景提供主动话题候选，但必须通过 Initiative Window 和 Arbiter。
- 主动发起话题必须短、低打扰，并在用户不接时自然收回。
- Persona 手动调参必须标记为 debug override。
- 短期 Session 优先级高于长期记忆和 WorldOps。
- 每轮输出保留 trace 和 state_diff。

## 测试

```powershell
& "C:\Users\aizen\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests
```
