# Memory 十模块：PRD 与当前代码一致性审查

11月8日r9增量：A03语义MERGE与A04预算已修复，默认Lifecycle热度/到期/遗忘标记周期调度已接并有持久暂停/恢复。下面A03/A04复现保留为历史缺陷依据，不能再当作当前未修复清单。A05完整近邻/图聚类/LLM巩固及真实tokenizer校准仍开放。证据：`outputs/memory-effectiveness-20261108/VERIFICATION.md`。


11月8日第一批P0记忆治理交付：审核批准/拒绝/延期与候选、正式记忆、投递意图和精确请求审计同事务提交；外部索引/领域投影/监听器在本地提交后投递，监听器持久化重试、过期来源阻断，真实领域注册后恢复。完整Python635项通过，新增16项故障/进程退出/重试一致性用例；实际r6安装窗口三种审核操作通过，待审核3→0且独立审计核对一致。另用合成凭据通过真实Windows凭据库的目标删除/邻居保留验收。r6未签名候选28,899,414字节，SHA256 `0cc8a44b021947817abaef54b4c74ecbd44443e731cb156dde769614789969f5`，已安装、manifest及程序完整性通过。无模型请求或日常数据操作。详见`outputs/memory-governance-20261108/VERIFICATION.md`。跨库为可恢复的至少一次投递，完整领域故障矩阵及其余F01–F30/发布门禁仍开放。


核查日期：2026-10-30。范围：十份现行模块PRD全文、当前工作区实现及默认组装入口。HEAD为37756f3，存在未提交代码；本次不代表远端、部署实例或旧安装包。逐份PRD版本和源码指纹保存在outputs/memory-prd-alignment-20261030。

2026-11-06修复增量已经通过源码回归。A01公共commit的本地SQLite异常事务隔离、A02缺基线阻断已修复，新增7项专项及当前全量598项通过。复现、范围和历史失败报告见`outputs/memory-blockers-20261106/VERIFICATION.md`。原审计正文保留当时结论，A03/A04/A05与跨数据库副作用、审核崩溃一致性继续开放。安装包验收仍待完成。

## 1. 结论

**主干方向一致，但不能说“十份PRD已与代码完全一致”。** 文档同时包含最终目标、早期契约、历史验收和当前实施说明；代码已有真实服务，却仍有部分职责留在MemoryRuntime、条件启用的模型能力、未实现的目标算法，以及可以复现的契约缺口。

因此读不懂不只是读者的问题：仅在每篇开头补“做什么”，还不能消除后面的字段名、操作含义和完成状态冲突。此前开篇更新不等于这次全文/代码一致性审计。下面分别列出文档应澄清的地方与代码需要补齐的地方，不通过降低目标来宣布一致。

本文所说的“一致”只在明确子范围成立：逻辑职责一致，不等于物理代码已经拆好；有适配器不等于当前启用；正常路径测试通过，不等于异常情况下满足事务与发布要求。

## 2. 当前真实链路

```text
回答当前问题：
用户文字/权限 → 06决定是否及怎样查 → 05混合检索/融合/重排
             → 07呈现与记忆预算 → Runtime整轮上下文/回答生成

提交后形成新记忆：
Journal成功提交 → Outbox分发 → 01来源/episode/job
 → 02受管理异步抽取 → evidence/candidate物化 → 03 Gate
 → 04逻辑上的正式事实提交
 → 05派生索引 / 09领域投影 → 后续轮次使用

生命周期与治理：
访问/反馈/时间 → 08显式运行服务 → 巩固候选走03，正式变化传播05/09
用户隐藏/纠错/forget → 各自受控入口，不必先经过对话LLM
10横向提供评测/trace/管理/发布检查，不是每轮最后再调用一次模型
```

物理入口是MemorySystem/MemoryRuntime及其协作服务；逻辑上的04提交目前主要仍在MemoryRuntime.commit中。当前只确认02抽取有默认受管理调度，不把08、索引和投影一并说成独立常驻服务。

## 3. 逐模块对照

