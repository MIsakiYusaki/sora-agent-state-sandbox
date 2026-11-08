import { CompanionGovernance } from "./CompanionGovernance";
import {
  Activity,
  ArrowLeft,
  Braces,
  BrainCircuit,
  Check,
  ChevronRight,
  CircleDot,
  CircleAlert,
  Database,
  Gauge,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  Network,
  RefreshCw,
  Route,
  SearchCode,
  ShieldCheck,
  TerminalSquare,
  TestTube2,
  Workflow,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  DesktopDebugBridge,
  DesktopDebugSnapshot,
  DesktopDebugSubsystem,
  GatewayEvent,
} from "../contracts/generated";
import { IconButton, Segmented, StatusDot, Tag } from "../components/ui";
import type { DesktopTransport } from "../transport";
import { TurnEvidencePanel } from "./TurnEvidence";
import { AdminCommandPanel } from "./AdminCommandPanel";
import { FrameInspector } from "./FrameInspector";
import { MemoryGovernancePanel } from "./MemoryGovernancePanel";
import { QualityInspector } from "./QualityInspector";
import { MemoryQueuePanel } from "./MemoryQueuePanel";
import type { GovernanceKind } from "../transport/DesktopTransport";

type DeveloperTab = "timeline" | "frames" | "routes" | "memory" | "async" | "state" | "quality" | "console";
type StateDomain = "persona" | "affect" | "relationship" | "worldops";

interface DiagnosticEvent {
  sequence: number;
  eventType: string;
  createdAtMs: number;
  payloadFields: string[];
}

interface DiagnosticModelCall {
  callId: string;
  logicalTask: string;
  route: string;
  model: string | null;
  status: string;
  thinkingDisabled: boolean | null;
  promptTokens: number | null;
  completionTokens: number | null;
  latencyMs: number | null;
  errorCode: string | null;
  createdAtMs: number | null;
}

interface DiagnosticFrame {
  frameId: string;
  schemaVersion: string | null;
  runtimeMode: string | null;
  revision: string | number | null;
  fieldCount: number | null;
  itemCount: number | null;
  fields: string[];
}

interface DiagnosticTurn {
  turnId: string;
  correlationId: string;
  status: string;
  stage: string;
  turnKind: string;
  createdAtMs: number;
  updatedAtMs: number;
  committedAtMs: number | null;
  terminal: boolean;
  durationMs: number;
  firstOutputLatencyMs: number | null;
  events: DiagnosticEvent[];
  modelCalls: DiagnosticModelCall[];
  frames: DiagnosticFrame[];
  contextContract: Readonly<Record<string, unknown>>;
  evidence: Readonly<Record<string, unknown>>;
}

interface TimelineSpan {
  id: string;
  name: string;
  category: "gateway" | "model";
  startAtMs: number;
  endAtMs: number;
  status: string;
  detail: string;
  tone: "mint" | "blue" | "violet" | "amber";
}

const tabs: ReadonlyArray<{ id: DeveloperTab; label: string; icon: typeof Activity }> = [
  { id: "timeline", label: "单轮时间线", icon: Activity },
  { id: "frames", label: "上下文帧", icon: Braces },
  { id: "routes", label: "模型路由", icon: Route },
  { id: "memory", label: "记忆流水线", icon: BrainCircuit },
  { id: "async", label: "异步任务", icon: Workflow },
  { id: "state", label: "状态系统", icon: Layers3 },
  { id: "quality", label: "评测与发布", icon: TestTube2 },
  { id: "console", label: "受控控制台", icon: TerminalSquare },
];

