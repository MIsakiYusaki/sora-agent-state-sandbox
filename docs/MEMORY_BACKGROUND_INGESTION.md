# Memory 后台抽取与提交来源接线

11月8日第一批P0记忆治理交付：审核批准/拒绝/延期与候选、正式记忆、投递意图和精确请求审计同事务提交；外部索引/领域投影/监听器在本地提交后投递，监听器持久化重试、过期来源阻断，真实领域注册后恢复。完整Python635项通过，新增16项故障/进程退出/重试一致性用例；实际r6安装窗口三种审核操作通过，待审核3→0且独立审计核对一致。另用合成凭据通过真实Windows凭据库的目标删除/邻居保留验收。r6未签名候选28,899,414字节，SHA256 `0cc8a44b021947817abaef54b4c74ecbd44443e731cb156dde769614789969f5`，已安装、manifest及程序完整性通过。无模型请求或日常数据操作。详见`outputs/memory-governance-20261108/VERIFICATION.md`。跨库为可恢复的至少一次投递，完整领域故障矩阵及其余F01–F30/发布门禁仍开放。


版本：1.0。更新日期：2026-09-10。状态：两个 Memory 接线 P0 已修复于当前工作区，尚未提交或推送。

## 1. 已解决的两个问题

过去提交后分发器只把两段文本交给 Memory：它会创建 episode 和 job，却没有默认抽取调度；conversation/turn/event 身份也在此处丢失。现在产品组装入口启动受管理的抽取 worker，Memory 消费完整的 committed-turn 事件。无须每轮从 REPL 手动执行抽取。

本页是运行契约与故障处理说明。跨系统总图仍以 `docs/architecture/CONVERSATION_DATAFLOW.md` 为唯一维护源；本次没有把 Lifecycle 周期调度或独立索引调度标为完成。

## 2. 实际执行顺序

```text
ConversationRuntime: 校验回答 -> Journal/Outbox 提交 -> response.completed
  PostTurnDispatcher: conversation.turn_committed
    CommittedTurnIngestion: 校验来源身份及内容指纹
      SQLite 事务: receipt + events + episode + rule evidence/candidates + extraction job
      可恢复 Gate 准入: 复用候选 ID，不重新提取规则候选
      receipt = completed -> 唤醒调度器
    MemoryExtractionScheduler: 配置检查 -> 原子领作业 -> 租约心跳
      查询当前来源/相关记忆 -> LLM -> schema/evidence 校验 -> 写入决策校准
      结果 checkpoint -> 原子 materialize -> 原有 Gate -> 完成 job
        Canonical / Index Outbox / Projection Bus -> 下一轮使用
```

抽取网络请求不占用回答热路径，也不持有 Memory 的共享 SQLite 锁。相关记忆查询遵循现有 Module 05 实现；其内部检索锁策略不在此承诺为完全无锁。Index/Projector 当前仍可在记忆变更时同步 drain，因此本次并非将所有下游成本搬到了独立调度器。

## 3. 来源身份与幂等

| 字段 | 来源与用途 |
|---|---|
| source_event_id | Journal outbox 的 event_id；不是重新生成的随机对话 ID |
| conversation_id | 已接受整轮的 conversation_id；写入 Memory event/episode 的 session_id |
| turn_id | runtime_turn_id，与 outbox 顶层 turn_id 交叉校验；不使用会话内数字轮次 |
| correlation_id | 保留在来源 receipt、证据 metadata 和 extractor 输入来源信息中 |
| owner_user_id / agent_id | 已绑定 Memory 身份；显式来源不一致则拒收 |
| payload_hash | 完整 payload 的规范 JSON SHA-256；同来源不同内容失败，不覆盖旧事实 |

`memory_turn_receipt` 对 source_event_id 和 owner/agent/conversation/turn 组合分别约束唯一性。同一事件重投返回原 receipt 结果；不同会话或不同轮次即使文字相同也分别保留。Memory 自身生成的 user/assistant event ID 仍用于 evidence span，raw_ref 指回真正的来源事件。

