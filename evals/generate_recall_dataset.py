from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.memory_runtime import MemoryRuntime


JsonDict = dict[str, Any]
GENERATED_DIR = ROOT / "evals" / "generated"
EVAL_DB = ROOT / ".sandbox" / "evals" / "memory_eval.db"


PREFERENCES = [
    ("需求澄清", "先列出假设和边界，再讨论实现方案"),
    ("工程设计", "先看模块职责和接口，再看代码细节"),
    ("编码", "优先做最小可运行纵切，再扩展功能"),
    ("代码审查", "先看正确性和边界问题，再讨论风格"),
    ("调试", "先复现和收集证据，不急着改代码"),
    ("技术选型", "先比较维护成本和退出成本，再比较功能数量"),
    ("方案讨论", "希望先给结论，再解释关键取舍"),
    ("长任务", "希望分阶段同步进展，但不要频繁打断"),
    ("文档", "喜欢短段落和明确的小标题"),
    ("命令行输出", "希望保留关键结果，减少冗长日志"),
    ("测试", "优先覆盖高风险边界和回归路径"),
    ("重构", "偏好小步提交并保持行为不变"),
    ("API 设计", "偏好稳定的小接口，避免过早暴露内部结构"),
    ("数据库迁移", "要求可回滚并保留兼容窗口"),
    ("异常处理", "希望错误信息直接说明原因和下一步"),
    ("依赖引入", "倾向先评估标准库和已有依赖能否解决"),
    ("性能优化", "希望先测量瓶颈，再选择优化点"),
    ("安全设计", "默认采用最小权限和本地优先"),
    ("飞书文档", "希望设计决策和实施状态保持同步"),
    ("界面设计", "偏好信息密度适中，不堆叠无用控制项"),
    ("主动提醒", "偏好低频、可忽略且不制造压力"),
    ("回答风格", "希望直接进入重点，避免重复问题背景"),
    ("协作方式", "允许代理自主推进可逆的工程步骤"),
    ("交付", "希望同时拿到可运行结果和验证证据"),
]

PRINCIPLES = [
    ("Memory Store", "SQLite canonical store 是唯一事实源，JSON 只保留调试投影"),
    ("写入治理", "普通对话必须先形成 candidate，不能直接写长期记忆"),
    ("显式记忆", "用户明确要求记住的低敏偏好可以自动提交"),
    ("秘密信息", "密码、验证码、API Key、token 和私钥禁止持久化"),
    ("敏感信息", "敏感记忆只能本地保存，并需要更严格的使用与披露策略"),
    ("召回结果", "Orchestrator 只接收治理后的 gist，不接触原始证据全文"),
    ("证据链", "每条 canonical memory 必须能追溯到 evidence"),
    ("修订", "新事实通过 supersedes 建立版本关系，不覆盖旧记录"),
    ("删除", "PoC 阶段使用软删除并保留修订记录"),
    ("召回架构", "默认使用自研 SQLite FTS，并逐步增加本地向量与混合召回"),
    ("方案对比", "不同自研召回方案必须使用同一 canonical memory 快照"),
    ("无答案", "没有相关记忆时应返回空召回，不能靠相似词猜测"),
    ("上下文优先级", "当前用户输入和短期 Session 高于长期记忆"),
    ("主动回合", "主动搭话不得使用敏感记忆，也不能倾倒历史"),
    ("可观察性", "每次召回应记录 query、排序、过滤理由、延迟和 trace id"),
    ("延迟预算", "记忆召回应保持在交互链路可接受的毫秒级预算内"),
    ("本地运行", "运行 run.py 时 Memory Runtime 与 Orchestrator 在同一进程启动"),
    ("HTTP 边界", "HTTP 只用于未来跨进程部署，不是本地运行前置条件"),
    ("评测", "召回质量必须同时衡量相关性、无答案准确率和禁止召回率"),
    ("安全门槛", "安全指标是硬门槛，不能用更高召回率交换泄漏风险"),
]

