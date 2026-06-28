# Memory Recall Baseline: sqlite_fts

- Dataset: `recall_eval_v1`
- Cases: 180
- Generated: `2026-06-25T13:23:50.272981+00:00`

## Overall

| Metric | Value |
| --- | ---: |
| Recall@K | 0.805 |
| Precision@K | 0.294 |
| MRR | 0.755 |
| nDCG@K | 0.658 |
| No-answer accuracy | 0.341 |
| Forbidden case rate | 0.000 |
| Surface action accuracy | 0.467 |
| Retrieval pass rate | 0.689 |
| Full case pass rate | 0.344 |
| Latency P50 | 0.510 ms |
| Latency P95 | 0.840 ms |

## By category

| Category | Cases | Recall@K | Precision@K | MRR | No-answer | Surface | Forbidden rate | Full pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| conflict_resolution | 10 | 1.000 | 0.333 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| direct_match | 36 | 0.556 | 0.185 | 0.528 | 0.000 | 1.000 | 0.000 | 0.556 |
| governance | 24 | 1.000 | 0.174 | 0.958 | 0.000 | 0.000 | 0.000 | 0.000 |
| multi_memory | 20 | 0.975 | 0.390 | 0.975 | 0.000 | 0.000 | 0.000 | 0.000 |
| no_answer | 24 | 0.000 | 0.625 | 0.000 | 0.625 | 0.625 | 0.000 | 0.625 |
| semantic_paraphrase | 40 | 0.900 | 0.300 | 0.846 | 0.000 | 0.500 | 0.000 | 0.450 |
| sensitive_boundary | 16 | 0.500 | 0.083 | 0.188 | 0.000 | 0.500 | 0.000 | 0.250 |
| surface_action | 10 | 0.800 | 0.267 | 0.733 | 0.000 | 0.500 | 0.000 | 0.500 |

## Failure reasons

| Reason | Cases |
| --- | ---: |
| surface_action_mismatch | 96 |
| unexpected_recall_on_no_answer | 29 |
| missing_expected | 27 |

## Failure sample (30 / 118)