事件、episode、规则证据、候选、job 与 ingested receipt 在一个短事务中写入。Gate 在后续可重试步骤执行；在 receipt 完成前，调度器不领取对应 job。Gate 后、Dispatcher receipt 前崩溃，也只复用原候选，不生成第二批证据。这里承诺的是 Memory 域内幂等，不是多个领域数据库之间的全局 exactly-once。

默认 Gateway 使用真实用户身份。旧 REPL 会话没有 user_id 时，Runtime 使用已绑定 Memory 的 owner，而不是另一个固定用户名；显式 user_id 不会被偷偷改写。直接调用旧的两参数 observe_turn_candidates 仍是兼容/手动入口，不能用它冒充已携带生产来源的入口。

## 4. 调度、重试与恢复

| 设置 | 当前默认值/语义 |
|---|---|
| start_workers | SoraRuntimeStack.build 默认 True；False 同时关闭领域 worker、Memory 调度和提交后分发线程 |
| poll_seconds | 1 秒；收到新 committed-turn 会主动唤醒，不必等待下一次轮询 |
| worker 数量 | 每个 MemorySystem 一条受管理线程；多实例共享 DB 时 SQLite 原子 claim 防止重复领取 |
| lease_seconds | 120 秒，约每 40 秒续租；测试可注入更短值 |
| 最大尝试次数 | 3；过期租约恢复也计入累计 attempts，达到上限转 failed |
| 失败退避 | 首次 60 秒，随后 120 秒；计算上限 900 秒，是否可再次领取受 available_at 控制 |
| 不可恢复输出 | schema/evidence 校验失败或来源不可用，不重复消耗模型请求 |
| 无抽取路由 | waiting_provider；不领取 job，不增加 attempts；之后配置路由即可继续 |
| 关闭等待 | 默认等待 30 秒；仍有调用则抛出 TimeoutError，保持依赖数据库和 Provider 打开 |

由 Model Provider 管理的产品实例每次检查 `memory_extraction` workload 路由，不会越过它回退环境变量。独立 legacy MemoryRuntime 才保留原有真实环境变量 provider 兼容入口。测试 provider 只用于测试，不会作为产品默认回退。

正常启动会扫描已有积压；没有新消息也会恢复处理。运行中的任务有持久化 lease token 和 expiry，旧 worker 在写 checkpoint、materialize、Gate 准入与最终完成时都要再次验证租约。模型长调用通过独立心跳维持租约；无法续租则不得晚到提交。

模型结果经过验证和写入决策校准后保存 checkpoint。重试读取原结果及原相关记忆快照，不重新发起随机 LLM 决策；新的 evidence/candidate ID 由 job 和候选序号稳定导出。materialize 事务失败会回滚证据和候选，Gate 提交完成后重试也不会再次增加 canonical revision。

## 5. 删除、隐私和关闭边界

用户取消或删除来源后，迟到的模型结果不能继续 materialize。每个写入阶段检查当前 episode 和来源 events，不信任模型请求发出时的旧快照。来源删除同时使运行中/待运行 job 取消，并清理其 checkpoint。checkpoint 不重复保存原 episode，完成和终止状态会清理派生结果快照。

来源 receipt 不存对话正文；删除后保留身份和指纹以抵抗 outbox 重放。清空整个 Memory store 的 reset 会清理 receipt。保留期限若以后调整，必须与上游 outbox 的最大重放窗口一并设计，不能只按年龄删除 receipt 后允许旧事件重新学习。

已知秘密类别在进入 committed-turn Memory 队列前被拒收，包括 API key 的空格/下划线/连字符写法。本次没有宣称关键字检测能识别所有秘密或代替完整安全系统。新 worker 错误诊断只保存异常类型，不保存可能包含正文或凭据的 provider 异常全文。

停止顺序是先停止 ConversationRuntime，再等待 Memory 抽取 drain，随后关闭 WorldOps、Relationship、Affect、Persona、Memory 和 Provider。超时不假装已经停止，也不在后台线程仍使用连接时强行关闭数据库。