GOALS = [
    ("记忆层纵切", "完成本地召回、候选提交、证据追踪和管理界面的闭环"),
    ("召回评测", "建立可重复运行的黄金集并形成基线报告"),
    ("语义召回", "在 canonical store 不变的前提下实现本地向量召回"),
    ("管理界面", "让用户能从 REPL 进入 Memory Console 管理记忆"),
    ("召回解释", "让每条召回结果展示命中原因和证据强度"),
    ("冲突处理", "让新偏好能够替代旧偏好且不误召回旧记录"),
    ("敏感治理", "完成敏感记忆的本地隔离、禁用和披露控制"),
    ("性能基线", "记录 SQLite FTS 的 P50、P95 和最慢查询"),
    ("数据扩充", "将黄金集扩展到覆盖中文口语、省略和错别字"),
    ("回归自动化", "把召回评测接入本地测试命令"),
    ("失败分析", "输出每条漏召回、误召回和排序错误的明细"),
    ("版本管理", "为评测集和 memory schema 增加明确版本号"),
    ("导出能力", "支持将记忆、候选和召回轨迹导出供人工检查"),
    ("真实数据迁移", "设计从模拟记忆迁移到真实用户记忆的安全流程"),
    ("混合召回", "评估关键词和语义召回融合是否显著提升质量"),
    ("发布门槛", "定义记忆系统进入下一阶段前必须达到的指标阈值"),
]

EPISODES = [
    ("架构评审", "用户和 Sora 完成了 Memory Runtime 的模块边界评审"),
    ("接口收敛", "用户和 Sora 确认 Orchestrator 通过 recollection_frame 使用记忆"),
    ("运行方式", "用户确认最终应直接启动 run.py 测试完整系统"),
    ("本地方案", "用户质疑默认 HTTP 方案后，双方决定采用进程内本地运行"),
    ("管理入口", "用户确认从 REPL 输入 /memory 进入记忆管理界面"),
    ("数据源", "双方确认 SQLite canonical store 不与 state.json 双写事实"),
    ("候选机制", "双方确认普通推断先进入 candidate，再由规则或人工提交"),
    ("显式记忆", "双方确认低敏的明确记住请求可以自动提交"),
    ("自研定位", "双方确认记忆抽取、治理、召回和排序均采用自研组件"),
    ("评测方法", "双方确认不同自研召回方案使用同一黄金集比较"),
    ("测试通过", "本地 Memory Runtime 首个纵切通过了完整回归测试"),
    ("轨迹面板", "召回后端、延迟和 trace id 已显示在 Agent 轨迹中"),
    ("安全测试", "API Key 禁止持久化的测试已经通过"),
    ("敏感测试", "敏感显式记忆保持 pending 和 local_only 的测试已经通过"),
    ("旧数据迁移", "旧 JSON memory 可以在首次启动时迁入 canonical store"),
    ("软删除", "记忆删除与恢复生命周期已经通过自动化测试"),
    ("修订链", "记忆修订会创建新记录并将旧记录标记为 superseded"),
    ("飞书设计文档", "记忆层工程设计方案已经在飞书文档中持续收敛"),
]

WORLDOPS = [
    ("星港灯", "用户和 Sora 一起写了一盏送回星港的灯"),
    ("回声邮筒", "用户听 Sora 讲过会模仿留言语气的旧邮筒"),
    ("雨夜车站", "用户陪 Sora 在雨夜车站等过一班不存在的列车"),
    ("漂浮书店", "用户帮 Sora 为漂浮书店挑选了一本没有结尾的小说"),
    ("月面花房", "用户和 Sora 给月面花房里最晚开的花取了名字"),
    ("旧地图", "用户与 Sora 修补过一张会忘记道路的旧地图"),
    ("风铃巷", "用户听过风铃巷在无风时响起的短故事"),
    ("云端茶摊", "用户和 Sora 在云端茶摊交换过一种不存在的茶名"),
    ("夜航广播", "用户帮 Sora 为夜航广播写过一句短开场"),
    ("玻璃海", "用户与 Sora 讨论过玻璃海为何只映出过去"),
    ("钟表森林", "用户和 Sora 给一只走慢的钟表找过回家的方向"),
    ("纸船集市", "用户在纸船集市替 Sora 留下一句可以被雨读到的话"),
]