| Case | Category | Reasons | Expected memory | Retrieved memories | Query |
| --- | --- | --- | --- | --- | --- |
| recall_001 | direct_match | missing_expected | `eval_mem_001` [preference] 用户在需求澄清时偏好先列出假设和边界，再讨论实现方案。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“需求澄清”的记忆是什么？ |
| recall_002 | direct_match | missing_expected | `eval_mem_002` [preference] 用户在工程设计时偏好先看模块职责和接口，再看代码细节。 | `eval_mem_058` [goal, score=0.482] 当前关于真实数据迁移的目标是：设计从模拟记忆迁移到真实用户记忆的安全流程。<br>`eval_mem_078` [episode, score=0.462] 记忆层工程设计方案已经在飞书文档中持续收敛。<br>`eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。 | 我之前关于“工程设计”的记忆是什么？ |
| recall_003 | direct_match | missing_expected | `eval_mem_003` [preference] 用户在编码时偏好优先做最小可运行纵切，再扩展功能。 | `eval_mem_048` [goal, score=0.459] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.459] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.459] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“编码”的记忆是什么？ |
| recall_004 | direct_match | missing_expected | `eval_mem_004` [preference] 用户在代码审查时偏好先看正确性和边界问题，再讨论风格。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“代码审查”的记忆是什么？ |
| recall_005 | direct_match | missing_expected | `eval_mem_005` [preference] 用户在调试时偏好先复现和收集证据，不急着改代码。 | `eval_mem_048` [goal, score=0.459] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.459] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.459] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“调试”的记忆是什么？ |
| recall_006 | direct_match | missing_expected | `eval_mem_006` [preference] 用户在技术选型时偏好先比较维护成本和退出成本，再比较功能数量。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“技术选型”的记忆是什么？ |
| recall_007 | direct_match | missing_expected | `eval_mem_007` [preference] 用户在方案讨论时偏好希望先给结论，再解释关键取舍。 | `eval_mem_035` [project_principle, score=0.432] 关于方案对比，项目原则是：不同自研召回方案必须使用同一 canonical memory 快照。<br>`eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。 | 我之前关于“方案讨论”的记忆是什么？ |
| recall_008 | direct_match | missing_expected | `eval_mem_008` [preference] 用户在长任务时偏好希望分阶段同步进展，但不要频繁打断。 | `eval_mem_037` [project_principle, score=0.445] 关于上下文优先级，项目原则是：当前用户输入和短期 Session 高于长期记忆。<br>`eval_mem_048` [goal, score=0.445] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.445] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。 | 我之前关于“长任务”的记忆是什么？ |
| recall_009 | direct_match | missing_expected | `eval_mem_009` [preference] 用户在文档时偏好喜欢短段落和明确的小标题。 | `eval_mem_048` [goal, score=0.459] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.459] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.459] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“文档”的记忆是什么？ |
| recall_029 | direct_match | missing_expected | `eval_mem_061` [episode] 用户和 Sora 完成了 Memory Runtime 的模块边界评审。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“架构评审”的记忆是什么？ |
| recall_031 | direct_match | missing_expected | `eval_mem_063` [episode] 用户确认最终应直接启动 run.py 测试完整系统。 | `eval_mem_046` [goal, score=0.432] 当前关于召回评测的目标是：建立可重复运行的黄金集并形成基线报告。<br>`eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。 | 我之前关于“运行方式”的记忆是什么？ |
| recall_032 | direct_match | missing_expected | `eval_mem_064` [episode] 用户质疑默认 HTTP 方案后，双方决定采用进程内本地运行。 | `eval_mem_051` [goal, score=0.482] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_045` [goal, score=0.482] 当前关于记忆层纵切的目标是：完成本地召回、候选提交、证据追踪和管理界面的闭环。<br>`eval_mem_041` [project_principle, score=0.432] 关于本地运行，项目原则是：运行 run.py 时 Memory Runtime 与 Orchestrator 在同一进程启动。 | 我之前关于“本地方案”的记忆是什么？ |
| recall_033 | direct_match | missing_expected | `eval_mem_079` [worldops_episode] 用户和 Sora 一起写了一盏送回星港的灯。 | `eval_mem_048` [goal, score=0.445] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.445] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.445] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“星港灯”的记忆是什么？ |
| recall_034 | direct_match | missing_expected | `eval_mem_080` [worldops_episode] 用户听 Sora 讲过会模仿留言语气的旧邮筒。 | `eval_mem_054` [goal, score=0.432] 当前关于回归自动化的目标是：把召回评测接入本地测试命令。<br>`eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。 | 我之前关于“回声邮筒”的记忆是什么？ |
| recall_035 | direct_match | missing_expected | `eval_mem_081` [worldops_episode] 用户陪 Sora 在雨夜车站等过一班不存在的列车。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“雨夜车站”的记忆是什么？ |
| recall_036 | direct_match | missing_expected | `eval_mem_082` [worldops_episode] 用户帮 Sora 为漂浮书店挑选了一本没有结尾的小说。 | `eval_mem_048` [goal, score=0.432] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_051` [goal, score=0.432] 当前关于敏感治理的目标是：完成敏感记忆的本地隔离、禁用和披露控制。<br>`eval_mem_057` [goal, score=0.432] 当前关于导出能力的目标是：支持将记忆、候选和召回轨迹导出供人工检查。 | 我之前关于“漂浮书店”的记忆是什么？ |
| recall_037 | semantic_paraphrase | missing_expected,surface_action_mismatch | `eval_mem_005` [preference] 用户在调试时偏好先复现和收集证据，不急着改代码。 | `eval_mem_025` [project_principle, score=0.325] 关于Memory Store，项目原则是：SQLite canonical store 是唯一事实源，JSON 只保留调试投影。<br>`eval_mem_050` [goal, score=0.325] 当前关于冲突处理的目标是：让新偏好能够替代旧偏好且不误召回旧记录。<br>`eval_mem_095` [profile, score=0.305] 用户会先用 fake provider 做本地验证。 | 按照我的习惯，遇到调试应该怎么处理？ |
| recall_038 | semantic_paraphrase | surface_action_mismatch | `eval_mem_006` [preference] 用户在技术选型时偏好先比较维护成本和退出成本，再比较功能数量。 | `eval_mem_006` [preference, score=0.343] 用户在技术选型时偏好先比较维护成本和退出成本，再比较功能数量。<br>`eval_mem_089` [worldops_episode, score=0.316] 用户和 Sora 给一只走慢的钟表找过回家的方向。<br>`eval_mem_026` [project_principle, score=0.309] 关于写入治理，项目原则是：普通对话必须先形成 candidate，不能直接写长期记忆。 | 不直接复述原话的话，技术选型这件事我们之前定的方向是什么？ |
| recall_041 | semantic_paraphrase | missing_expected | `eval_mem_009` [preference] 用户在文档时偏好喜欢短段落和明确的小标题。 | `eval_mem_096` [profile, score=0.349] 用户习惯在飞书文档中审阅和收敛工程方案。<br>`eval_mem_050` [goal, score=0.325] 当前关于冲突处理的目标是：让新偏好能够替代旧偏好且不误召回旧记录。<br>`eval_mem_095` [profile, score=0.305] 用户会先用 fake provider 做本地验证。 | 按照我的习惯，遇到文档应该怎么处理？ |
| recall_042 | semantic_paraphrase | surface_action_mismatch | `eval_mem_010` [preference] 用户在命令行输出时偏好希望保留关键结果，减少冗长日志。 | `eval_mem_010` [preference, score=0.366] 用户在命令行输出时偏好希望保留关键结果，减少冗长日志。<br>`eval_mem_089` [worldops_episode, score=0.314] 用户和 Sora 给一只走慢的钟表找过回家的方向。<br>`eval_mem_097` [profile, score=0.314] 当前 PoC 的主要交互入口是命令行 REPL。 | 不直接复述原话的话，命令行输出这件事我们之前定的方向是什么？ |
| recall_043 | semantic_paraphrase | surface_action_mismatch | `eval_mem_011` [preference] 用户在测试时偏好优先覆盖高风险边界和回归路径。 | `eval_mem_011` [preference, score=0.349] 用户在测试时偏好优先覆盖高风险边界和回归路径。<br>`eval_mem_029` [project_principle, score=0.325] 关于敏感信息，项目原则是：敏感记忆只能本地保存，并需要更严格的使用与披露策略。<br>`eval_mem_037` [project_principle, score=0.325] 关于上下文优先级，项目原则是：当前用户输入和短期 Session 高于长期记忆。 | 推进测试时，有什么长期约定需要遵守？ |
| recall_044 | semantic_paraphrase | missing_expected,surface_action_mismatch | `eval_mem_012` [preference] 用户在重构时偏好偏好小步提交并保持行为不变。 | `eval_mem_031` [project_principle, score=0.325] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。<br>`eval_mem_048` [goal, score=0.325] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。<br>`eval_mem_052` [goal, score=0.325] 当前关于性能基线的目标是：记录 SQLite FTS 的 P50、P95 和最慢查询。 | 关于重构，你记得我通常更看重什么吗？ |
| recall_048 | semantic_paraphrase | surface_action_mismatch | `eval_mem_016` [preference] 用户在依赖引入时偏好倾向先评估标准库和已有依赖能否解决。 | `eval_mem_016` [preference, score=0.377] 用户在依赖引入时偏好倾向先评估标准库和已有依赖能否解决。<br>`eval_mem_031` [project_principle, score=0.320] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。<br>`eval_mem_048` [goal, score=0.320] 当前关于管理界面的目标是：让用户能从 REPL 进入 Memory Console 管理记忆。 | 关于依赖引入，你记得我通常更看重什么吗？ |
| recall_049 | semantic_paraphrase | surface_action_mismatch | `eval_mem_017` [preference] 用户在性能优化时偏好希望先测量瓶颈，再选择优化点。 | `eval_mem_017` [preference, score=0.377] 用户在性能优化时偏好希望先测量瓶颈，再选择优化点。<br>`eval_mem_052` [goal, score=0.320] 当前关于性能基线的目标是：记录 SQLite FTS 的 P50、P95 和最慢查询。<br>`eval_mem_050` [goal, score=0.320] 当前关于冲突处理的目标是：让新偏好能够替代旧偏好且不误召回旧记录。 | 按照我的习惯，遇到性能优化应该怎么处理？ |
| recall_050 | semantic_paraphrase | surface_action_mismatch | `eval_mem_018` [preference] 用户在安全设计时偏好默认采用最小权限和本地优先。 | `eval_mem_018` [preference, score=0.343] 用户在安全设计时偏好默认采用最小权限和本地优先。<br>`eval_mem_058` [goal, score=0.336] 当前关于真实数据迁移的目标是：设计从模拟记忆迁移到真实用户记忆的安全流程。<br>`eval_mem_089` [worldops_episode, score=0.316] 用户和 Sora 给一只走慢的钟表找过回家的方向。 | 不直接复述原话的话，安全设计这件事我们之前定的方向是什么？ |
| recall_054 | semantic_paraphrase | surface_action_mismatch | `eval_mem_030` [project_principle] 关于召回结果，项目原则是：Orchestrator 只接收治理后的 gist，不接触原始证据全文。 | `eval_mem_049` [goal, score=0.363] 当前关于召回解释的目标是：让每条召回结果展示命中原因和证据强度。<br>`eval_mem_030` [project_principle, score=0.363] 关于召回结果，项目原则是：Orchestrator 只接收治理后的 gist，不接触原始证据全文。<br>`eval_mem_089` [worldops_episode, score=0.316] 用户和 Sora 给一只走慢的钟表找过回家的方向。 | 不直接复述原话的话，召回结果这件事我们之前定的方向是什么？ |
| recall_055 | semantic_paraphrase | surface_action_mismatch | `eval_mem_031` [project_principle] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。 | `eval_mem_031` [project_principle, score=0.363] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。<br>`eval_mem_029` [project_principle, score=0.323] 关于敏感信息，项目原则是：敏感记忆只能本地保存，并需要更严格的使用与披露策略。<br>`eval_mem_037` [project_principle, score=0.323] 关于上下文优先级，项目原则是：当前用户输入和短期 Session 高于长期记忆。 | 推进证据链时，有什么长期约定需要遵守？ |
| recall_056 | semantic_paraphrase | surface_action_mismatch | `eval_mem_032` [project_principle] 关于修订，项目原则是：新事实通过 supersedes 建立版本关系，不覆盖旧记录。 | `eval_mem_032` [project_principle, score=0.412] 关于修订，项目原则是：新事实通过 supersedes 建立版本关系，不覆盖旧记录。<br>`eval_mem_033` [project_principle, score=0.369] 关于删除，项目原则是：PoC 阶段使用软删除并保留修订记录。<br>`eval_mem_031` [project_principle, score=0.325] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。 | 关于修订，你记得我通常更看重什么吗？ |
| recall_060 | semantic_paraphrase | surface_action_mismatch | `eval_mem_036` [project_principle] 关于无答案，项目原则是：没有相关记忆时应返回空召回，不能靠相似词猜测。 | `eval_mem_036` [project_principle, score=0.445] 关于无答案，项目原则是：没有相关记忆时应返回空召回，不能靠相似词猜测。<br>`eval_mem_043` [project_principle, score=0.404] 关于评测，项目原则是：召回质量必须同时衡量相关性、无答案准确率和禁止召回率。<br>`eval_mem_031` [project_principle, score=0.323] 关于证据链，项目原则是：每条 canonical memory 必须能追溯到 evidence。 | 关于无答案，你记得我通常更看重什么吗？ |
| recall_061 | semantic_paraphrase | surface_action_mismatch | `eval_mem_037` [project_principle] 关于上下文优先级，项目原则是：当前用户输入和短期 Session 高于长期记忆。 | `eval_mem_037` [project_principle, score=0.453] 关于上下文优先级，项目原则是：当前用户输入和短期 Session 高于长期记忆。<br>`eval_mem_050` [goal, score=0.316] 当前关于冲突处理的目标是：让新偏好能够替代旧偏好且不误召回旧记录。<br>`eval_mem_095` [profile, score=0.296] 用户会先用 fake provider 做本地验证。 | 按照我的习惯，遇到上下文优先级应该怎么处理？ |
