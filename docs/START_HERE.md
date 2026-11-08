# Project Sora 新任务接续入口

刷新：2026-11-08。先确认正在操作 `sora-agent-state-sandbox`，不要新建第二个客户端或运行时。

## 1. 五分钟建立正确上下文

1. 阅读仓库 `AGENTS.md` 与 `docs/PROJECT_CONTEXT.md`，确认产品定位、用户约束、正式桌面与原型的区别。
2. 执行下面的只读命令，报告 cwd、HEAD、dirty状态、活动任务。别把同项目不同worktree当成同一份文件。
3. 阅读 `docs/architecture/CONVERSATION_DATAFLOW.md`。它是当前跨系统接线源；旧PRD图不覆盖最新代码事实。
4. 先用 `docs/PRD_INDEX.md` 按职责找到正式PRD和内部组件归属，再按 `docs/context/catalog/systems.json` 找代码、测试，并查 `PROJECT_LEDGER.md`、`open-items.json` 和最近workstream。
5. 用一句话明确“本次关闭哪一个缺口、在哪个所有者内实现、如何验收”，然后begin工作流。

```powershell
Get-Location
git rev-parse --show-toplevel
git log -1 --format="%h %s"
git status --short --branch
.venv\Scripts\python.exe scripts/project_context.py status --check
```

## 2. 根据任务选择入口

| 任务 | 先看哪里 | 不要做什么 |
| --- | --- | --- |
| 对话链路/延迟 | 统一数据流、runtime_stack、conversation_runtime、agent | 把Gateway当成领域编排器；漏算Memory辅助调用 |
| 客户端 | apps/desktop、desktop-design-prototype/DESIGN_SYSTEM.md、desktop PRD | 重新搭客户端；让Mock取代真实Host/Gateway |
| 记忆 | 十模块目录、Memory设计与对应审计、dataflow第5/6节、MEMORY_BACKGROUND_INGESTION运行契约 | 因为抽取已自动调度就推断Lifecycle及索引也有常驻调度 |
| Persona/情绪/关系/叙事 | 对应系统canonical设计、独立代码目录、projection/adapters | 把外部系统实现塞进MemoryRuntime |
| 发布/状态 | 当前Git、已封板workstream、安装/测试证据、open-items | 把历史通过数、未签名包或设计稿当作发布完成 |
| 文档同步 | artifacts.json里的已核对URL和revision | 用审计报告替代设计；凭旧标题猜Affect/WorldOps |

## 3. 当前应带入的事实

11月8日r10已安装：B1历史核心补齐，正文搜索/多页续查、旧会话实际最近活动排序、准确分页末尾、预览与对话阅读锚点、独立历史读取错误、同请求去重和A→B→A迟到回包隔离。普通列表只解密标题/末条摘要，不创建明文正文索引。Python692及前端154项通过，7个亮暗/紧凑/错误与滚动夹具通过；实际构建/安装Gateway验证105段会话、400条加密消息八页与重启；实际安装窗口完成正文搜索→原会话预览→继续原会话→加载前文并保留位置。包28,962,452字节，SHA256 c492940c8e5090f080b95a1d3822c57beb59d0f5cad4fdbb874378f34e403994，未签名。Host仅有预期UNK→NSS三字节打包标记差异，其他字节一致，Gateway符合manifest。无外部模型请求或日常数据修改；未重测新的真实模型续聊回合。证据 outputs/history-core-20261108/VERIFICATION.md。下一批B5优先补用户可用的主动陪伴/后台回应及深度记忆整理，所需真实调试处置随能力补齐；B4正式内容服务与发布依赖仍单列。

11月8日新增r9：B4客户端可从聊天信息参与真实签名故事选项、读取任务/个人时间线及实际共同记忆审核结果；共同经历的证据、候选、Gate入队同事务，WorldOps经历禁止自动提交。B2实现A03语义MERGE（摘要/标签/重要性、来源/基线检查、隐私保留及冲突回滚）、A04实际返回文本同口径预算，以及默认每小时最多100条的热度/到期/遗忘标记维护；记忆与隐私可查看结果并确认暂停/恢复。当前故事来源仍是明确标注的本机开发预览，新的灯笺内容源尚未正式发布，正式内容分发/编审/签名服务仍开放。r9已安装，28,958,485字节，SHA256 4d6aa3c95319be0b7762d808f7faef344265e1cfd367d14560cf3e45796a4384，未签名。验收与边界见 outputs/memory-effectiveness-20261108/VERIFICATION.md；无外部模型请求或日常数据修改。