| 模块 | 代码已经怎样做 | 与PRD一致的部分 | 尚不一致或容易误解的部分 | 判断 |
| --- | --- | --- | --- | --- |
| 01 来源与片段 | CommittedTurnIngestion接完整成功事件，形成receipt、事件、episode、规则候选与job | 来源身份、重复投递、防晚到删除和可恢复接收主链已接通 | 旧表仍会写“02下一模块/worker待做”；原始证据加密与完整多源产品入口未完成 | 主干一致；历史描述和安全目标须分开 |
| 02 抽取与写入提案 | worker校验/repair/校准/checkpoint，ExtractionDelivery先物化再Gate | 真Provider协议、异步租约、四操作提案与治理边界一致 | 个别旧表写NOOP/REJECT，实际是NOOP/REVIEW；确定性校准主要是词法相似与冲突cue，不是完整embedding/NLI评分体系 | 主干一致，部分目标和枚举描述超前/过时 |
| 03 Gate | 确定性准入、独立quarantine、review/defer、候选hash与受控处置 | 候选不等于事实；目标、敏感性、证据与组合阈值在Gate判断 | auto_committed是Gate返回结果，不是当前持久候选状态；MERGE与异常事务语义未达到文档完整承诺 | 部分一致，存在真实实现差距 |
| 04 正式存储 | MemoryCanonicalStore负责证据关联/审计/修复；Runtime负责大量commit/revision/status/rollback | SQLite正式记录是事实源，索引可重建，四操作有具体分支与回滚路径 | 不是已完整独立的CommitEngine；公共commit异常路径不保证自动rollback；原始证据并未全加密 | 行为主干存在，但模块拆分与原子性不完整 |
| 05 索引与检索 | FTS/vector/temporal/graph-lite、RRF、重排、outbox/rebuild/trace | 混合检索及真实外部适配不是stub；写入侧也复用Planner | 真实embedding/rerank依赖路由；默认可退确定性hash向量/特征排序；不是所有索引任务独立调度 | 框架一致，运行配置与生产验收有条件 |
| 06 召回规划 | 规则/Hybrid计划、子查询、write_related_memory、prefetch cache与trace | 先决定是否查，再生成查询与预算；不负责最终披露 | service_budget_ms/rerank_budget_ms主要是计划字段，不是全链截止执行器；预取不等于Next-Scene预测已完成 | 核心一致，预算执行/预测仍是目标边界 |
| 07 呈现与预算 | SurfacePolicyEngine + MemoryContextPacker，受控同意/隐藏/反馈与压缩 | 查到不等于能说，记忆包与整轮包分层存在 | §8旧action名与实际v2不同；五池整轮预算主要在Runtime；极小预算可出现计数截小而内容仍超量 | 部分一致，协议说明和预算实现需处理 |
| 08 演化与遗忘 | heat v2.1、分层、明确运行、互补/矛盾候选、tombstone/deep recall | 热度与真伪/披露分离，forget优先，巩固经Gate | 实际分桶+文本相似，不是PRD的ANN+局部图聚类/LLM巩固；无默认周期调度；trace联动未完成；§7.2仍残留旧公式 | 当前实现明显低于完整目标 |
| 09 外部投影 | Bus事件/ack/checkpoint/retry/rebuild，Stack注册三个真实领域adapter | 不拥有Persona/Relationship/WorldOps事实，撤销/重建有协议 | 独立构建的Bus可回退ContractProjectionAdapter，不能把契约回执等同于真实领域应用；独立调度/跨服务恢复待验收 | 产品组装边界基本一致，运行形态需分清 |
| 10 质量与运维 | suite adapter、trace index、baseline、Admin、Release Gate和有限桌面桥 | 不是另一个LLM；治理仍由所有者执行，八个维护适配可查 | 无baseline时仍可能返回passed，与“必须baseline diff”冲突；全域观测与生产运维未完成 | 核心设施存在，但发布判定有真实缺口 |

## 4. 可复现的实现差距

### A01 / P1：公共commit失败后可被后续写入意外提交

PRD03 §7/§11和PRD04 §8要求memory、证据、候选、revision与索引任务不能部分成功。当前MemoryRuntime.commit有锁和最后commit，但没有自身的异常rollback保护。注入“更新candidate为committed前失败”，此前插入的正式memory仍留在连接事务中；后续普通add_evidence提交该连接时，这些失败操作的早期结果也被持久化。

临时库实际结果：异常后in_transaction=true；另一独立写入后，新连接读到memory增加1条，但candidate仍为pending。这是实现缺口，不是把文档中“事务”换个词能解决的。

代码：src/sora_sandbox/memory_runtime.py:1726（commit）。范围限定：这是没有外层rollback包装的公共提交路径；ExtractionDelivery.transaction本身有异常rollback，不能把本次发现扩大成所有异步抽取都必然部分提交。领域外部副作用的一致性还需单独验证。

