# Project Sora 正式 PRD 索引

更新：2026-10-29。目标设计与实际交付分开核对；当前状态见 [客户端交付状态](DESKTOP_DELIVERY_STATUS.md)、[工程台账](context/PROJECT_LEDGER.md)。

所有现行PRD的开头必须先解释职责、存在必要性、上游具体输入、下游具体输出、运行时机、输入到输出的核心机制和全系统价值，并以因果例子说明，不只写抽象一句话定义。详见[PRD开篇规范](PRD_AUTHORING_STANDARD.md)。

系统清单中的19个已实现/集成系统均有设计入口；配套六份设计另列。Presence和多模态仍延后；F01–F30未完成。

2026-10-29已完成全部28份设计/总览的代码实践对齐与逐份读回：更新当前实现、运行契约、证据及剩余验收，旧实施记录明确标为历史，保留最终目标与正文架构图。完整版本与范围见[更新回执](../outputs/prd-refresh-all-20261029/COMPLETION.md)；精确版本见`context/catalog/artifacts.json`，不以设计更新代替发布验收。

## 1. 项目总体设计

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Project Sora Overall Architecture](https://qcnqo2xrtj31.feishu.cn/docx/Ax9FdxrV5oNHqlxW7O4cEebln3e) | 系统边界、当前协作和阅读入口 |
| [Unified Conversation Dataflow and System Collaboration v1.4](https://qcnqo2xrtj31.feishu.cn/docx/Wl2cdE1y7oP4zaxIVAycQV2pnKe) | 系统边界、当前协作和阅读入口 |
| [Project Sora Memory Architecture](https://qcnqo2xrtj31.feishu.cn/docx/O8uDdAggsofRM2xrB5TcuoOhnUu) | 系统边界、当前协作和阅读入口 |

## 2. 核心运行时、模型与用户控制

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Text Conversation Runtime](https://qcnqo2xrtj31.feishu.cn/docx/J5Sid3laJo8PRAxOrmncyflqnPc) | 整轮状态机、解释、仲裁、生成、取消与提交 |
| [Live Text Conversation Gateway](https://qcnqo2xrtj31.feishu.cn/wiki/AFMOwQKPaijrkJkFVBNcmm7Dn7B) | 本地认证API、客户端交付、恢复与背压 |
| [Model Provider and BYOK PRD v1.0](https://qcnqo2xrtj31.feishu.cn/docx/XpWBdVgJKoVbBoxzWFacMxbXng5) | BYOK、OS凭据、能力探测、路由与fallback |
| [Interruptibility System](https://qcnqo2xrtj31.feishu.cn/docx/Tn2td1lJqoXujOx8xgscnayunKc) | 回应、展开、追问、主动性与明确控制 |

## 3. Memory 十模块

当前“设计目标与代码到底哪里不同”见[逐模块一致性审查](https://qcnqo2xrtj31.feishu.cn/wiki/IrC6wNyOriorMbktzjVcVLn2nje)，本地[代码对照与复现证据](MEMORY_PRD_IMPLEMENTATION_ALIGNMENT.md)。它记录未修复风险，不替代正式目标。

先看[Memory 系统总览：十模块如何协作](https://qcnqo2xrtj31.feishu.cn/wiki/Zivwwi8cXidwYokMPAncj5oJnGc)，或本地[同名总览](MEMORY_SYSTEM_OVERVIEW.md)，理解写入、召回、演化和治理全貌。它是解释性汇总，不代替下面十份正式PRD。

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Memory Module 01](https://qcnqo2xrtj31.feishu.cn/docx/K5Gvd02F8o00rexLWCzcy0Z2nMa) | Module 01：对话事件、episode、来源与证据链 |
| [Memory Module 02 LLM Extractor](https://qcnqo2xrtj31.feishu.cn/docx/XBxPdktWaoUJI7xZMBqcloOwnTh) | Module 02：异步LLM抽取、相关记忆及四操作决策 |
| [Memory Module 03 Gate](https://qcnqo2xrtj31.feishu.cn/docx/XlF7doxxWow4ClxbB9XcCkpVnWd) | Module 03：候选准入、独立隔离、审核与提交 |
| [Memory Module 04 Canonical Store](https://qcnqo2xrtj31.feishu.cn/docx/ZXEXdtB1Vo0KxHx8gPLc4zrfn8M) | Module 04：Canonical事实、版本、回滚与outbox |
| [Memory Module 05 Retrieval](https://qcnqo2xrtj31.feishu.cn/docx/U588ddMFpoj5fmxgDH5cEYjMnqh) | Module 05：向量/词法/时间检索、融合与重排 |
| [Memory Module 06 Recall Planner](https://qcnqo2xrtj31.feishu.cn/docx/AJNodsUmeov55oxDvYace4IQnkb) | Module 06：召回前规划、查询分解及策略 |
| [Memory Module 07 Surface Policy](https://qcnqo2xrtj31.feishu.cn/docx/T5uddHGYboXxsBxFrlOczQtWnxc) | Module 07：记忆呈现权限、预算与上下文打包 |
| [Memory Module 08 Lifecycle](https://qcnqo2xrtj31.feishu.cn/docx/BJTSdsRMSoL05qxjUjLczuSnnlh) | Module 08：热度、巩固、归档、遗忘与tombstone |
| [Memory Module 09 Projection Gateway](https://qcnqo2xrtj31.feishu.cn/docx/MDpsdtnQpoVYQ9xqUwDcbYyZnzP) | Module 09：外部领域投影契约、重建及删除传播 |
| [Memory Module 10 Quality Ops](https://qcnqo2xrtj31.feishu.cn/docx/AJfEdFEEEoKUQRxYq5ycAVU8nOX) | Module 10：评测、观测、管理操作与发布质量 |

## 4. 独立领域系统

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Persona System Engineering Design](https://qcnqo2xrtj31.feishu.cn/docx/FYKCdChDqoG9r3xYvNlcpjcgnFb) | 长期人格、有界成长、反思与重建 |
| [Affect and Internal State System v2](https://qcnqo2xrtj31.feishu.cn/docx/ZxQidBZGZopHbNxnpw3cXk2Unug) | 短期内部状态、调节、衰减及表达边界 |
| [Relationship System Engineering Design v3.1](https://qcnqo2xrtj31.feishu.cn/docx/MOceduqJroFb9VxkWhfc0gn1nhf) | 用户隔离关系、连续性、修复与治理 |
| [WorldOps Narrative System Engineering Design v2](https://qcnqo2xrtj31.feishu.cn/docx/OJ4Ada3VSoolFOxspXpcRdrJnte) | 内容包、叙事事件、任务、选择及编剧审核 |

## 5. 桌面客户端与体验

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Desktop Shell and Text UX System](https://qcnqo2xrtj31.feishu.cn/wiki/RrFkwF6p9iqtIJkaa9fcELABnld) | 正式桌面、文字体验、设置及开发者模式 |

## 6. 配套系统与生产治理

| 正式 PRD | 职责 / 阅读重点 |
| --- | --- |
| [Presence & Behavior](https://qcnqo2xrtj31.feishu.cn/docx/XaRDd6dYBodugZxlHLKcJf1PnUf) | 桌面场景、授权采集与行为证据；延后实施 |
| [Expression & Embodiment](https://qcnqo2xrtj31.feishu.cn/docx/Y6todoYs9oLXZzxuQV4ciRAjnfg) | 语音、角色渲染与具身表达；多模态延后 |
| [Cross-System Contract Registry](https://qcnqo2xrtj31.feishu.cn/docx/VfZadrfvToBtrkxCOBXcvJ1Sngh) | 跨系统类型、版本兼容及契约治理 |
| [Security, Privacy & Data Governance](https://qcnqo2xrtj31.feishu.cn/docx/KfCOdB8n0oLbf2x2ur3c5BNInTb) | 凭据、授权、导出、删除与数据治理 |
| [Provider, Deployment & Operations](https://qcnqo2xrtj31.feishu.cn/docx/ND39ditWHoCgFcx7HiacQeIOnRe) | 部署、后台运行、容量与运维 |
| [Release Baseline, Changelog & Rollout Governance](https://qcnqo2xrtj31.feishu.cn/docx/M7kbdozetoOAqkxUdTTcPBoPnpf) | 基线、打包、迁移、更新、回滚和发布门禁 |

## 7. 内部组件的设计归属

| 组件 | 所属设计 |
| --- | --- |
| Turn Interpreter / BoundedDecisionProvider | Text Runtime §6；Interruptibility负责未决权限维度解析 |
| Signal / Initiative / Semantic Arbiter / Response Planner | Text Runtime §3、§5、§6；各领域PRD约束其Frame和Signal |
| Context Packer / Expression Router | Text Runtime §7–8；Memory 07约束记忆披露与预算；多模态见Expression PRD |
| Extractor scheduler / committed-turn来源 | Memory 01–03 + Text Runtime §9；运行契约：docs/MEMORY_BACKGROUND_INGESTION.md |
| Frame Inspector / 受控维护 / Quality Inspector | Desktop PRD + Memory 10；工程边界：docs/DESKTOP_DELIVERY_STATUS.md及F01–F30台账 |
| 空间切换 / 通知 / 导出删除 | Desktop PRD + Security/Data Governance + BYOK；F23–F25仍须安装和故障验收 |

## 8. 验收与维护

源码/测试入口见 `context/catalog/systems.json`；正式文档与审计类型见 `context/catalog/artifacts.json`。PRD描述目标，审计描述审计时点，工作流和实际验证描述当前进展。引用的章节以最新PRD目录为准。

新组件应先关联所属PRD；独立所有权需要独立设计。修改入口时同时同步飞书导航、整体架构简明版第8节“各系统 PRD 与工程资料”和本索引。原始引用、历史研究和架构图保留供追溯。
