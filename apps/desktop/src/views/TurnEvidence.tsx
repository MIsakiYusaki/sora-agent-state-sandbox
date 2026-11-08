import { useState } from "react";

type RecordValue = Readonly<Record<string, unknown>>;
const row = (v: unknown): RecordValue => v !== null && typeof v === "object" && !Array.isArray(v) ? v as RecordValue : {};
const rows = (v: unknown): RecordValue[] => Array.isArray(v) ? v.map(row) : [];
const number = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
const text = (v: unknown): string => typeof v === "string" ? v : "未报告";
const duration = (v: unknown): string => { const n = number(v); return n === null ? "未观测" : `${n.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} ms`; };
const timestamp = (v: unknown): string => { const n = number(v); return n === null ? "未观测" : new Date(n).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, fractionalSecondDigits: 3 }); };
const status = (v: unknown): string => ({ completed: "完成", running: "执行中", failed: "失败", cancelled: "取消", delivered: "已分发", pending: "待处理", processing: "处理中", dead_letter: "死信", ok: "正常", timeout: "超时", degraded: "降级", retry: "等待重试", superseded: "已失去租约", interrupted: "执行中断", end_unobserved: "结束未观测" }[text(v)] ?? text(v));

const names: Record<string, string> = {
  "memory.extractor.attempt": "记忆抽取作业执行", "memory.extractor.checkpoint_read": "读取抽取检查点",
  "memory.extractor.input": "准备抽取输入与相关记忆", "memory.extractor.provider": "抽取模型调用（含内部修复）",
  "memory.extractor.validate": "校验模型抽取结果", "memory.extractor.validate_checkpoint": "校验已保存结果",
  "memory.extractor.calibrate": "校准记忆写入决策", "memory.extractor.checkpoint_save": "保存已校验检查点",
  "memory.extractor.materialize_and_gate": "候选落地与 Gate 准入", "memory.extractor.complete": "完成抽取作业",
  "runtime.execute": "运行时执行", "runtime.history_read": "读取加密历史", "agent.run": "整轮回应编排",
  "context.assembly": "理解、上下文与裁决", "turn.interpretation": "本轮意图理解", "turn.frame": "构建轮次帧",
  "context.use_policy": "上下文使用策略", "relationship.initial_frame": "关系状态预读", "relationship.governed_frame": "按权限生成关系帧",
  "interruptibility.evaluate": "可打断性与用户控制", "session.context": "近期会话上下文", "origin.frame": "角色初始底色",
  "persona.frame": "人格帧", "affect.frame": "情绪与内部状态帧", "worldops.frame": "叙事世界帧",
  "memory.recall": "记忆召回全程", "recall.plan": "召回规划", "recall.retrieve": "候选检索", "recall.fts": "词法检索",
  "recall.vector": "向量检索", "recall.temporal": "时间检索", "recall.graph": "关联扩展", "recall.fuse": "多路结果融合",
  "recall.rerank": "候选重排", "recall.contextual_policy": "情绪与关系上下文约束", "recall.govern": "记忆准入过滤",
  "recall.surface": "记忆外显策略", "recall.pack": "记忆预算与压缩", "recall.persist_trace": "保存召回轨迹", "recall.access": "记录记忆访问",
  "event_bus.build": "构建事件总线", "signals.collect": "收集并行子系统信号", "initiative.window": "主动性窗口",
  "initiative.signals": "主动性信号", "arbitration.resolve": "信号裁决", "context.generation_gate": "生成上下文权限与预算门禁",
  "response.plan_and_validate": "生成回复并校验", "expression.route": "表达路由", "context.delivery_pack": "校验后交付打包",
  "runtime.atomic_commit": "整轮原子提交", "delivery.persist_event": "持久化交付事件", "delivery.runtime_sink": "运行时事件交付",
  "model.turn_interpretation": "理解模型调用", "model.semantic_arbitration": "语义裁决模型调用", "model.response_generation": "回答模型调用",
  "model.interruptibility_resolution": "权限语义解析调用", "model.domain_background": "领域模型调用",
  "post_turn.memory": "记忆提交后消费", "post_turn.affect": "情绪提交后消费", "post_turn.relationship": "关系提交后消费", "post_turn.worldops": "叙事提交后消费",
  "signal.dialogue": "对话信号", "signal.behavior": "行为信号", "signal.memory": "记忆信号", "signal.persona": "人格信号",
  "signal.affect": "情绪信号", "signal.relationship": "关系信号", "signal.worldops": "叙事信号", "signal.origin": "初始底色信号",
  _total: "Agent 总耗时", context_frames: "上下文帧组装（含子阶段）", turn_interpretation: "意图理解", memory_recall: "召回旧指标（不含后续轨迹写入）",
  subsystem_signals: "信号耗时聚合（非连续）", response_planner: "回复规划", state_commit: "Agent 工作状态更新（不是原子提交）",
};
const label = (v: unknown) => names[text(v)] ?? text(v);
const reasonNames: Record<string, string> = {
  active_visibility: "本轮活动信号", background_visibility: "仅作背景",
  initiative_allowed_semantic_pass: "主动性许可且语义通过", semantic_judge_promoted: "语义裁决选中",
  semantic_judge_background: "语义裁决保留为背景", interruptibility_initiative_blocked: "用户权限禁止主动性",
  interruptibility_expansion_blocked: "用户权限禁止延伸", surface_silent: "对应状态禁止外显",
  greeting_no_worldops: "问候不插入世界事件", world_lore_not_event: "允许世界设定回答，但未命中具体事件",
  worldops_scene_restricted: "当前场景不允许世界事件展开", priority_or_visibility: "优先级或可见性不足",
};