### A02 / P1：没有baseline，发布门禁仍可通过

PRD10 §5.3/§11要求Release Gate比较明确baseline。compare_baseline在无活动baseline时返回skipped/no_active_baseline；run_release_gate只把failed差异加入blockers，skipped没有阻断。

临时库注册一个确定性、本地通过的fixture suite后，无baseline的结果为status=passed、baseline_diff.status=skipped、blockers=[]。这是验证门禁控制流的测试，不是运行真实产品质量评测。

代码：src/sora_sandbox/memory_quality_ops.py:710、:789。缺baseline应阻断正式发布，或被显式区分为bootstrap/非发布检查，不能与已完成基线比较共用passed含义。

### A03 / P2：MERGE只追加证据，没有完成增强摘要的语义

PRD03 §7明确要求MERGE追加evidence并更新summary/importance/tags。当前commit的MERGE分支只插入memory_evidence、记录revision并刷新派生状态，没有写入这些增强字段。

说明性临时样本：目标“用户周末喜欢茶”，候选“用户周末喜欢不加糖的茉莉花茶”。提交后证据增加1条，但summary和tags保持旧值。若业务只想佐证同一结论，evidence-only可成立；但它不满足目前PRD对互补信息合并的承诺。

代码：src/sora_sandbox/memory_runtime.py:1726内MERGE分支。应实现经治理的摘要增强，或另行评审区分MERGE_EVIDENCE与语义MERGE；本次不擅自改变既定目标。

### A04 / P2：Memory预算计数不等于内容真的装得下

PRD07 §10/§18要求预算可靠。当前Packer使用字符启发式估算；当首条内容仍超量时，有分支把item_tokens取min(budget, estimate)，并未同时保证实际内容被裁到该估算内。

极小边界样本token_budget=1，返回token_used=1，但用同一个估算函数重算gist、why_recalled及同一固定开销得到23。重复压缩还可给短文本反复追加省略号。这个样本不是模型实际tokenizer测量，但已经证明“计数不超”不能作为“同口径内容不超”的证据。

代码：src/sora_sandbox/memory_context_packer.py:108及后续回退分支、:282（估算器）。当前整轮GenerationContextPacker是另一层防护，不能据此声称此局部预算问题不存在；也不能把局部发现直接扩大为全部模型请求必超窗口。

### A05 / P2：08的巩固算法不是文档中的ANN/局部图聚类

PRD08 §8.0.1明确要求粗筛、复用05的embedding/vector近邻、局部图聚类及预算；§8.3还规划真实LLM巩固。当前_candidate_pairs按type/tag/term分桶，只比较2–40条的桶；_plan_pair使用规范文本相等、冲突规则和token Jaccard，未调用vector index、图连通分量或LLM巩固Provider。

因此它能做有限重复/矛盾候选规划，但不能写成已实现语义近邻聚类、跨记忆pattern提炼。简单分桶的规模约束也不能被描述为已有ANN生产复杂度保障。

代码：src/sora_sandbox/memory_lifecycle.py:289、:850、:873。周期调度与trace联动则是另外两个已知缺口，不因修复聚类自动完成。

## 5. 文档自身的冲突和职责误读

### D01：三个不同位置的“四操作”不是三套操作系统

02提出操作意图；03决定该候选是否准入；04在逻辑所有权上保存变更。当前物理提交代码仍主要在MemoryRuntime里。PRD里未来的commit_engine.py、CommitResult.rollback_token等不能直接当作当前可调用类或返回字段。

另外，02一处仍写NOOP/REJECT，实际提取器枚举为ADD/UPDATE/MERGE/DELETE/NOOP/REVIEW；REJECT是Gate决定，不是Extractor枚举。03的auto_committed是Gate结果，而memory_candidate持久状态为committed。

### D02：05、06、07不是三次各做一遍召回

06生成计划；05的MemoryRetrievalService实际调用planner后执行检索、融合和重排；07选择呈现策略并打包。05文档内的Planner/Packer框是协作调用点，不意味着05又拥有一套独立规划与披露规则。

同样，07的记忆内部预算池不是整个模型窗口的全部预算；system/recent/domain/controls/response reserve由Runtime的GenerationContextPacker整合。两层都存在，但不能把最终整轮责任全部写到07。