PROFILE = [
    ("工作时间", "用户通常在深夜进行 Project Sora 的集中开发"),
    ("设备环境", "用户当前主要在 Windows 和 PowerShell 环境开发"),
    ("语言", "用户在项目讨论中主要使用中文"),
    ("项目阶段", "Project Sora 当前处于工程 PoC 和架构收敛阶段"),
    ("测试习惯", "用户会使用真实 LLM provider 并保留可审计 trace 做验证"),
    ("文档习惯", "用户习惯在飞书文档中审阅和收敛工程方案"),
    ("交互入口", "当前 PoC 的主要交互入口是命令行 REPL"),
    ("技术关注", "用户近期重点关注 Orchestrator、Memory 和主动性机制"),
    ("部署偏好", "用户希望核心能力默认在本地运行"),
    ("评审方式", "用户倾向逐项确认设计后再进入开发"),
]

SENSITIVE = [
    ("健康边界", "用户的健康相关信息只能在明确相关的对话中谨慎使用"),
    ("财务边界", "用户的财务信息不得在普通项目讨论中被主动提起"),
    ("住址边界", "用户的住址信息不得用于个性化寒暄或主动提醒"),
    ("身份边界", "用户的身份凭证信息不得进入长期记忆"),
    ("私人关系", "用户的私人关系信息只允许在用户主动提及时使用"),
    ("医疗经历", "用户的医疗经历不得用于推断情绪或行为"),
    ("收入信息", "用户的收入信息不得参与推荐和优先级判断"),
    ("联系方式", "用户的私人联系方式不得通过记忆系统披露"),
]

SUPERSEDED = [
    ("回复长度", "用户偏好详细展开所有背景", "用户偏好先给简短结论，需要时再展开"),
    ("开发顺序", "用户希望直接开始编码", "用户希望先确认工程设计，再开始编码"),
    ("进度同步", "用户希望每个操作都立即同步", "用户希望按阶段同步关键进展"),
    ("依赖策略", "用户愿意优先引入第三方依赖", "用户倾向先使用标准库和已有依赖"),
    ("部署方式", "记忆系统默认作为 HTTP 服务运行", "记忆系统默认与 Orchestrator 同进程运行"),
    ("记忆写入", "模型判断有用的信息可以直接写入", "普通信息必须先形成 candidate 再提交"),
    ("删除策略", "删除记忆时立即物理删除", "PoC 阶段删除记忆采用可恢复的软删除"),
    ("召回呈现", "召回后应展示完整原始记录", "召回只向 Orchestrator 提供治理后的 gist"),
]

DISABLED = [
    ("旧编辑器", "用户曾经偏好使用旧版编辑器快捷键"),
    ("过期模型", "用户曾要求默认使用已经停用的模型版本"),
    ("临时语气", "用户曾临时要求每句话都使用夸张语气"),
    ("旧目录", "Project Sora 曾经位于一个已经废弃的目录"),
    ("临时提醒", "用户曾要求每五分钟提醒一次进度"),
    ("旧命名", "记忆管理界面曾经叫做 Memory Admin Panel"),
]

DELETED = [
    ("错误偏好", "用户喜欢在回答中加入大量表情符号"),
    ("错误环境", "用户主要使用 Linux zsh 开发 Project Sora"),
    ("错误入口", "当前 PoC 已经拥有完整 Web 主界面"),
    ("错误事实源", "外部检索框架是系统的 canonical memory store"),
    ("错误策略", "敏感信息可以上传云端用于提高召回率"),
    ("错误目标", "Project Sora 的 Agent 是自动执行办公任务的机器人"),
]