const decisionNames: Record<string, string> = {
  interpretation_mode: "理解路径", references_previous_context: "声明依赖上文", resolved_target_count: "已解析指代数",
  unresolved_reference_count: "未解析指代数", recalled_item_count: "召回记忆数", memory_surface: "记忆呈现策略",
  recall_degraded: "召回是否降级", selected_signal_count: "选中信号数", background_signal_count: "背景信号数",
  suppressed_signal_count: "抑制信号数", semantic_judgment_mode: "语义裁决路径", question_budget: "追问预算", worldops_expression: "世界事件表达",
  worldops_event_expressed: "世界事件实际表达", growth_event_created: "生成成长候选", memory_hook_created: "生成记忆钩子候选", initiative_allowed: "允许主动性",
};

export function TurnEvidencePanel({ execution, legacy, asyncExecutions, postTurn, modelCalls, memoryJobs = {} }: {
  execution: RecordValue; legacy: RecordValue; asyncExecutions: readonly RecordValue[]; postTurn: readonly RecordValue[]; modelCalls: readonly RecordValue[];
  memoryJobs?: RecordValue;
}) {
  const [filter, setFilter] = useState("");
  const spans = rows(execution.spans);
  const decisions = row(legacy.decisions);
  return <div className="turn-evidence">
    <section aria-label="真实执行区间">
      <h2>真实执行区间</h2>
      <p>起止在执行位置采集，耗时使用单调时钟。父阶段包含子阶段，并行阶段不能相加。完成只表示调用返回，业务降级请对照下方决策证据。</p>
      <label className="evidence-filter">筛选阶段<input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="例如：召回、persona、裁决" /></label>
      {spans.length ? <ExecutionTree execution={execution} filter={filter} /> : <p className="evidence-empty">本轮没有精确区间记录。旧版耗时与事件在下方单独展示，不推算缺失起止。</p>}
      {number(execution.dropped_count) !== null && Number(execution.dropped_count) > 0 && <p role="status">采集达到上限，另有 {String(execution.dropped_count)} 条未保留。</p>}
    </section>
    <section aria-label="本轮决策证据">
      <h2>本轮决策证据</h2>
      <p>来自本轮保存的上下文。没有值表示未观测，不代表否定或零；这里不透出消息、原始证据或隐藏推理。</p>
      {Object.keys(decisions).length ? <dl className="evidence-decisions">{Object.entries(decisions).map(([key, value]) => <div key={key}><dt>{decisionNames[key] ?? key}</dt><dd>{value === null ? "未观测" : value === true ? "是" : value === false ? "否" : String(value)}</dd></div>)}</dl> : <p>本轮尚无已保存的决策证据。</p>}
      {rows(legacy.signals).length > 0 && <details><summary>子系统信号状态（{rows(legacy.signals).length}）</summary><div className="evidence-facts">{rows(legacy.signals).map((item, index) => <div key={index}><strong>{label(`signal.${text(item.subsystem)}`)}</strong><span>{status(item.status)}</span><span>{duration(item.latency_ms)}</span><span>信号 {number(item.signal_count) ?? "未报告"}</span></div>)}</div></details>}
      {rows(legacy.signal_decisions).length > 0 && <details><summary>逐信号裁决（{rows(legacy.signal_decisions).length}）</summary><p>只显示来源、裁决和白名单规则；模型自由文本理由未透出。选中不等于已写入长期状态。</p><div className="evidence-facts">{rows(legacy.signal_decisions).map((item, index) => <div key={index}><strong>{label(`signal.${text(item.source)}`)}</strong><span>{{selected:"选中",background:"背景",suppressed:"抑制"}[text(item.decision)] ?? "未观测"}</span><span>{reasonNames[text(item.reason_code)] ?? "理由未投影"}</span></div>)}</div></details>}
    </section>
    <section aria-label="提交后工作">
      <h2>提交后工作</h2>
      <p>与前台耗时分开。消费回执不等于抽取、Gate、索引或人格更新全部完成。记忆抽取作业在下方单独核对，其他领域独立 worker 尚未关联。</p>
      {!postTurn.length && <p>本轮未保留提交后事件；不能据此判断所有后台任务已完成。</p>}
      {postTurn.map((event, index) => <div className="evidence-outbox" key={index}><strong>{status(event.status)} · 第 {number(event.attempts) ?? "?"} 次领取</strong><small>{text(event.event_id)}</small><div>{rows(event.consumers).map((consumer, i) => <span key={i}>{text(consumer.consumer_name)}：{status(consumer.status)}</span>)}</div></div>)}
      {asyncExecutions.map((item, index) => <details key={index}><summary>消费执行记录 · 尝试 {number(item.attempt) ?? "未报告"} · {timestamp(item.anchor_at_ms)}</summary><ExecutionTree execution={item} filter="" /></details>)}
      <MemoryJobs observation={memoryJobs} />
    </section>
    <details><summary>旧版耗时与模型账本（不推算起止）</summary>
      <p>context_frames 包含内部阶段；subsystem_signals 是非连续聚合。模型账本记录完成时刻与总耗时，不据此反推开始，也不视为供应商首 token 延迟。</p>
      <div className="evidence-facts">{rows(legacy.durations).map((item, index) => <div key={index}><strong>{label(item.name)}</strong><span>{duration(item.duration_ms)}</span><small>{text(item.name)}</small></div>)}
      {modelCalls.map((call, index) => <div key={`call-${index}`}><strong>{text(call.logical_task)}</strong><span>{duration(call.latency_ms)}</span><span>{status(call.status)}</span></div>)}</div>
      {!rows(legacy.durations).length && !modelCalls.length && <p>本轮尚未保存耗时账本。</p>}
    </details>
    <details><summary>Agent 执行事件（时间点，不推算区间）</summary><div className="evidence-facts">{(rows(execution.events).length ? rows(execution.events) : rows(legacy.milestones)).map((event, index) => <div key={index}><strong>{text(event.name)}</strong><span>{timestamp(event.at_ms)}</span></div>)}</div></details>
  </div>;
}