11月8日第一批P0记忆治理交付：审核批准/拒绝/延期与候选、正式记忆、投递意图和精确请求审计同事务提交；外部索引/领域投影/监听器在本地提交后投递，监听器持久化重试、过期来源阻断，真实领域注册后恢复。完整Python635项通过，新增16项故障/进程退出/重试一致性用例；实际r6安装窗口三种审核操作通过，待审核3→0且独立审计核对一致。另用合成凭据通过真实Windows凭据库的目标删除/邻居保留验收。r6未签名候选28,899,414字节，SHA256 `0cc8a44b021947817abaef54b4c74ecbd44443e731cb156dde769614789969f5`，已安装、manifest及程序完整性通过。无模型请求或日常数据操作。详见`outputs/memory-governance-20261108/VERIFICATION.md`。跨库为可恢复的至少一次投递，完整领域故障矩阵及其余F01–F30/发布门禁仍开放。

11月7日队列恢复与状态真实性接续已完成：r4实际安装窗口完成单条死信的修订预览、确认、重新入队、审计及真实FTS消费，最终已应用、累计尝试2。随后修复死信1/pending0误标绿色、主存储误用整体健康及维护错误提示不可读，生成并安装r5未签名候选，28,891,896字节，SHA256 `a7114d84cebbf84e4a6a957162116d7cda202b5e22d0ad5b600f8b3c86de30eb`。当前前端131项及10个明确视觉夹具通过；后端未改，Python619/Host18沿用此前r4报告。打包Gateway检查通过，实际r5窗口可读原恢复事件，升级前后QA记忆/队列/审计元数据一致。无新模型请求或日常数据修改。F17本条索引事件安装恢复链通过，跨数据库一致性、其余F01–F30和发布门禁仍开放。详见`outputs/desktop-queue-native-20261107/VERIFICATION.md`及`outputs/desktop-memory-status-20261107/VERIFICATION.md`。

正式桌面已接真实Host/Gateway、历史、记忆和对话控制，调试也已有时间线/按需帧/受控维护桥；历史安装版有有界真实模型、取消、故障恢复、命令与重启样本，完整发布仍未通过。客户端新任务先读[当前交付状态](DESKTOP_DELIVERY_STATUS.md)及[F01–F30台账](../outputs/desktop-functional-repair-20261015/PROGRESS.md)：本批质量/审核/来源与路由修复已有源码回归，多空间有debug原生切换样本，隐私导出/删除Host/UI/恢复已接；通知安装实测、隐私原生及安装剩余验收、领域治理/worker和其他部分缺口未闭合，旧安装包不含本批。Memory自动抽取与来源身份两个P0已修复，Lifecycle默认周期调度现由11月8日r9接通，见最新报告。统一数据流定义接线，不用历史测试数代替当前验证。

11月6日增量已接安全导出和整空间删除Host/UI/持久恢复，用户完成一次隔离原生摘要保存，真实Host/Gateway隔离删除与新身份组装通过。导出发现3条开发种子记忆误入新空间，已移除默认注入，并对可证明未经编辑的旧示例做可恢复停用迁移。Python610/前端116/Host17及8个隐私视觉场景通过，该阶段原生安装待验的记录由下述11月7验收报告更新，真实vault仍待验，详见`outputs/desktop-privacy-20261106/VERIFICATION.md`。

当前本地HEAD为37756f3，存在多个任务未提交代码；远端本次未fetch核实。另一worktree或新机器无法自动得到未提交修改。活跃任务只更新它声明的路径；共享文档由文档任务统一维护，禁止覆盖他人未完成结果。

11月6日队列增量将F17所选失败/死信接入精确修订、预览/确认和原子重新入队审计，正常消费者拒绝过期投影与旧tombstone。当前Python617/前端118及5个队列视觉场景通过，安装待验，详见`outputs/desktop-queue-20261106/VERIFICATION.md`。

## 4. 事实与目标冲突时

用户决策/当前PRD决定“应该做什么”；当前代码及可复现验证决定“现在做到什么”。遇到不一致，登记为待实现、设计升级或回归，不要静默选其中之一。历史审计描述审计时点，不代表今天仍未修复，也不证明今天的改动已经验收。

本仓库共享工程记录，不共享模型的私有聊天记忆。已开始的任务不会因文件变化自动重读；改范围和提交前必须再查status/diff。任何尚未落入仓库/PRD的聊天决定都要先沉淀为ADR或待讨论项，才可声称其他任务可以恢复。

## 5. 完成交接

finish记录必须含changed_paths、实际运行的验证及剩余风险。架构/状态变更同时更新snapshot、ledger、catalog和ADR；同步飞书要读回验证。只有用户授权才commit/push，只有真实发布门禁通过才写“已发布”。命令用项目.venv，不因系统Python缺包而误判项目不可运行。

飞书凭据依赖桌面用户DPAPI。所有需要身份的CLI读写保持同一桌面上下文；token_missing先检查上下文，桌面验证有效就不要重复授权。必要时只给授权链接，不生成二维码；大段XML走原生exe和相对@file，不经过.cmd参数转义。