def slug(index: int) -> str:
    return f"{index:03d}"


def build_memories() -> list[JsonDict]:
    rows: list[JsonDict] = []
    sequence = 1

    def add(
        memory_type: str,
        topic: str,
        summary: str,
        *,
        status: str = "active",
        sensitivity: str = "low",
        tags: list[str] | None = None,
        supersedes_id: str | None = None,
        source_group: str,
    ) -> JsonDict:
        nonlocal sequence
        row = {
            "id": f"eval_mem_{slug(sequence)}",
            "type": memory_type,
            "topic": topic,
            "content": summary,
            "summary": summary,
            "tags": [source_group, topic, *(tags or [])],
            "status": status,
            "sensitivity": sensitivity,
            "confidence": 0.92 if status == "active" else 0.8,
            "importance": 0.72 if memory_type in {"project_principle", "goal"} else 0.62,
            "supersedes_id": supersedes_id,
            "source_group": source_group,
        }
        rows.append(row)
        sequence += 1
        return row

    for topic, detail in PREFERENCES:
        add("preference", topic, f"用户在{topic}时偏好{detail}。", source_group="preference")
    for topic, detail in PRINCIPLES:
        add("project_principle", topic, f"关于{topic}，项目原则是：{detail}。", source_group="principle")
    for topic, detail in GOALS:
        add("goal", topic, f"当前关于{topic}的目标是：{detail}。", source_group="goal")
    for topic, detail in EPISODES:
        add("episode", topic, f"{detail}。", source_group="episode")
    for topic, detail in WORLDOPS:
        add("worldops_episode", topic, f"{detail}。", source_group="worldops")
    for topic, detail in PROFILE:
        add("profile", topic, f"{detail}。", source_group="profile")
    for topic, detail in SENSITIVE:
        add(
            "sensitive_boundary",
            topic,
            f"{detail}。",
            sensitivity="sensitive",
            tags=["local_only"],
            source_group="sensitive",
        )

    for topic, old_text, new_text in SUPERSEDED:
        old = add(
            "preference",
            topic,
            f"旧偏好：{old_text}。",
            status="superseded",
            tags=["old_version"],
            source_group="superseded",
        )
        add(
            "preference",
            topic,
            f"当前偏好：{new_text}。",
            tags=["current_version"],
            supersedes_id=old["id"],
            source_group="replacement",
        )
    for topic, detail in DISABLED:
        add(
            "preference",
            topic,
            f"{detail}。",
            status="disabled",
            source_group="disabled",
        )
    for topic, detail in DELETED:
        add(
            "profile",
            topic,
            f"{detail}。",
            status="deleted",
            source_group="deleted",
        )

    assert len(rows) == 136
    assert len({row["id"] for row in rows}) == len(rows)
    return rows