function MemoryJobs({ observation }: { observation: RecordValue }) {
  const jobs = rows(observation.jobs);
  return <section aria-label="本轮记忆抽取作业">
    <h3>本轮记忆抽取作业</h3>
    <p>按对话轮次和关联标识读取真实队列。完成抽取不等于全部候选已成为长期记忆；历史尝试不反推耗时。</p>
    {observation.status !== "available" ? <p>作业记录暂不可读取，不能判定后台已完成。</p> : <>
      {!jobs.length && <p>没有关联到本轮的抽取作业。可能尚未分发、被预过滤或未保留来源记录，不视为全部完成。</p>}
      {jobs.map((job, index) => <details className="evidence-memory-job" key={text(job.job_id)}>
        <summary>记忆抽取 {index + 1}：{status(job.status)} · 已尝试 {number(job.attempt_count) ?? "未报告"} 次</summary>
        <div className="evidence-outbox"><small>{text(job.job_id)}</small>
          <dl className="evidence-decisions">
            <div><dt>入队</dt><dd>{timestamp(job.created_at_ms)}</dd></div>
            <div><dt>最近领取</dt><dd>{timestamp(job.claimed_at_ms)}</dd></div>
            <div><dt>作业完成</dt><dd>{timestamp(job.completed_at_ms)}</dd></div>
            <div><dt>等待原因</dt><dd>{{ queued: "排队待领取", retry_backoff: "失败后退避", ingestion_pending: "等待来源接收完成" }[text(job.wait_reason)] ?? "未报告等待原因"}</dd></div>
            {job.wait_reason === "retry_backoff" && <div><dt>可重试时间</dt><dd>{timestamp(job.available_at_ms)}</dd></div>}
            <div><dt>错误分类</dt><dd>{job.error_code === null ? "未记录错误" : text(job.error_code)}</dd></div>
            <div><dt>恢复检查点</dt><dd>{job.checkpoint_available === true ? "已保留" : job.checkpoint_available === false ? "当前无检查点" : "未报告"}</dd></div>
          </dl>
        </div>
        {rows(job.attempts).map((attempt, i) => <details key={i}>
          <summary>尝试 {number(attempt.attempt) ?? "未报告"}：{status(attempt.outcome)}</summary>
          {typeof attempt.error_code === "string" && <p>错误分类：{text(attempt.error_code)}。不显示异常正文。</p>}
          <ExecutionTree execution={row(attempt.execution)} filter="" />
        </details>)}
        {!rows(job.attempts).length && <p>尚无保留的尝试执行树，不按作业时间戳推算阶段耗时。</p>}
        {job.attempts_truncated === true && <p>部分历史尝试没有保留，仅展示已观测记录。</p>}
      </details>)}
      {observation.truncated === true && <p>作业记录达到显示上限，当前列表不完整。</p>}
      {(number(observation.observation_write_failures) ?? 0) > 0 && <p role="status">本次进程有观测写入失败，部分执行区间可能缺失；业务状态仍以队列记录为准。</p>}
    </>}
  </section>;
}