## 6. 使用与排查

正常用户使用已有桌面或 REPL 启动方式，并在 Model Provider 中配置抽取 workload。正常回复不等待长期抽取；候选是否自动进入长期记忆由原有 Gate 决定，needs_review 是合法结果。

工程调用由已构建的 `stack.memory.extraction_scheduler.snapshot()` 获取状态，包含 running、status、processed、last_error、updated_at 和各状态 queue 数量；Module 10 导出新增 `memory_extraction_scheduler` 区段。它不输出对话正文和密钥，也不自动暴露给普通用户界面。

| 观察到的情况 | 处理方式 |
|---|---|
| waiting_provider + pending | 配置或修复 memory_extraction 路由；不需要重发原对话 |
| pending 且 available_at 在未来 | 正在退避，等待；不要循环强制重试造成重复费用 |
| running，进程已退出 | 新进程在租约过期后恢复；无须手工把所有 running 改成 pending |
| failed | 查看脱敏错误类型，修复 provider/schema/来源问题后，使用现有 backfill/治理入口定向重建作业 |
| completed + needs_review | 抽取已经成功，等待 Gate 审核；不能当作没有记住而反复抽取 |
| source replay conflict | 排查发送方身份/事件复用，不能覆盖 receipt 以绕过检查 |
| stop 超时 | 保持依赖存活，待 provider 调用退出后再次关闭；进程强制退出后的恢复交给租约机制 |

不要直接编辑生产 SQLite 来重置状态。数据库迁移是追加表/字段，已有 evidence、canonical memory 和已完成 job 不会因升级被删除或自动全量回填。

## 7. 验证与剩余边界

专项测试：`tests/test_memory_background.py`，覆盖 24 项身份、真实 Runtime 接线、原子回滚、Gate 前后崩溃、多连接、租约、重复重试、迟到取消、关机保护和动态路由行为。普通自动化测试使用受控测试 provider，验证时序与失败路径；真实模型另行执行，二者不混称。

2026-09-10 真实模型冒烟使用配置中的 `deepseek-v4-flash`，只发送专门构造的测试对话，数据库为临时目录：1 次尝试、10.45 秒完成、1 条 LLM evidence、1 个 needs_review 候选、0 次越过 Gate 的强行提交，来源 turn_id 校验通过。该单样本不是性能 SLO、准确率报告或真实用户数据评测。

完整 Python 回归为 462 passed、0 failed、0 skipped，耗时 75.35 秒；两条非失败警告为测试依赖弃用提示与 pytest 缓存目录 ACL。验收摘要、源码指纹和飞书回读记录见 `docs/context/baselines/memory-background-ingestion-20260910.json`；上下文治理命令记录于 `ws-memory-background-ingestion-p0-20260910`。未改变前端页面；没有重新验证安装包签名/更新/目标机器验收。本轮也不包含 Lifecycle 常驻周期调度、索引/Projection 独立调度服务，以及所有领域之间的全局事务。

## 8. 11月8日r9周期维护接线

MemorySystem在领域适配器注册完成后，与抽取/派生投递一起管理MemoryLifecycleScheduler，关闭时先等待维护停止，避免数据库仍被使用。默认启动等待30秒、每小时一批最多100条，按最久未评分顺序选取；到期处理、热度状态和遗忘标记维护及周期checkpoint同一Memory事务。暂停状态/最近结果/下次时间持久化，重启不会重复已完成周期；失败保留脱敏异常类型，至少60秒后重试。

GET/POST `/v1/memory/maintenance`经Host固定命令、身份/权限及明确确认/配置修订校验；普通记忆页面按需读真实结果。暂停只针对周期维护，不阻断显式Forget、隐私控制或抽取审核。诊断导出`memory_lifecycle_scheduler`保留真实run_id、耗时和结果计数。默认不调用LLM、不自动运行/批准语义巩固，完整A05仍单列。验证见`outputs/memory-effectiveness-20261108/VERIFICATION.md`。