export function DeveloperView({
  conversationId,
  events,
  transport,
  onExit,
}: {
  conversationId: string;
  events: ReadonlyArray<GatewayEvent>;
  transport: DesktopTransport;
  onExit: () => void;
}) {
  const [activeTab, setActiveTab] = useState<DeveloperTab>("timeline");
  const [snapshot, setSnapshot] = useState<DesktopDebugSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshEpoch, setRefreshEpoch] = useState(0);

  const refresh = useCallback(async () => {
    setRefreshEpoch((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const collect = async () => {
      setLoading(true);
      try {
        const next = await transport.getDebugSnapshot(conversationId);
        if (!cancelled) {
          setSnapshot(next);
          setError(null);
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法读取运行快照");
      } finally {
        if (!cancelled) {
          setLoading(false);
          // Schedule after completion: slow Gateway calls must never pile up.
          timer = window.setTimeout(() => void collect(), 2000);
        }
      }
    };
    void collect();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [conversationId, transport, refreshEpoch]);

  useEffect(() => {
    setSnapshot(null);
    setError(null);
  }, [conversationId, transport]);

  const currentCorrelation = events.at(-1)?.correlation_id
    ?? latestDiagnosticTurn(snapshot)?.correlationId
    ?? "等待当前轮次";
  const visibleBridges = snapshot
    ? snapshot.bridges.filter((item) => item.status !== "pending").length
    : null;

  return (
    <main className="developer-page">
      <header className="developer-header">
        <div className="developer-header__identity">
          <IconButton label="返回对话" onClick={onExit}><ArrowLeft size={19} /></IconButton>
          <span className="developer-header__mark"><SearchCode size={19} /></span>
          <div>
            <strong>运行时检查器</strong>
            <span><StatusDot tone={error ? "danger" : snapshot ? "positive" : "warning"} />{error ? "快照读取失败" : snapshot ? "真实运行快照已连接" : "正在读取运行状态"}</span>
          </div>
        </div>
        <dl className="developer-context">
          <div><dt>配置</dt><dd>{snapshot?.profile_id_hash ?? "读取中"}</dd></div>
          <div><dt>对话</dt><dd>{conversationId}</dd></div>
          <div><dt>关联 ID</dt><dd>{currentCorrelation}</dd></div>
        </dl>
        <button className="dev-button" type="button" disabled={loading} onClick={() => void refresh()}>
          {loading ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
          刷新快照
        </button>
      </header>

      <div className="developer-shell">
        <nav className="developer-nav" aria-label="运行时检查器">
          <div className="developer-nav__label">生产诊断</div>
          {tabs.map(({ id, label, icon: Icon }) => (
            <button key={id} className={activeTab === id ? "is-active" : ""} type="button" aria-label={label} title={label} aria-current={activeTab === id ? "page" : undefined} onClick={() => setActiveTab(id)}>
              <Icon size={17} /><span>{label}</span>
            </button>
          ))}
          <div className="developer-capability">
            <LockKeyhole size={16} />
            <span><strong>{visibleBridges === null ? "诊断桥读取中" : `${visibleBridges} 个诊断桥可见`}</strong><small>身份隔离 · L0 脱敏 · 无模拟指标</small></span>
          </div>
        </nav>
        <section className="developer-content">
          {snapshot && <p className="dev-snapshot-freshness" role={error ? "status" : undefined}>
            {error ? "刷新失败，以下保留上次快照，不代表当前状态。" : "自动刷新，每次完成后间隔 2 秒。"}
            {` 上次采集：${formatTime(snapshot.collected_at_ms)}`}
          </p>}
          {loading && !snapshot ? <DeveloperLoading /> : error && !snapshot ? <DeveloperError error={error} onRetry={refresh} /> : snapshot ? (
            <DeveloperPanel tab={activeTab} snapshot={snapshot} liveEvents={events} transport={transport} />
          ) : null}
        </section>
      </div>
    </main>
  );
}

function DeveloperPanel({ tab, snapshot, liveEvents, transport }: { tab: DeveloperTab; snapshot: DesktopDebugSnapshot; liveEvents: ReadonlyArray<GatewayEvent>; transport: DesktopTransport }) {
  if (tab === "timeline") return <TimelinePanel snapshot={snapshot} liveEvents={liveEvents} />;
  if (tab === "frames") return <FramesPanel snapshot={snapshot} transport={transport} />;
  if (tab === "routes") return <RoutesPanel snapshot={snapshot} />;
  if (tab === "memory") return <MemoryPipelinePanel snapshot={snapshot} transport={transport} />;
  if (tab === "async") return <AsyncPanel record={snapshot.async_work} />;
  if (tab === "state") return <StateSystemsPanel snapshot={snapshot} transport={transport} />;
  if (tab === "quality") return <QualityPanel snapshot={snapshot} transport={transport} />;
  return <AdminCommandPanel transport={transport} />;
}

function TimelinePanel({ snapshot, liveEvents }: { snapshot: DesktopDebugSnapshot; liveEvents: ReadonlyArray<GatewayEvent> }) {
  const [clock, setClock] = useState(Date.now());
  const turns = useMemo(() => diagnosticTurns(snapshot, liveEvents, clock), [snapshot, liveEvents, clock]);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const effectiveTurnId = turns.some((turn) => turn.turnId === selectedTurnId) ? selectedTurnId : turns[0]?.turnId ?? null;
  const selectedTurn = turns.find((turn) => turn.turnId === effectiveTurnId) ?? null;
  const spans = useMemo(() => selectedTurn ? timelineSpans(selectedTurn) : [], [selectedTurn]);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const selectedSpan = spans.find((span) => span.id === selectedSpanId) ?? spans[0] ?? null;

  useEffect(() => {
    if (!turns.some((turn) => !turn.terminal && !Object.keys(turn.evidence).length)) return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [turns]);

  if (!selectedTurn) {
    return (
      <div className="dev-panel">
        <PanelHeader title="单轮时间线" description="运行时执行、交付事件与提交后消费证据；未采集的阶段不推算。" tag={<Tag tone="neutral">无观测轮次</Tag>} />
        <EmptyDiagnostic>当前对话还没有可观测轮次。发送消息后，运行时会在这里保存真实时间线。</EmptyDiagnostic>
      </div>
    );
  }

  const timelineEnd = Math.max(selectedTurn.createdAtMs + 1, selectedTurn.createdAtMs + selectedTurn.durationMs);
  return (
    <div className="dev-panel">
      <PanelHeader title="单轮时间线" description="按轮次查看从请求创建、上下文组装、模型调用、输出到原子提交的真实耗时。" tag={<span className={`live-indicator ${selectedTurn.terminal ? "" : "is-live"}`}><i />{selectedTurn.terminal ? statusLabel(selectedTurn.status) : "实时采集中"}</span>} />
      <div className="dev-turn-strip" aria-label="选择诊断轮次">
        {turns.map((turn, index) => (
          <button key={turn.turnId} type="button" className={turn.turnId === effectiveTurnId ? "is-active" : ""} onClick={() => { setSelectedTurnId(turn.turnId); setSelectedSpanId(null); }}>
            <span>轮次 {turns.length - index}</span>
            <strong>{formatDuration(turn.durationMs)}</strong>
            <small>{formatTime(turn.createdAtMs)} · {statusLabel(turn.status)}</small>
          </button>
        ))}
      </div>
      <div className="dev-metric-grid">
        <MetricCard label="前台轮次耗时" value={formatDuration(selectedTurn.durationMs)} note={selectedTurn.terminal ? "运行时创建 → 终态（不含后台）" : "运行时创建 → 快照时刻"} />
        <MetricCard label="首个交付事件" value={selectedTurn.firstOutputLatencyMs === null ? "未观测" : formatDuration(selectedTurn.firstOutputLatencyMs)} note="非模型首 token，非屏幕绘制耗时" />
        <MetricCard label="前台路由调用" value={Array.isArray(selectedTurn.evidence.model_calls) ? String(selectedTurn.modelCalls.length) : "未观测"} note="不含独立 Memory Provider 或后台调用" />
        <MetricCard label="事件节点" value={String(selectedTurn.events.length)} note={`阶段 ${selectedTurn.stage}`} />
      </div>

      <TurnEvidencePanel key={selectedTurn.turnId} execution={recordValue(selectedTurn.evidence, "execution")} legacy={recordValue(selectedTurn.evidence, "legacy_evidence")} asyncExecutions={objectArray(selectedTurn.evidence, "async_executions")} postTurn={objectArray(selectedTurn.evidence, "post_turn")} modelCalls={objectArray(selectedTurn.evidence, "model_calls")} memoryJobs={recordValue(selectedTurn.evidence, "memory_jobs")} />

      <section className="waterfall-panel">
        <header><div><strong>交付事件时间点</strong><span>{selectedTurn.correlationId} · {turnKindLabel(selectedTurn.turnKind)} · 事件不代表阶段区间</span></div><Tag tone={statusTone(selectedTurn.status)}>{statusLabel(selectedTurn.status)}</Tag></header>
        <div className="waterfall-scale"><span>0</span><span>{formatDuration(selectedTurn.durationMs * .25)}</span><span>{formatDuration(selectedTurn.durationMs * .5)}</span><span>{formatDuration(selectedTurn.durationMs * .75)}</span><span>{formatDuration(selectedTurn.durationMs)}</span></div>
        <div className="waterfall-list">
          {spans.map((span) => {
            const left = ((span.startAtMs - selectedTurn.createdAtMs) / Math.max(1, timelineEnd - selectedTurn.createdAtMs)) * 100;
            const width = ((span.endAtMs - span.startAtMs) / Math.max(1, timelineEnd - selectedTurn.createdAtMs)) * 100;
            return (
              <button key={span.id} className={selectedSpan?.id === span.id ? "is-selected" : ""} type="button" onClick={() => setSelectedSpanId(span.id)}>
                <span className="waterfall-name">{span.name}</span>
                <span className="waterfall-track"><i className={`tone-${span.tone}`} style={{ left: `${Math.min(99, Math.max(0, left))}%`, width: `${Math.min(100 - left, Math.max(.8, width))}%` }} /></span>
                <span className="waterfall-duration">时间点</span>
              </button>
            );
          })}
        </div>
      </section>

      {selectedSpan && (
        <section className="span-inspector">
          <header><div><span className="dev-kicker">当前节点 · {selectedSpan.category === "model" ? "模型调用" : "Gateway / Runtime"}</span><h2>{selectedSpan.name}</h2></div><span>{selectedSpan.id}</span></header>
          <div className="span-inspector__grid">
            <dl><div><dt>开始</dt><dd>{formatPreciseTime(selectedSpan.startAtMs)}</dd></div><div><dt>结束</dt><dd>{formatPreciseTime(selectedSpan.endAtMs)}</dd></div></dl>
            <dl><div><dt>耗时</dt><dd>事件无区间耗时</dd></div><div><dt>状态</dt><dd>{statusLabel(selectedSpan.status)}</dd></div></dl>
          </div>
          <div className="payload-preview"><span>脱敏诊断信息</span><pre>{selectedSpan.detail}</pre></div>
        </section>
      )}
    </div>
  );
}

function FramesPanel({ snapshot, transport }: { snapshot: DesktopDebugSnapshot; transport: DesktopTransport }) {
  const turns = diagnosticTurns(snapshot, [], snapshot.collected_at_ms);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const turn = turns.find((item) => item.turnId === selectedTurnId) ?? turns[0] ?? null;
  const [selectedFrameId, setSelectedFrameId] = useState<string | null>(null);
  const groups = frameGroups(turn?.frames ?? []);
  const selectedFrame = turn?.frames.find((item) => item.frameId === selectedFrameId) ?? turn?.frames[0] ?? null;
  const frameBridge = bridge(snapshot, "typed_frames");
  const contract = turn?.contextContract ?? {};
  const frameCount = turn?.frames.length ?? 0;

  return (
    <div className="dev-panel">
      <PanelHeader title="上下文帧" description="检查本轮允许披露的字段值、来源、版本变化与实际预算；不透出原始证据或隐藏推理。" tag={<Tag tone={frameCount > 0 ? "mint" : "amber"}>{frameCount > 0 ? `${frameCount} 个真实帧` : bridgeLabel(frameBridge?.status)}</Tag>} />
      <TurnPicker turns={turns} selectedId={turn?.turnId ?? null} onSelect={(id) => { setSelectedTurnId(id); setSelectedFrameId(null); }} />
      <label className="frame-selection">选择帧<select aria-label="选择上下文帧" value={selectedFrame?.frameId ?? ""} onChange={event => setSelectedFrameId(event.target.value)}>{turn?.frames.map(frame => <option key={frame.frameId} value={frame.frameId}>{frameLabel(frame.frameId)}</option>)}</select></label>
      <FrameInspector key={`${snapshot.profile_id_hash}:${snapshot.conversation_id}:${turn?.turnId}:${selectedFrame?.frameId}`} transport={transport} conversationId={snapshot.conversation_id} profileHash={snapshot.profile_id_hash} turnId={turn?.turnId ?? null} correlationId={turn?.correlationId ?? null} frameId={selectedFrame?.frameId ?? null} previousTurnId={turns[turns.findIndex(item => item.turnId === turn?.turnId) + 1]?.turnId ?? null} />
      <div className="frame-flow" aria-label="上下文帧组装流程"><span>输入封装</span><ChevronRight size={15} /><span>策略帧</span><ChevronRight size={15} /><span>上下文组装</span><ChevronRight size={15} /><span>回应契约</span></div>
      <div className="frame-grid">
        {groups.map((group) => (
          <section className={`frame-block frame-block--${group.tone}`} key={group.name}>
            <header><div><CircleDot size={16} /><strong>{group.name}</strong></div><span>{group.frames.length} 个契约</span></header>
            <dl>
              {group.frames.length > 0 ? group.frames.map((frame) => <div key={frame.frameId}><dt>{frameLabel(frame.frameId)}</dt><dd>{frame.schemaVersion ?? "未声明版本"} · {displayValue(frame.fieldCount)} 字段</dd></div>) : <div><dt>本轮数据</dt><dd>未观测</dd></div>}
            </dl>
            <footer><button type="button" disabled={group.frames.length === 0} onClick={() => setSelectedFrameId(group.frames[0]?.frameId ?? null)}>检查字段</button><span>{group.frames.length > 0 ? "已从本轮日志验证" : "无帧记录"}</span></footer>
          </section>
        ))}
      </div>
      <section className="contract-inspector">
        <header><div><ShieldCheck size={18} /><span><strong>上下文契约</strong><small>{stringValue(contract, "schema_version", "本轮未保存上下文契约")}</small></span></div><Tag tone={frameCount > 0 ? "mint" : "neutral"}>{frameCount} 个帧已发现</Tag></header>
        <div className="contract-rules"><span><Check size={14} /> 隐藏推理不出运行时</span><span><Check size={14} /> 按需读取白名单字段值</span><span><Check size={14} /> 权限限定到当前配置</span><span><Check size={14} /> 原始证据保持加密</span></div>
      </section>
      <div className="frame-contract-grid">
        <section className="span-inspector">
          <header><div><span className="dev-kicker">帧结构</span><h2>{selectedFrame ? frameLabel(selectedFrame.frameId) : "未选择帧"}</h2></div><span>{selectedFrame?.schemaVersion ?? "—"}</span></header>
          <div className="payload-preview"><span>字段名（不含字段值）</span><pre>{selectedFrame ? selectedFrame.fields.join("\n") : "本轮没有可检查的帧结构。"}</pre></div>
        </section>
        <section className="span-inspector">
          <header><div><span className="dev-kicker">实际预算</span><h2>{displayValue(contract["token_estimate"])} tokens</h2></div><span>回应预留 {displayValue(contract["reply_budget_tokens"])}</span></header>
          <div className="payload-preview"><span>上下文池与裁剪</span><pre>{formatContextContract(contract)}</pre></div>
        </section>
      </div>
    </div>
  );
}

function RoutesPanel({ snapshot }: { snapshot: DesktopDebugSnapshot }) {
  const provider = system(snapshot, "model_provider");
  const bindings = observedObjectArray(snapshot.routes, "bindings");
  const latestTurn = latestDiagnosticTurn(snapshot);
  const calls = latestTurn?.modelCalls ?? [];
  const missing = observedStringArray(provider?.details ?? {}, "missing_conversation_workloads");
  const conversationReady = nullableBoolean((provider?.details ?? {})["conversation_ready"]);
  const routeStatus = conversationReady === true
    ? { tone: "mint" as const, label: "对话路由就绪" }
    : conversationReady === false
      ? { tone: "amber" as const, label: "路由需要关注" }
      : { tone: "neutral" as const, label: "路由状态未观测" };

  return (
    <div className="dev-panel">
      <PanelHeader title="模型路由" description="查看当前真实 workload 绑定、降级链与最近一轮模型调用账本。" tag={<Tag tone={routeStatus.tone}>{routeStatus.label}</Tag>} />
      <div className="route-summary">
        <div><Gauge size={20} /><span><strong>{conversationReady === true ? "当前对话所需路由完整" : conversationReady === false && missing !== null ? `${missing.length} 个必需 workload 缺失` : "当前无法判定路由完整性"}</strong><small>{provider?.summary ?? "未取得模型服务健康摘要"}</small></span></div>
        <div><span>绑定</span><strong>{bindings === null ? "未观测" : bindings.length}</strong></div>
        <div><span>注册表修订</span><strong>{displayValue(snapshot.routes["registry_revision"])}</strong></div>
      </div>
      <div className="dev-table dev-table--routes">
        <div className="dev-table__head"><span>Workload</span><span>主模型</span><span>要求</span><span>降级顺序</span><span>状态</span></div>
        {bindings === null ? <TableEmpty>路由注册表未观测，不能把缺失数据解释为零条绑定。</TableEmpty> : bindings.length === 0 ? <TableEmpty>当前路由注册表确实没有绑定。</TableEmpty> : bindings.map((binding) => {
          const workload = stringValue(binding, "workload", "unknown");
          const unavailable = missing === null ? null : missing.includes(workload);
          const requirements = observedStringArray(binding, "requirements");
          const fallbacks = observedStringArray(binding, "fallback_models");
          return <div className="dev-table__row" key={workload}><span><strong>{workloadLabel(workload)}</strong><small>{workload}</small></span><span className="mono">{stringValue(binding, "model", "未绑定")}</span><span>{requirements === null ? "未观测" : requirements.join(" · ") || "无额外要求"}</span><span>{fallbacks === null ? "未观测" : fallbacks.join(" → ") || "无"}</span><span><StatusDot tone={unavailable === true ? "danger" : unavailable === false ? "positive" : "warning"} />{unavailable === true ? "缺失" : unavailable === false ? "已绑定" : "未观测"}</span></div>;
        })}
      </div>
      <section className="fallback-chain">
        <header><strong>最近一轮模型调用</strong><span>{latestTurn ? `${latestTurn.correlationId} · ${calls.length} 次` : "无观测轮次"}</span></header>
        {calls.length === 0 ? <EmptyDiagnostic>最近一轮没有写入模型调用账本；这通常表示确定性路径或尚未开始模型调用。</EmptyDiagnostic> : (
          <div className="route-call-list">{calls.map((call, index) => <span key={call.callId}><b>{index + 1}</b><strong>{workloadLabel(call.logicalTask)}</strong><small>{call.model ?? "模型未报告"} · {formatDuration(call.latencyMs)} · {tokenUsage(call)}</small></span>)}</div>
        )}
      </section>
    </div>
  );
}

function MemoryPipelinePanel({ snapshot, transport }: { snapshot: DesktopDebugSnapshot; transport: DesktopTransport }) {
  const [governance, setGovernance] = useState<GovernanceKind | null>(null);
  const [queue, setQueue] = useState(false);
  const memory = snapshot.memory;
  const pendingExtraction = metricValue(memory, "pending_extraction_jobs");
  const pendingCandidates = metricValue(memory, "pending_candidates");
  const openReviews = metricValue(memory, "open_reviews");
  const activeQuarantines = metricValue(memory, "active_quarantines");
  const activeMemories = metricValue(memory, "active_memories");
  const indexPending = metricValue(memory, "index_pending");
  const projectorPending = metricValue(memory, "projector_pending");
  const derivedQueue = memoryDerivedQueueStatus(memory);
  const stages = [
    { name: "对话封装", detail: `${diagnosticTurns(snapshot, [], snapshot.collected_at_ms).length} 个可诊断轮次`, meta: "运行时日志", state: "clear" },
    { name: "模型抽取", detail: metricWithUnit(pendingExtraction, "个待抽取任务"), meta: "异步", state: metricStage(pendingExtraction) },
    { name: "候选生成", detail: metricWithUnit(pendingCandidates, "个候选"), meta: "真实水位", state: metricStage(pendingCandidates) },
    { name: "Memory Gate", detail: `${metricText(openReviews)} 待审核 · ${metricText(activeQuarantines)} 隔离`, meta: "治理队列", state: metricStage(openReviews) },
    { name: "主存储", detail: metricWithUnit(activeMemories, "条有效记忆"), meta: activeMemories === null ? "记录数量未观测" : "已读取记录数量", state: activeMemories === null ? "unknown" : "clear" },
    { name: "索引与投影", detail: `${metricText(indexPending)} 索引 · ${metricText(projectorPending)} 投影`, meta: `派生视图 · ${derivedQueue.label}`, state: derivedQueue.state },
  ];
  return (
    <div className="dev-panel">
      <PanelHeader title="记忆流水线" description="从轮次封装到候选、Gate、主存储、索引与投影的真实聚合水位。" tag={<Tag tone={healthTone(stringValue(memory, "status", ""))}>{statusLabel(stringValue(memory, "status", "unknown"))}</Tag>} />
      <div className="pipeline-flow">{stages.map((stage) => <div className="pipeline-stage" key={stage.name}><span className={`pipeline-stage__status ${stage.state === "active" ? "is-active" : stage.state === "error" ? "is-error" : stage.state === "unknown" ? "is-unknown" : ""}`}>{stage.state === "active" ? <LoaderCircle className="spin" size={15} /> : stage.state === "error" ? <CircleAlert size={15} /> : stage.state === "unknown" ? <CircleDot size={15} /> : <Check size={15} />}</span><div><strong>{stage.name}</strong><span>{stage.detail}</span><small>{stage.meta}</small></div></div>)}</div>
      <div className="governance-actions" role="group" aria-label="查看记忆治理记录">{(["reviews", "candidates", "quarantines"] as const).map(kind => <button className="dev-button" type="button" key={kind} aria-pressed={governance === kind} onClick={() => { setGovernance(kind); setQueue(false); }}>{kind === "reviews" ? "查看待审核" : kind === "candidates" ? "查看候选" : "查看隔离"}</button>)}<button className="dev-button" type="button" aria-pressed={queue} onClick={() => { setGovernance(null); setQueue(true); }}>查看索引与投影事件</button></div>
      {governance && <MemoryGovernancePanel key={governance} transport={transport} initialKind={governance} />}
      {queue && <MemoryQueuePanel transport={transport} />}
      <div className="memory-debug-grid">
        <section><header><div><BrainCircuit size={17} /><strong>候选与 Gate</strong></div><Tag tone={metricTone(openReviews)}>{openReviews === null ? "水位未观测" : `${openReviews} 待审核`}</Tag></header><dl><DataRow label="候选" value={pendingCandidates} /><DataRow label="隔离" value={activeQuarantines} /><DataRow label="待抽取" value={pendingExtraction} /><DataRow label="有效记忆" value={activeMemories} /></dl><p>这里显示真实队列水位，不读取或展示记忆正文。</p></section>
        <section aria-label="索引与投影队列状态"><header><div><Network size={17} /><strong>索引与投影</strong></div><Tag tone={derivedQueue.tone}>{derivedQueue.label}</Tag></header><dl><DataRow label="索引等待" value={indexPending} /><DataRow label="索引失败/死信" value={metricPair(memory, "index_failed", "index_dead_letter")} /><DataRow label="投影等待" value={projectorPending} /><DataRow label="投影失败/死信" value={metricPair(memory, "projector_failed", "projector_dead_letter")} /></dl><p>Canonical Store 是事实源；索引与投影是可重建派生状态。</p></section>
      </div>
    </div>
  );
}

function AsyncPanel({ record }: { record: Readonly<Record<string, unknown>> }) {
  const pending = metricValue(record, "pending");
  const failed = metricValue(record, "failed");
  const deadLetter = metricValue(record, "dead_letter");
  const aggregateStatus = deadLetter !== null && deadLetter > 0
    ? { tone: "red" as const, label: "存在死信" }
    : failed !== null && failed > 0
      ? { tone: "amber" as const, label: "存在失败" }
      : failed === 0 && deadLetter === 0
        ? record["failure_coverage_complete"] === true ? { tone: "mint" as const, label: "无失败积压" } : { tone: "neutral" as const, label: "已观测队列未见失败（覆盖不完整）" }
        : { tone: "neutral" as const, label: "失败水位未观测" };
  const rows = ["memory", "persona", "affect", "relationship", "worldops"].map((id) => ({ id, record: recordValue(record, id) }));
  return (
    <div className="dev-panel">
      <PanelHeader title="异步任务" description="后台抽取、索引、投影和状态系统 Worker 的真实队列与失败水位。" tag={<Tag tone={aggregateStatus.tone}>{aggregateStatus.label}</Tag>} />
      <div className="dev-metric-grid dev-metric-grid--three"><MetricCard label="等待处理" value={metricText(pending)} note="所有必要来源均可观测时才聚合" /><MetricCard label="失败" value={metricText(failed)} note="尚未进入死信" /><MetricCard label="死信" value={metricText(deadLetter)} note="需要运维定位" /></div>
      <div className="dev-table dev-table--jobs"><div className="dev-table__head"><span>系统</span><span>状态</span><span>等待</span><span>失败</span><span>Worker / 队列</span></div>{rows.map(({ id, record: item }) => <div className="dev-table__row" key={id}><span><strong>{fieldLabel(id)}</strong><small>{id}</small></span><span><StatusDot tone={healthTone(stringValue(item, "status", "")) === "mint" ? "positive" : "warning"} />{statusLabel(stringValue(item, "status", "unknown"))}</span><span>{asyncPending(item)}</span><span>{asyncFailures(item)}</span><span className="mono">{workerLabel(item)}</span></div>)}</div>
      <section className="outbox-monitor"><header><div><Database size={18} /><span><strong>事件消费者</strong><small>真实 Outbox、索引和投影积压</small></span></div><Tag tone={deadLetter === null ? "neutral" : deadLetter > 0 ? "red" : "mint"}>{deadLetter === null ? "死信未观测" : deadLetter > 0 ? "需要处理" : "未发现死信"}</Tag></header><div><span>Memory 索引 <b>{metricWithUnit(metricValue(recordValue(record, "memory"), "index_pending"), "待处理")}</b></span><span>Memory 投影 <b>{metricWithUnit(metricValue(recordValue(record, "memory"), "projector_pending"), "待处理")}</b></span><span>Affect Outbox <b>{metricWithUnit(metricValue(recordValue(record, "affect"), "outbox_pending"), "待处理")}</b></span><span>WorldOps Outbox <b>{metricWithUnit(metricValue(recordValue(record, "worldops"), "outbox_pending"), "待处理")}</b></span></div></section>
    </div>
  );
}

function StateSystemsPanel({ snapshot, transport }: { snapshot: DesktopDebugSnapshot; transport: DesktopTransport }) {
  const [domain, setDomain] = useState<StateDomain>("persona");
  const subsystem = system(snapshot, domain);
  const turn = latestDiagnosticTurn(snapshot);
  const frame = turn?.frames.find((item) => item.frameId === `${domain}_frame`) ?? null;
  return (
    <div className="dev-panel">
      <PanelHeader title="状态系统" description="人格、情绪、关系与叙事世界系统的运行健康，以及最近一轮实际帧契约。" tag={<Segmented value={domain} options={[{ value: "persona", label: "人格" }, { value: "affect", label: "情绪" }, { value: "relationship", label: "关系" }, { value: "worldops", label: "叙事世界" }]} onChange={setDomain} label="状态系统" />} />
      <div className="state-snapshot-layout">
        <section className="state-overview"><header><div><Layers3 size={18} /><span><strong>{subsystem?.label ?? fieldLabel(domain)}</strong><small>{subsystem?.summary ?? "没有取得系统摘要"}</small></span></div><Tag tone={subsystem?.status === "active" ? "mint" : subsystem?.status === "degraded" ? "amber" : "red"}>{subsystem ? systemStatusLabel(subsystem.status) : "未观测"}</Tag></header><dl className="state-health-grid">{entries(subsystem?.details ?? {}).map(([key, value]) => <div key={key}><dt>{fieldLabel(key)}</dt><dd>{displayValue(value)}</dd></div>)}</dl></section>
        <section className="state-dimensions"><header><strong>最近一轮帧契约</strong><span>{turn ? turn.correlationId : "无观测轮次"}</span></header>{frame ? <dl className="state-health-grid"><DataRow label="数据结构" value={frame.schemaVersion ?? "未声明"} /><DataRow label="运行模式" value={frame.runtimeMode ?? "未报告"} /><DataRow label="修订" value={frame.revision ?? "未报告"} /><DataRow label="字段数量" value={frame.fieldCount} /><DataRow label="集合项" value={frame.itemCount} /></dl> : <EmptyDiagnostic>最近一轮没有保存该状态帧；不会用推测分数补齐。</EmptyDiagnostic>}</section>
        <section className="contribution-log"><header><strong>帧字段覆盖</strong><span>只显示字段名，不显示用户内容或隐藏推理</span></header>{frame ? frame.fields.map((field) => <div key={field}><span className="contribution-delta">✓</span><span><strong>{field}</strong><small>运行时帧中存在</small></span><time>{frame.schemaVersion ?? "—"}</time></div>) : <EmptyDiagnostic>无可显示字段。</EmptyDiagnostic>}</section>
      </div>
      {(domain === "persona" || domain === "relationship") && <CompanionGovernance key={domain} transport={transport} domain={domain} />}
    </div>
  );
}

function QualityPanel({ snapshot, transport }: { snapshot: DesktopDebugSnapshot; transport: DesktopTransport }) {
  const quality = snapshot.quality;
  const suites = observedObjectArray(quality, "eval_suites");
  const runs = observedObjectArray(quality, "latest_runs");
  const baselines = observedObjectArray(quality, "baselines");
  const openEvents = observedObjectArray(quality, "open_events");
  const latestRun = runs?.find(run => stringValue(run, "run_type", "") === "release_gate");
  const latestStatus = latestRun ? stringValue(latestRun, "status", "unknown") : "unverified";
  const verified = latestStatus === "passed";
  const runEvidenceLabel = runs === null ? "运行记录未观测" : runs.length === 0 ? "尚无运行证据" : statusLabel(latestStatus);
  return (
    <div className="dev-panel">
      <PanelHeader title="评测与发布门禁" description="展示真实注册评测、最近运行、基线和开放质量事件；没有证据时绝不显示发布通过。" tag={<Tag tone={verified ? "mint" : latestRun ? "amber" : "neutral"}>{runEvidenceLabel}</Tag>} />
      <div className="release-gate"><div className="release-gate__status"><ShieldCheck size={29} /><span><strong>{latestRun ? `最近质量运行：${statusLabel(latestStatus)}` : "当前无法判定发布状态"}</strong><small>{latestRun ? `${stringValue(latestRun, "id", "未报告 ID")} · ${stringValue(latestRun, "created_at", "时间未报告")}` : runs === null ? "质量运行读取失败或未报告，不能把它解释为零次运行" : "需要实际执行并保存质量评测后才能形成发布证据"}</small></span><Tag tone={verified ? "mint" : "amber"}>{verified ? "有通过证据" : "未验证"}</Tag></div><div className="release-gate__checks"><span><Check size={14} /> {observedCount(suites, " 个已注册评测")}</span><span><Check size={14} /> {observedCount(runs, " 个最近运行")}</span><span><Check size={14} /> {observedCount(baselines, " 个可见基线")}</span><span><Check size={14} /> {observedCount(openEvents, " 个开放事件")}</span></div></div>
      <QualityInspector transport={transport} />
      <div className="quality-grid">
        <section><header><strong>已注册评测</strong><span>{observedCount(suites, " 个")}</span></header>{suites === null ? <EmptyDiagnostic>评测注册表未观测，不能把读取失败显示为零个。</EmptyDiagnostic> : suites.length === 0 ? <EmptyDiagnostic>当前评测注册表确实为空。</EmptyDiagnostic> : suites.map((suite, index) => <div key={`${stringValue(suite, "name", "unknown")}:${index}`}><span><StatusDot tone="positive" /><strong>{stringValue(suite, "name", "unknown")}</strong></span><em>{stringValue(suite, "default_severity", "未报告")}</em></div>)}</section>
        <section><header><strong>最近质量运行</strong><span>真实持久化记录</span></header>{runs === null ? <EmptyDiagnostic>最近运行记录未观测，不能推断发布门禁结果。</EmptyDiagnostic> : runs.length === 0 ? <EmptyDiagnostic>尚无最近运行，不能推断发布门禁结果。</EmptyDiagnostic> : runs.map((run, index) => <div key={`${stringValue(run, "id", "run")}:${index}`}><span>{stringValue(run, "run_type", "quality")}</span><em>{statusLabel(stringValue(run, "status", "unknown"))}</em></div>)}</section>
      </div>
      <section className="golden-case"><header><div><TestTube2 size={18} /><span><strong>开放质量事件</strong><small>需要调查的真实质量记录</small></span></div><Tag tone={openEvents === null ? "neutral" : openEvents.length > 0 ? "amber" : "mint"}>{observedCount(openEvents, " 个")}</Tag></header>{openEvents === null ? <EmptyDiagnostic>开放质量事件未观测，不能显示为零个。</EmptyDiagnostic> : openEvents.length === 0 ? <EmptyDiagnostic>当前快照确认没有开放质量事件。</EmptyDiagnostic> : openEvents.map((item, index) => <div key={`${stringValue(item, "id", "event")}:${index}`}><span>{stringValue(item, "severity", "unknown")}</span><p>{stringValue(item, "event_type", "quality_event")} · {stringValue(item, "status", "unknown")}</p></div>)}</section>
    </div>
  );
}

function PanelHeader({ title, description, tag }: { title: string; description: string; tag: ReactNode }) {
  return <header className="dev-panel-header"><div><h1>{title}</h1><p>{description}</p></div><div className="dev-panel-header__actions">{tag}</div></header>;
}

function MetricCard({ label, value, note }: { label: string; value: string; note: string }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong><small>{note}</small></div>;
}

function TurnPicker({ turns, selectedId, onSelect }: { turns: ReadonlyArray<DiagnosticTurn>; selectedId: string | null; onSelect: (id: string) => void }) {
  if (turns.length === 0) return <EmptyDiagnostic>当前对话还没有可诊断轮次。</EmptyDiagnostic>;
  return <div className="dev-turn-picker"><span>检查轮次</span><div>{turns.map((turn, index) => <button key={turn.turnId} className={turn.turnId === selectedId ? "is-active" : ""} type="button" onClick={() => onSelect(turn.turnId)}>轮次 {turns.length - index} · {formatTime(turn.createdAtMs)}</button>)}</div></div>;
}

function EmptyDiagnostic({ children }: { children: ReactNode }) {
  return <div className="developer-empty-state">{children}</div>;
}

function TableEmpty({ children }: { children: ReactNode }) {
  return <div className="dev-table-empty">{children}</div>;
}

function DataRow({ label, value }: { label: string; value: unknown }) {
  return <div><dt>{label}</dt><dd>{displayValue(value)}</dd></div>;
}

function DeveloperLoading() {
  return <div className="developer-loading"><LoaderCircle className="spin" size={22} /><span>正在读取当前运行栈</span></div>;
}

function DeveloperError({ error, onRetry }: { error: string; onRetry: () => Promise<void> }) {
  return <div className="developer-loading is-error"><span>{error}</span><button className="dev-button" type="button" onClick={() => void onRetry()}><RefreshCw size={15} />重试</button></div>;
}

function diagnosticTurns(snapshot: DesktopDebugSnapshot, liveEvents: ReadonlyArray<GatewayEvent>, now: number): DiagnosticTurn[] {
  const turns = objectArray(snapshot.timeline, "turns").map(parseDiagnosticTurn).filter((item): item is DiagnosticTurn => item !== null);
  const byId = new Map(turns.map((turn) => [turn.turnId, turn]));
  for (const event of liveEvents) {
    // Runtime and Gateway sequences have different owners. Do not merge them or
    // replace canonical timestamps with an older client event from the same turn.
    if (turns.some((turn) => turn.turnId === event.turn_id)) continue;
    let turn = byId.get(event.turn_id);
    if (!turn) {
      turn = { turnId: event.turn_id, correlationId: event.correlation_id, status: "active", stage: event.event_type, turnKind: "reactive", createdAtMs: event.created_at_ms, updatedAtMs: event.created_at_ms, committedAtMs: null, terminal: false, durationMs: 0, firstOutputLatencyMs: null, events: [], modelCalls: [], frames: [], contextContract: {}, evidence: {} };
      byId.set(event.turn_id, turn);
    }
    if (!turn.events.some((item) => item.sequence === event.sequence)) {
      turn.events.push({ sequence: event.sequence, eventType: event.event_type, createdAtMs: event.created_at_ms, payloadFields: Object.keys(event.payload ?? {}).sort() });
    }
    turn.events.sort((a, b) => a.sequence - b.sequence);
    turn.updatedAtMs = Math.max(turn.updatedAtMs, event.created_at_ms);
    turn.stage = event.event_type;
    if (["turn.committed", "response.completed", "turn.cancelled", "turn.failed"].includes(event.event_type)) {
      turn.terminal = true;
      turn.status = event.event_type === "turn.cancelled" ? "cancelled" : event.event_type === "turn.failed" ? "failed" : "committed";
      turn.committedAtMs = event.event_type === "turn.committed" ? event.created_at_ms : null;
    }
    if (turn.firstOutputLatencyMs === null && ["response.start", "response.delta"].includes(event.event_type)) {
      turn.firstOutputLatencyMs = Math.max(0, event.created_at_ms - turn.createdAtMs);
    }
    turn.durationMs = Math.max(0, (turn.terminal ? turn.updatedAtMs : now) - turn.createdAtMs);
  }
  for (const turn of byId.values()) {
    if (!turn.terminal && Object.keys(turn.evidence).length === 0) turn.durationMs = Math.max(turn.durationMs, now - turn.createdAtMs);
  }
  return [...byId.values()].sort((a, b) => b.createdAtMs - a.createdAtMs).slice(0, 20);
}

function parseDiagnosticTurn(value: Readonly<Record<string, unknown>>): DiagnosticTurn | null {
  const turnId = stringValue(value, "turn_id", "");
  const createdAtMs = metricValue(value, "created_at_ms");
  const updatedAtMs = metricValue(value, "updated_at_ms");
  const durationMs = metricValue(value, "duration_ms");
  const terminal = nullableBoolean(value["terminal"]);
  if (!turnId || createdAtMs === null || updatedAtMs === null || durationMs === null || terminal === null) return null;
  return {
    turnId,
    correlationId: stringValue(value, "correlation_id", "未报告关联 ID"),
    status: stringValue(value, "status", "unknown"),
    stage: stringValue(value, "stage", "unknown"),
    turnKind: stringValue(value, "turn_kind", "reactive"),
    createdAtMs,
    updatedAtMs,
    committedAtMs: nullableNumber(value["committed_at_ms"]),
    terminal,
    durationMs,
    firstOutputLatencyMs: nullableNumber(value["first_output_latency_ms"]),
    events: objectArray(value, "events").map(parseDiagnosticEvent).filter((event): event is DiagnosticEvent => event !== null),
    modelCalls: objectArray(value, "model_calls").map((call, index) => ({ callId: stringValue(call, "call_id", `${turnId}:call:${index}`), logicalTask: stringValue(call, "logical_task", "unknown"), route: stringValue(call, "route", "unknown"), model: nullableString(call["model"]), status: stringValue(call, "status", "unknown"), thinkingDisabled: nullableBoolean(call["thinking_disabled"]), promptTokens: metricValue(call, "prompt_tokens"), completionTokens: metricValue(call, "completion_tokens"), latencyMs: metricValue(call, "latency_ms"), errorCode: nullableString(call["error_code"]), createdAtMs: metricValue(call, "created_at_ms") })),
    frames: objectArray(value, "frames").map((frame) => ({ frameId: stringValue(frame, "frame_id", "unknown_frame"), schemaVersion: nullableString(frame["schema_version"]), runtimeMode: nullableString(frame["runtime_mode"]), revision: scalarValue(frame["revision"]), fieldCount: metricValue(frame, "field_count"), itemCount: metricValue(frame, "item_count"), fields: stringArray(frame, "fields") })),
    contextContract: recordValue(value, "context_contract"),
    evidence: value,
  };
}

function parseDiagnosticEvent(value: Readonly<Record<string, unknown>>): DiagnosticEvent | null {
  const sequence = metricValue(value, "sequence");
  const createdAtMs = metricValue(value, "created_at_ms");
  if (sequence === null || createdAtMs === null) return null;
  return {
    sequence,
    eventType: stringValue(value, "event_type", "unknown"),
    createdAtMs,
    payloadFields: stringArray(value, "payload_fields"),
  };
}

function timelineSpans(turn: DiagnosticTurn): TimelineSpan[] {
  // ClientEvents are instants, not spans. Never stretch one to the next event.
  return [...turn.events].sort((a, b) => a.sequence - b.sequence).map((event) => ({
    id: `${turn.turnId}:event:${event.sequence}`, name: eventLabel(event.eventType), category: "gateway",
    startAtMs: event.createdAtMs, endAtMs: event.createdAtMs,
    status: event.eventType.includes("failed") ? "failed" : event.eventType.includes("cancelled") ? "cancelled" : "completed",
    detail: `事件：${event.eventType}\n序列：#${event.sequence}\n字段：${event.payloadFields.join(" · ") || "无"}\n此记录只有事件时间点，没有阶段起止。`,
    tone: eventTone(event.eventType),
  }));
}

function frameGroups(frames: ReadonlyArray<DiagnosticFrame>) {
  const find = (...ids: string[]) => frames.filter((frame) => ids.includes(frame.frameId));
  return [
    { name: "轮次理解帧", tone: "blue", frames: find("turn_frame", "turn_interpretation") },
    { name: "可打断性帧", tone: "mint", frames: find("interruptibility_frame", "dialogue_control_frame", "context_use_policy") },
    { name: "记忆上下文帧", tone: "violet", frames: find("recollection_frame") },
    { name: "陪伴状态帧", tone: "amber", frames: find("persona_frame", "affect_frame", "relationship_frame", "worldops_frame") },
  ] as const;
}

function latestDiagnosticTurn(snapshot: DesktopDebugSnapshot | null): DiagnosticTurn | null {
  return snapshot ? diagnosticTurns(snapshot, [], snapshot.collected_at_ms)[0] ?? null : null;
}

function system(snapshot: DesktopDebugSnapshot, id: string): DesktopDebugSubsystem | undefined {
  return snapshot.systems.find((item) => item.subsystem_id === id);
}

function bridge(snapshot: DesktopDebugSnapshot, id: string): DesktopDebugBridge | undefined {
  return snapshot.bridges.find((item) => item.bridge_id === id);
}

function entries(record: Readonly<Record<string, unknown>>): ReadonlyArray<[string, unknown]> {
  return Object.entries(record);
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function recordValue(record: Readonly<Record<string, unknown>>, key: string): Readonly<Record<string, unknown>> {
  const value = record[key];
  return isRecord(value) ? value : {};
}

function objectArray(record: Readonly<Record<string, unknown>>, key: string): ReadonlyArray<Readonly<Record<string, unknown>>> {
  const value = record[key];
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function observedObjectArray(record: Readonly<Record<string, unknown>>, key: string): ReadonlyArray<Readonly<Record<string, unknown>>> | null {
  const value = record[key];
  return Array.isArray(value) ? value.filter(isRecord) : null;
}

function stringArray(record: Readonly<Record<string, unknown>>, key: string): string[] {
  const value = record[key];
  return Array.isArray(value) ? value.map(String) : [];
}

function observedStringArray(record: Readonly<Record<string, unknown>>, key: string): string[] | null {
  const value = record[key];
  return Array.isArray(value) ? value.map(String) : null;
}

function stringValue(record: Readonly<Record<string, unknown>>, key: string, fallback: string): string {
  const value = record[key];
  return typeof value === "string" ? value : fallback;
}

function metricValue(record: Readonly<Record<string, unknown>>, key: string): number | null {
  return nullableNumber(record[key]);
}

function nullableBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function scalarValue(value: unknown): string | number | null {
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未观测";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length > 0 ? value.map(String).join("、") : "无";
  if (isRecord(value)) return `${Object.keys(value).length} 项`;
  return String(value);
}

function formatContextContract(contract: Readonly<Record<string, unknown>>): string {
  if (Object.keys(contract).length === 0) return "本轮没有保存上下文预算元数据。";
  const pools = recordValue(contract, "pools");
  const dropped = observedStringArray(contract, "dropped");
  const lines = entries(pools).map(([key, value]) => `${fieldLabel(key)}: ${displayValue(value)} tokens`);
  lines.push(`裁剪记录: ${dropped === null ? "未观测" : dropped.length > 0 ? dropped.join(" · ") : "无"}`);
  return lines.join("\n");
}

function tokenUsage(call: DiagnosticModelCall): string {
  if (call.promptTokens === null || call.completionTokens === null) return "供应商未报告 token usage";
  return `${call.promptTokens} 输入 / ${call.completionTokens} 输出 tokens`;
}

function metricText(value: number | null): string {
  return value === null ? "未观测" : String(value);
}

function metricWithUnit(value: number | null, unit: string): string {
  return value === null ? "未观测" : `${value} ${unit}`;
}

function observedCount(value: ReadonlyArray<unknown> | null, suffix = ""): string {
  return value === null ? "未观测" : `${value.length}${suffix}`;
}

function memoryDerivedQueueStatus(memory: Readonly<Record<string, unknown>>): {
  state: "error" | "active" | "unknown" | "clear";
  tone: "red" | "amber" | "neutral" | "mint";
  label: string;
} {
  const dead = [metricValue(memory, "index_dead_letter"), metricValue(memory, "projector_dead_letter")];
  const failed = [metricValue(memory, "index_failed"), metricValue(memory, "projector_failed")];
  const pending = [metricValue(memory, "index_pending"), metricValue(memory, "projector_pending")];
  if (dead.some(value => value !== null && value > 0)) return { state: "error", tone: "red", label: "存在死信" };
  if (failed.some(value => value !== null && value > 0)) return { state: "error", tone: "red", label: "存在失败" };
  const incomplete = [...dead, ...failed, ...pending].some(value => value === null || value < 0);
  if (pending.some(value => value !== null && value > 0)) return { state: "active", tone: "amber", label: incomplete ? "存在积压 · 部分未观测" : "存在积压" };
  if (incomplete) return { state: "unknown", tone: "neutral", label: "无法判定" };
  return { state: "clear", tone: "mint", label: "水位为空" };
}

function metricStage(value: number | null): "active" | "clear" | "unknown" {
  return value === null ? "unknown" : value > 0 ? "active" : "clear";
}

function metricTone(value: number | null): "mint" | "amber" | "neutral" {
  return value === null ? "neutral" : value > 0 ? "amber" : "mint";
}

function metricPair(record: Readonly<Record<string, unknown>>, first: string, second: string): string {
  return `${metricText(metricValue(record, first))} / ${metricText(metricValue(record, second))}`;
}

function sumPresentMetrics(record: Readonly<Record<string, unknown>>, keys: ReadonlyArray<string>): number | null {
  const present = keys.filter((key) => Object.prototype.hasOwnProperty.call(record, key));
  if (present.length === 0) return null;
  const values = present.map((key) => metricValue(record, key));
  return values.every((value): value is number => value !== null)
    ? values.reduce((sum, value) => sum + value, 0)
    : null;
}

function asyncPending(record: Readonly<Record<string, unknown>>): string {
  return metricText(sumPresentMetrics(record, ["pending", "pending_extraction_jobs", "index_pending", "projector_pending", "outbox_pending", "write_queue_depth"]));
}

function asyncFailures(record: Readonly<Record<string, unknown>>): string {
  const failed = sumPresentMetrics(record, ["failed", "failed_extraction_jobs", "index_failed", "projector_failed", "outbox_failed"]);
  const dead = sumPresentMetrics(record, ["dead_letter", "index_dead_letter", "projector_dead_letter", "outbox_dead_letter"]);
  return `${metricText(failed)} / 死信 ${metricText(dead)}`;
}

function workerLabel(record: Readonly<Record<string, unknown>>): string {
  if (record["worker_error_present"] === true) return "存在运行错误，请检查任务记录";
  if (record["worker_alive"] === true) return "运行中";
  if (record["worker_alive"] === false) return "未运行";
  if (record["worker_enabled"] === true) return "已启用 · 状态未报告";
  return "状态未报告";
}

function fieldLabel(key: string): string {
  const labels: Record<string, string> = {
    memory: "记忆", persona: "人格", affect: "情绪状态", relationship: "关系连续性", worldops: "叙事世界", status: "状态",
    rollout_mode: "部署模式", deployment: "部署拓扑", worker_enabled: "Worker 已启用", worker_alive: "Worker 存活",
    queue_depth: "队列深度", pending: "等待处理", quarantined: "隔离", dead_letter: "死信",
    outbox_pending: "Outbox 等待", outbox_failed: "Outbox 失败", outbox_dead_letter: "Outbox 死信",
    write_queue_depth: "写队列深度", under_backpressure: "背压中", incomplete_consumer_effects: "未完成消费效果",
    open_sync_conflicts: "同步冲突", recent_turns: "近期对话", memory_pool: "记忆池", domain_frames: "状态帧", control_frames: "控制帧",
  };
  return labels[key] ?? key.replaceAll("_", " ");
}

function frameLabel(id: string): string {
  const labels: Record<string, string> = {
    turn_frame: "Turn Frame", turn_interpretation: "Turn Interpretation", interruptibility_frame: "Interruptibility Frame",
    dialogue_control_frame: "Dialogue Control Frame", context_use_policy: "Context Use Policy", recollection_frame: "Recollection Frame",
    persona_frame: "Persona Frame", affect_frame: "Affect Frame", relationship_frame: "Relationship Frame", worldops_frame: "WorldOps Frame",
    arbitration_result: "Arbitration Result",
  };
  return labels[id] ?? id;
}

function workloadLabel(id: string): string {
  const labels: Record<string, string> = {
    turn_interpretation: "轮次理解", semantic_arbitration: "语义仲裁", interruptibility_resolution: "可打断性解析",
    response_generation: "生成回应", memory_extraction: "记忆抽取", memory_recall_planning: "记忆召回规划",
    memory_surface: "记忆呈现", memory_consolidation: "记忆巩固", persona_candidate: "人格候选",
    affect_appraisal: "情绪评估", relationship_evidence: "关系证据", worldops_interpretation: "叙事世界解释",
    domain_background: "后台领域任务", embedding_documents: "文档向量化", embedding_query: "查询向量化", memory_rerank: "记忆重排",
  };
  return labels[id] ?? id;
}

function eventLabel(type: string): string {
  const labels: Record<string, string> = {
    "turn.accepted": "轮次接受", "turn.preparing": "上下文组装与生成", "turn.progress": "上下文组装与生成",
    "response.start": "验证后输出开始", "response.delta": "回应流式输出", "response.completed": "原子提交完成",
    "turn.committed": "原子提交完成", "turn.cancelled": "轮次取消", "turn.failed": "轮次失败",
  };
  return labels[type] ?? type;
}

function eventTone(type: string): TimelineSpan["tone"] {
  if (type.includes("failed") || type.includes("cancelled")) return "amber";
  if (type.startsWith("response")) return "blue";
  if (type.includes("committed") || type.includes("completed")) return "mint";
  return "violet";
}

function formatTime(timestamp: number): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(timestamp);
}

function formatPreciseTime(timestamp: number): string {
  return `${formatTime(timestamp)}.${String(Math.max(0, Math.floor(timestamp)) % 1000).padStart(3, "0")}`;
}

function formatDuration(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "未报告";
  const ms = Math.max(0, value);
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = { ok: "正常", ready: "就绪", healthy: "正常", active: "进行中", accepted: "已接受", preparing: "准备中", generating: "生成中", streaming: "输出中", committing: "提交中", committed: "已提交", completed: "已完成", passed: "通过", warning: "警告", degraded: "降级", cancelled: "已取消", error: "异常", failed: "失败", unavailable: "不可用", unknown: "未报告", unverified: "未验证" };
  return labels[status] ?? (status || "未报告");
}

function turnKindLabel(kind: string): string {
  return kind === "proactive" ? "主动轮次" : kind === "proactive_reserved" ? "预留主动轮次" : "用户文字轮次";
}

function systemStatusLabel(status: DesktopDebugSubsystem["status"]): string {
  return status === "active" ? "运行中" : status === "degraded" ? "降级" : "不可用";
}

function bridgeLabel(status: DesktopDebugBridge["status"] | undefined): string {
  return status === "available" ? "可用" : status === "partial" ? "部分可用" : status === "pending" ? "待桥接" : "未声明";
}

function healthTone(status: string): "mint" | "amber" | "red" | "neutral" {
  if (["ok", "ready", "healthy", "active", "passed", "completed", "committed"].includes(status)) return "mint";
  if (["error", "failed", "unavailable", "stopped"].includes(status)) return "red";
  return status ? "amber" : "neutral";
}

function statusTone(status: string): "mint" | "amber" | "red" | "neutral" {
  return healthTone(status);
}

function riskTone(risk: string): "amber" | "blue" | "neutral" {
  const normalized = risk.toLowerCase();
  return normalized.includes("3") || normalized.includes("high") ? "amber" : normalized ? "blue" : "neutral";
}