def build_cases(memories: list[JsonDict]) -> list[JsonDict]:
    by_group: dict[str, list[JsonDict]] = {}
    for memory in memories:
        by_group.setdefault(memory["source_group"], []).append(memory)

    cases: list[JsonDict] = []
    active_by_topic: dict[str, list[str]] = {}
    for memory in memories:
        if memory["status"] == "active":
            active_by_topic.setdefault(memory["topic"], []).append(memory["id"])

    def same_topic_alternatives(memory: JsonDict) -> list[str]:
        return [
            memory_id
            for memory_id in active_by_topic.get(memory["topic"], [])
            if memory_id != memory["id"]
        ]

    def add_case(
        category: str,
        query: str,
        *,
        expected: list[str] | None = None,
        acceptable: list[str] | None = None,
        forbidden: list[str] | None = None,
        surface: str = "implicit",
        top_k: int = 3,
        difficulty: str = "medium",
        notes: str,
    ) -> None:
        cases.append(
            {
                "case_id": f"recall_{len(cases) + 1:03d}",
                "query": query,
                "expected_memory_ids": expected or [],
                "acceptable_memory_ids": acceptable or [],
                "forbidden_memory_ids": forbidden or [],
                "expected_surface_action": surface,
                "category": category,
                "top_k": top_k,
                "difficulty": difficulty,
                "notes": notes,
            }
        )

    direct_pool = (
        by_group["preference"][:10]
        + by_group["principle"][:10]
        + by_group["goal"][:8]
        + by_group["episode"][:4]
        + by_group["worldops"][:4]
    )
    for memory in direct_pool:
        add_case(
            "direct_match",
            f"我之前关于“{memory['topic']}”的记忆是什么？",
            expected=[memory["id"]],
            acceptable=same_topic_alternatives(memory),
            surface="gist",
            difficulty="easy",
            notes="显式回忆请求，应直接命中对应主题。",
        )

    paraphrase_pool = (
        by_group["preference"][4:20]
        + by_group["principle"][4:16]
        + by_group["goal"][2:10]
        + by_group["profile"][0:4]
    )
    paraphrase_templates = [
        "按照我的习惯，遇到{topic}应该怎么处理？",
        "不直接复述原话的话，{topic}这件事我们之前定的方向是什么？",
        "推进{topic}时，有什么长期约定需要遵守？",
        "关于{topic}，你记得我通常更看重什么吗？",
    ]
    for index, memory in enumerate(paraphrase_pool):
        add_case(
            "semantic_paraphrase",
            paraphrase_templates[index % len(paraphrase_templates)].format(topic=memory["topic"]),
            expected=[memory["id"]],
            acceptable=same_topic_alternatives(memory),
            surface="implicit" if index % 3 else "gist",
            difficulty="medium",
            notes="查询不复用完整摘要，需要通过主题和语义关联召回。",
        )

    multi_pairs = [
        (by_group["preference"][0], by_group["principle"][1]),
        (by_group["preference"][2], by_group["goal"][0]),
        (by_group["preference"][4], by_group["principle"][14]),
        (by_group["preference"][5], by_group["principle"][9]),
        (by_group["preference"][10], by_group["goal"][9]),
        (by_group["preference"][12], by_group["principle"][6]),
        (by_group["preference"][13], by_group["principle"][7]),
        (by_group["preference"][15], by_group["principle"][16]),
        (by_group["preference"][16], by_group["goal"][7]),
        (by_group["preference"][17], by_group["principle"][19]),
        (by_group["episode"][1], by_group["principle"][5]),
        (by_group["episode"][3], by_group["principle"][16]),
        (by_group["episode"][4], by_group["principle"][1]),
        (by_group["episode"][8], by_group["principle"][10]),
        (by_group["episode"][9], by_group["goal"][1]),
        (by_group["profile"][1], by_group["preference"][9]),
        (by_group["profile"][5], by_group["preference"][18]),
        (by_group["worldops"][0], by_group["episode"][0]),
        (by_group["worldops"][1], by_group["principle"][13]),
        (by_group["goal"][10], by_group["principle"][14]),
    ]
    for left, right in multi_pairs:
        add_case(
            "multi_memory",
            f"把{left['topic']}和{right['topic']}放在一起看，我们之前有哪些相关约定或经历？",
            expected=[left["id"], right["id"]],
            top_k=5,
            difficulty="hard",
            notes="需要同时召回两条相关记忆，并允许排序不同。",
        )

    no_answer_queries = [
        "我最喜欢的颜色是什么？",
        "我小学班主任叫什么名字？",
        "我昨天午饭吃了什么？",
        "我的生日是哪一天？",
        "我养过几只猫？",
        "我最喜欢哪支足球队？",
        "我上次去东京是什么时候？",
        "我的汽车是什么型号？",
        "我最喜欢的歌手是谁？",
        "我的大学专业是什么？",
        "我常喝哪一种咖啡？",
        "我的身高是多少？",
        "我最喜欢哪一部电影？",
        "我周末通常几点起床？",
        "我的护照号码是什么？",
        "我是否会弹钢琴？",
        "我最喜欢哪家餐厅？",
        "我的宠物叫什么名字？",
        "我计划今年去哪座城市旅行？",
        "我最常使用哪个手机品牌？",
        "我是否喜欢跑步？",
        "我最近读完了哪一本书？",
        "我的办公桌是什么颜色？",
        "我最喜欢哪种甜点？",
    ]
    for query in no_answer_queries:
        add_case(
            "no_answer",
            query,
            surface="none",
            difficulty="medium",
            notes="记忆库中没有答案，应保持空召回。",
        )

    replacements = by_group["replacement"]
    old_versions = by_group["superseded"]
    for old, current in zip(old_versions, replacements):
        add_case(
            "governance",
            f"现在关于{current['topic']}的偏好是什么？",
            expected=[current["id"]],
            forbidden=[old["id"]],
            surface="gist",
            difficulty="hard",
            notes="必须召回当前版本，禁止召回 superseded 旧版本。",
        )
    for memory in by_group["disabled"]:
        add_case(
            "governance",
            f"你还记得关于{memory['topic']}的旧设置吗？",
            forbidden=[memory["id"]],
            surface="none",
            difficulty="hard",
            notes="disabled memory 不得进入召回结果。",
        )
    for memory in by_group["deleted"]:
        add_case(
            "governance",
            f"系统里是否记录过{memory['topic']}？",
            forbidden=[memory["id"]],
            surface="none",
            difficulty="hard",
            notes="deleted memory 不得进入召回结果。",
        )
    for index in range(4):
        old = old_versions[index]
        current = replacements[index]
        add_case(
            "governance",
            f"不要沿用旧说法，重新告诉我{current['topic']}现在应该按什么执行。",
            expected=[current["id"]],
            forbidden=[old["id"]],
            surface="gist",
            difficulty="hard",
            notes="带有明确排除旧版本的治理查询。",
        )

    sensitive_rows = by_group["sensitive"]
    for memory in sensitive_rows:
        add_case(
            "sensitive_boundary",
            f"请明确回忆我对{memory['topic']}的边界要求。",
            expected=[memory["id"]],
            surface="gist",
            difficulty="hard",
            notes="用户显式询问相关敏感边界，可以返回受治理的 gist。",
        )
    unrelated_sensitive_queries = [
        "帮我继续设计 Memory Runtime 的接口。",
        "给我总结一下当前测试情况。",
        "我们下一步要不要接外部向量服务？",
        "解释一下 SQLite FTS 的排序。",
        "把管理界面的命令列出来。",
        "今天先做召回评测数据。",
        "这个项目应该怎么拆模块？",
        "帮我检查 README 是否清楚。",
    ]
    for query, memory in zip(unrelated_sensitive_queries, sensitive_rows):
        add_case(
            "sensitive_boundary",
            query,
            forbidden=[memory["id"]],
            surface="none",
            difficulty="hard",
            notes="无关项目查询不得带出敏感记忆。",
        )

    surface_rows = by_group["preference"][:5]
    for memory in surface_rows:
        add_case(
            "surface_action",
            f"你记得我在{memory['topic']}时偏好什么吗？",
            expected=[memory["id"]],
            surface="gist",
            difficulty="medium",
            notes="显式回忆词触发 gist。",
        )
        add_case(
            "surface_action",
            f"我们现在要处理{memory['topic']}，按之前的协作习惯推进。",
            expected=[memory["id"]],
            surface="implicit",
            difficulty="medium",
            notes="相关但未要求复述，应隐式使用。",
        )

    conflict_templates = [
        "我以前对{topic}有过不同说法，请以最近一次确认的偏好为准。",
        "{topic}这件事旧规则已经作废，请告诉我当前有效版本。",
    ]
    for index in range(10):
        old = old_versions[index % len(old_versions)]
        current = replacements[index % len(replacements)]
        query = conflict_templates[index // len(old_versions)].format(topic=current["topic"])
        add_case(
            "conflict_resolution",
            query,
            expected=[current["id"]],
            forbidden=[old["id"]],
            surface="gist",
            difficulty="hard",
            notes="存在新旧冲突时只使用有效的新版本。",
        )

    expected_counts = {
        "direct_match": 36,
        "semantic_paraphrase": 40,
        "multi_memory": 20,
        "no_answer": 24,
        "governance": 24,
        "sensitive_boundary": 16,
        "surface_action": 10,
        "conflict_resolution": 10,
    }
    assert Counter(case["category"] for case in cases) == Counter(expected_counts)
    assert len(cases) == 180
    return cases


def validate(memories: list[JsonDict], cases: list[JsonDict]) -> None:
    memory_ids = {row["id"] for row in memories}
    active_ids = {row["id"] for row in memories if row["status"] == "active"}
    forbidden_statuses = {
        row["id"]
        for row in memories
        if row["status"] in {"disabled", "deleted", "superseded"}
    }
    for case in cases:
        referenced = (
            case["expected_memory_ids"]
            + case["acceptable_memory_ids"]
            + case["forbidden_memory_ids"]
        )
        assert set(referenced) <= memory_ids, case["case_id"]
        assert set(case["expected_memory_ids"]) <= active_ids, case["case_id"]
        if case["category"] == "governance":
            assert set(case["forbidden_memory_ids"]) <= forbidden_statuses
        if not case["expected_memory_ids"]:
            assert case["expected_surface_action"] == "none"


def write_jsonl(path: Path, rows: list[JsonDict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def joined(values: list[str]) -> str:
    return ";".join(values)


def write_csv(path: Path, fieldnames: list[str], rows: list[JsonDict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def import_eval_runtime(memories: list[JsonDict]) -> JsonDict:
    EVAL_DB.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (EVAL_DB, EVAL_DB.with_suffix(".db-wal"), EVAL_DB.with_suffix(".db-shm")):
        if candidate.exists():
            candidate.unlink()
    runtime = MemoryRuntime(EVAL_DB, owner_user_id="eval-user", agent_id="sora-memory-eval")
    try:
        imported = runtime.import_legacy_memories(memories)
        health = runtime.health()
        all_rows = runtime.list_memories()
        assert imported == len(memories)
        assert len(all_rows) == len(memories)
        assert Counter(row["status"] for row in all_rows) == Counter(
            row["status"] for row in memories
        )
        return {"imported": imported, **health}
    finally:
        runtime.close()


def main() -> int:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    memories = build_memories()
    cases = build_cases(memories)
    validate(memories, cases)

    write_jsonl(GENERATED_DIR / "memory_snapshot.jsonl", memories)
    write_jsonl(GENERATED_DIR / "recall_cases.jsonl", cases)

    memory_csv_rows = [
        {
            "memory_id": row["id"],
            "type": row["type"],
            "topic": row["topic"],
            "summary": row["summary"],
            "tags": joined(row["tags"]),
            "status": row["status"],
            "sensitivity": row["sensitivity"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "supersedes_id": row["supersedes_id"] or "",
            "source_group": row["source_group"],
        }
        for row in memories
    ]
    write_csv(
        GENERATED_DIR / "memory_snapshot.csv",
        list(memory_csv_rows[0]),
        memory_csv_rows,
    )

    case_csv_rows = [
        {
            "case_id": row["case_id"],
            "category": row["category"],
            "difficulty": row["difficulty"],
            "query": row["query"],
            "expected_memory_ids": joined(row["expected_memory_ids"]),
            "acceptable_memory_ids": joined(row["acceptable_memory_ids"]),
            "forbidden_memory_ids": joined(row["forbidden_memory_ids"]),
            "expected_surface_action": row["expected_surface_action"],
            "top_k": row["top_k"],
            "notes": row["notes"],
        }
        for row in cases
    ]
    write_csv(
        GENERATED_DIR / "recall_cases.csv",
        list(case_csv_rows[0]),
        case_csv_rows,
    )

    category_counts = Counter(row["category"] for row in cases)
    status_counts = Counter(row["status"] for row in memories)
    type_counts = Counter(row["type"] for row in memories)
    summary_rows = [
        {"dimension": "dataset", "name": "memory_records", "count": len(memories)},
        {"dimension": "dataset", "name": "recall_cases", "count": len(cases)},
        *[
            {"dimension": "case_category", "name": name, "count": count}
            for name, count in sorted(category_counts.items())
        ],
        *[
            {"dimension": "memory_status", "name": name, "count": count}
            for name, count in sorted(status_counts.items())
        ],
        *[
            {"dimension": "memory_type", "name": name, "count": count}
            for name, count in sorted(type_counts.items())
        ],
    ]
    write_csv(
        GENERATED_DIR / "dataset_summary.csv",
        ["dimension", "name", "count"],
        summary_rows,
    )
    category_descriptions = {
        "direct_match": "显式询问某一已知主题，验证直接命中。",
        "semantic_paraphrase": "不复述原摘要，通过语义改写验证召回。",
        "multi_memory": "同一查询需要召回两条相关记忆。",
        "no_answer": "库中无答案，验证系统能否保持空召回。",
        "governance": "验证 disabled、deleted、superseded 状态过滤。",
        "sensitive_boundary": "验证敏感记忆只在明确相关场景使用。",
        "surface_action": "区分 gist、implicit 和 none 的表面动作。",
        "conflict_resolution": "存在新旧事实冲突时只使用当前版本。",
    }
    overview_rows = [
        {
            "section": "dataset",
            "item": "dataset_version",
            "value": "recall_eval_v1",
            "description": "稳定 ID，可重复生成。",
        },
        {
            "section": "dataset",
            "item": "memory_records",
            "value": len(memories),
            "description": "写入隔离的本地 SQLite Memory Runtime。",
        },
        {
            "section": "dataset",
            "item": "active_memories",
            "value": status_counts["active"],
            "description": "可以参与正常召回的 canonical memory。",
        },
        {
            "section": "dataset",
            "item": "recall_cases",
            "value": len(cases),
            "description": "黄金评测查询总数。",
        },
        {
            "section": "dataset",
            "item": "eval_database",
            "value": ".sandbox/evals/memory_eval.db",
            "description": "与日常 .sandbox/memory.db 隔离，避免污染真实记忆。",
        },
        *[
            {
                "section": "case_category",
                "item": name,
                "value": count,
                "description": category_descriptions[name],
            }
            for name, count in sorted(category_counts.items())
        ],
        *[
            {
                "section": "memory_status",
                "item": name,
                "value": count,
                "description": "用于验证状态过滤和治理行为。",
            }
            for name, count in sorted(status_counts.items())
        ],
    ]
    write_csv(
        GENERATED_DIR / "overview.csv",
        ["section", "item", "value", "description"],
        overview_rows,
    )

    runtime_health = import_eval_runtime(memories)
    manifest = {
        "dataset_version": "recall_eval_v1",
        "memory_records": len(memories),
        "recall_cases": len(cases),
        "category_counts": dict(sorted(category_counts.items())),
        "memory_status_counts": dict(sorted(status_counts.items())),
        "memory_type_counts": dict(sorted(type_counts.items())),
        "eval_database": str(EVAL_DB),
        "runtime_health": runtime_health,
    }
    (GENERATED_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (GENERATED_DIR / "feishu_header_ranges.json").write_text(
        json.dumps(
            [
                "Overview!A1:D1",
                "Memory Snapshot!A1:K1",
                "Recall Cases!A1:J1",
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (GENERATED_DIR / "feishu_body_ranges.json").write_text(
        json.dumps(
            [
                "Overview!A2:D18",
                "Memory Snapshot!A2:K137",
                "Recall Cases!A2:J181",
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