### D03：07的动作名和字段示例不是当前接口清单

§8表仍使用inline_gist、answer_memory、defer_review、block；当前SurfacePolicyEngine主要使用answer_memory_question、quote_memory、relationship_context、suppress等名称。memory_context_frame_v2又不同于旧示例surface_budget_v1。权限示例也混有不同阶段的命名。

设计示例可以保留，但必须标为目标/旧协议，并列出实际生产者与消费者的字段映射；否则读者照抄会调用不存在的动作。特别是allow/silent_only/deny概念模型不能无说明地当作所有store字段当前允许值。

代码：src/sora_sandbox/memory_surface_policy.py:555、:627；memory_context_packer.py:154；memory_gate.py中的权限生成。

### D04：08同时保留了两套“推荐公式”

§7.0新增v2.1，§7.2代码块却仍是旧权重和惩罚项，读者无法知道哪套是当前规则。代码score_memory的0.20/0.13/0.15/0.11/0.12/0.10/0.07/0.06/0.06正项及0.10/0.08/0.06惩罚，与v2.1一致。旧式应移为明确历史对照或统一为v2.1，不应两套并列作现行规则。

代码：src/sora_sandbox/memory_lifecycle.py:246。注意“数值与v2.1匹配”不证明实际用户热度分布已校准。

### D05：“独立模块”是架构目标，不是已经完成的物理拆分

当前memory_runtime.py共3974行、MemoryRuntime有190个方法，仍承载propose、materialize、commit、rollback、状态变更和质量入口等；MemoryCanonicalStore并没有独立接管全部提交。代码已拆出多项服务，不能说全是stub；也不能说所有权已完全落实到独立实现边界。

本次保持代码不动。后续拆分应围绕已验证行为与事务边界，不重搭第二个Memory系统。

## 6. 已有但需要配置的能力，不应错报为“没有做”

真实embedding、Qdrant、HTTP cross-encoder和LLM rerank都有实现适配。MemoryRuntime根据workload绑定决定是否使用真实Provider；没有相应路由时可使用DeterministicEmbeddingProvider或FeatureMemoryReranker。前者是hash词法特征向量，不是神经语义embedding，也不是一次LLM调用。

因此正确说法是“外部能力已适配，是否启用与实际质量要核配置并验收”，不是“已全面部署生产语义检索”，也不是“全部仍是fake”。本次没有读取用户密钥或发请求去确认个人实例的在线服务。

09在正式SoraRuntimeStack中注册真实Persona/Relationship/WorldOps adapter；独立使用MemoryRuntime/Bus时可保留ContractProjectionAdapter。检查某个独立测试的契约快照，不能据此认定默认产品路径没有真实领域；契约ack也不能冒充外部领域的业务效果已验收。

## 7. 本次验证与限制

新鲜读取十份PRD，读时revision依次为41、36、27、52、33、26、27、24、17、30。来源全文、线性文本与源码SHA256见审计目录；之后的文档澄清版本记录在写入回执中。

执行23项既有隔离测试：23通过，0失败、0错误、0跳过。覆盖01来源/作业、02worker、03审核/hash、04回滚、05检索/重排适配、06计划/预取、07同意/隐藏、08归档/遗忘/矛盾、09回执/重试。测试中的fake外部服务仅证明适配控制流，不是外部生产服务验收。

另在临时SQLite与本地确定性fixture中复现A01/A02/A03/A04，结果见outputs/memory-prd-alignment-20261030/probe-results.json。所有这些操作均禁止真实远程HTTP；没有改产品代码，没有真实模型请求、生产数据库修改、安装、提交或推送。

这不是所有条款的形式化证明，也不是安全认证、真实模型质量/延迟评测或新安装包验收。现有23项通过与新增探针发现问题可以同时成立：它们覆盖的条件不同。

## 8. 处理顺序

优先修复A01提交异常隔离和A02缺baseline的正式门禁，再补A03语义MERGE及A04真实预算闭环；这些直接影响正确性和验收可信度。随后补08的近邻/巩固算法和默认调度、trace联动，并落实MemoryRuntime职责迁移。

文档方面，清理明确过时的枚举、公式和“当前没有真实向量/仍只有stub”等句子；把当前API、最终目标与历史记录区分开。保留尚未实现的要求，不能通过删除ANN、语义MERGE或事务性目标让报告看起来全部一致。