function ExecutionTree({ execution, filter }: { execution: RecordValue; filter: string }) {
  const spans = rows(execution.spans);
  const validIds = new Set(spans.map((span) => text(span.span_id)));
  const total = Math.max(1, ...spans.map((span) => (number(span.start_offset_ms) ?? 0) + (number(span.duration_ms) ?? 0)));
  const query = filter.toLowerCase();
  function renderSpan(span: RecordValue, depth: number, ancestors: Set<string>): React.ReactNode {
    const id = text(span.span_id);
    if (ancestors.has(id) || depth > 20) return null;
    const children = spans.filter((item) => item.parent_id === id);
    const start = number(span.start_offset_ms);
    const elapsed = number(span.duration_ms);
    const unobservedEnd = span.status === "end_unobserved" || (span.status === "running" && execution.snapshot_state !== "live");
    const body = <><span className="evidence-span-label"><strong>{label(span.name)}</strong><small>{text(span.name)}</small></span><span className="evidence-span-bar" aria-hidden="true">{start !== null && elapsed !== null && <i style={{ left: `${100 * start / total}%`, width: `${100 * elapsed / total}%` }} />}</span><span className="evidence-span-duration">{elapsed === null ? "耗时未观测" : duration(elapsed)}<small>{unobservedEnd ? "结束未观测" : status(span.status)}</small></span></>;
    const detail = <dl className="evidence-span-detail"><div><dt>相对开始</dt><dd>{duration(span.start_offset_ms)}</dd></div><div><dt>实测结束</dt><dd>{timestamp(span.ended_at_ms)}</dd></div><div><dt>父阶段</dt><dd>{span.parent_id === null ? "根阶段" : text(span.parent_id)}</dd></div></dl>;
    return <details key={id} open={query || depth < 2 ? true : undefined} className="evidence-span" data-span-id={id}>
      <summary>{body}</summary>{detail}
      {!query && children.length > 0 && <div className="evidence-children">{children.map((child) => renderSpan(child, depth + 1, new Set([...ancestors, id])))}</div>}
    </details>;
  }
  const visible = query ? spans.filter((span) => `${label(span.name)} ${text(span.name)}`.toLowerCase().includes(query)) : spans.filter((span) => !validIds.has(text(span.parent_id)));
  return <div className="evidence-tree"><div className="evidence-axis"><span>阶段 / 子阶段</span><span>相对执行起点 0 → {duration(total)}</span><span>实测耗时</span></div>{visible.map((span) => renderSpan(span, 0, new Set()))}{!visible.length && <p>没有匹配的已采集阶段。</p>}</div>;
}
