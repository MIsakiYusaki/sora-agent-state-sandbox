import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Braces,
  BrainCircuit,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  Clock3,
  CloudCog,
  Database,
  Download,
  ExternalLink,
  FileClock,
  Gauge,
  GitCompareArrows,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  Network,
  Pause,
  Play,
  RefreshCw,
  Route,
  SearchCode,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  TestTube2,
  TimerReset,
  TriangleAlert,
  Workflow,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { debugCommands, timelineSpans } from "../data";
import { IconButton, Metric, Segmented, StatusDot, Switch, Tag } from "../components/ui";

type DeveloperTab =
  | "timeline"
  | "frames"
  | "routes"
  | "memory"
  | "async"
  | "state"
  | "quality"
  | "console";

type StateDomain = "persona" | "affect" | "relationship" | "worldops";

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

export function DeveloperView({ onExit }: { onExit: () => void }) {
  const [activeTab, setActiveTab] = useState<DeveloperTab>("timeline");
  const [live, setLive] = useState(true);

  return (
    <main className="developer-page">
      <header className="developer-header">
        <div className="developer-header__identity">
          <IconButton label="返回对话" onClick={onExit}><ArrowLeft size={17} /></IconButton>
          <span className="developer-header__mark"><SearchCode size={18} /></span>
          <div><strong>运行时检查器</strong><span><StatusDot tone="positive" /> 已连接本机运行时</span></div>
        </div>
        <dl className="developer-context">
          <div><dt>配置</dt><dd>profile_main</dd></div>
          <div><dt>对话</dt><dd>conv_8fc2</dd></div>
          <div><dt>关联 ID</dt><dd>cor_01K4M8QJ</dd></div>
        </dl>
        <div className="developer-header__actions">
          <button className="dev-button" type="button" onClick={() => setLive((current) => !current)}>
            {live ? <Pause size={14} /> : <Play size={14} />}{live ? "暂停实时刷新" : "继续实时刷新"}
          </button>
          <IconButton label="导出脱敏调试记录"><Download size={16} /></IconButton>
        </div>
      </header>

      <div className="developer-shell">
        <nav className="developer-nav" aria-label="运行时检查器">
          <div className="developer-nav__label">调试工具</div>
          {tabs.map(({ id, label, icon: Icon }) => (
            <button key={id} className={activeTab === id ? "is-active" : ""} type="button" onClick={() => setActiveTab(id)}>
              <Icon size={16} /><span>{label}</span>{id === "async" && <i>3</i>}
            </button>
          ))}
          <div className="developer-capability">
            <LockKeyhole size={15} />
            <span><strong>限定调试权限</strong><small>剩余 12:41</small></span>
          </div>
        </nav>

        <section className="developer-content">
          {activeTab === "timeline" && <TimelinePanel live={live} />}
          {activeTab === "frames" && <FramesPanel />}
          {activeTab === "routes" && <RoutesPanel />}
          {activeTab === "memory" && <MemoryPipelinePanel />}
          {activeTab === "async" && <AsyncPanel />}
          {activeTab === "state" && <StateSystemsPanel />}
          {activeTab === "quality" && <QualityPanel />}
          {activeTab === "console" && <ConsolePanel />}
        </section>
      </div>
    </main>
  );
}

function DevPanelHeader({ title, description, children }: { title: string; description: string; children?: React.ReactNode }) {
  return (
    <header className="dev-panel-header">
      <div><h1>{title}</h1><p>{description}</p></div>
      {children && <div className="dev-panel-header__actions">{children}</div>}
    </header>
  );
}

function TimelinePanel({ live }: { live: boolean }) {
  const [selected, setSelected] = useState("轮次理解");
  return (
    <div className="dev-panel">
      <DevPanelHeader title="单轮时间线" description="一条用户文字从对话入口、策略处理、模型回应到持久化的完整调用瀑布。">
        <span className={`live-indicator ${live ? "is-live" : ""}`}><i />{live ? "实时" : "已暂停"}</span>
        <button className="dev-button" type="button"><RefreshCw size={14} /> 重放本轮</button>
      </DevPanelHeader>
      <div className="dev-metric-grid">
        <Metric label="全链路耗时" value="1.82 s" note="p50 1.76 s" />
        <Metric label="首字延迟" value="486 ms" note="预算 650 ms" />
        <Metric label="热路径模型调用" value="2" note="思考模式已关闭" />
        <Metric label="后台任务" value="3" note="抽取 · 巩固 · 追踪" />
      </div>

      <section className="waterfall-panel">
        <header><div><strong>cor_01K4M8QJ</strong><span>22:18:04.031 · 文字轮次 · 8 个节点</span></div><Tag tone="mint">正常</Tag></header>
        <div className="waterfall-scale"><span>0 ms</span><span>450</span><span>900</span><span>1.35 s</span><span>1.8 s</span></div>
        <div className="waterfall-list">
          {timelineSpans.map((span) => (
            <button key={span.name} className={selected === span.name ? "is-selected" : ""} type="button" onClick={() => setSelected(span.name)}>
              <span className="waterfall-name">{span.name}</span>
              <span className="waterfall-track"><i className={`tone-${span.tone}`} style={{ left: `${span.start}%`, width: `${span.width}%` }} /></span>
              <span className="waterfall-duration">{span.duration}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="span-inspector">
        <header><div><span className="dev-kicker">当前节点</span><h2>{selected}</h2></div><span>span_061bf2</span></header>
        <div className="span-inspector__grid">
          <dl><div><dt>模型供应商</dt><dd>DeepSeek V4 Flash</dd></div><div><dt>任务类型</dt><dd>turn_interpreter</dd></div><div><dt>思考模式</dt><dd><Tag tone="mint">已关闭</Tag></dd></div><div><dt>缓存</dt><dd>未命中</dd></div></dl>
          <dl><div><dt>输入词元</dt><dd>412</dd></div><div><dt>输出词元</dt><dd>88</dd></div><div><dt>预估成本</dt><dd>$0.000043</dd></div><div><dt>重试次数</dt><dd>0</dd></div></dl>
        </div>
        <div className="payload-preview"><span>脱敏输出</span><pre>{`{\n  "intent": "companionship",\n  "response_shape": "gentle_acknowledgement",\n  "follow_up_permission": "none",\n  "memory_query": ["团子", "窗边"]\n}`}</pre></div>
      </section>
    </div>
  );
}

function FramesPanel() {
  const frames = [
    { name: "轮次理解帧", version: "TurnFrame v3", tone: "blue", rows: [["意图 intent", "陪伴 companionship"], ["话题 topic", "共同日常"], ["明确控制", "不追问"], ["置信度", "0.96"]] },
    { name: "可打断性帧", version: "InterruptibilityFrame v1", tone: "mint", rows: [["回应要求", "需要回应"], ["展开许可", "适度"], ["追问许可", "禁止"], ["主动会话租约", "有效 · 11 分钟"]] },
    { name: "记忆上下文帧", version: "MemoryContextFrame v4", tone: "violet", rows: [["已选择", "2 条记忆"], ["呈现方式", "仅隐式使用"], ["词元预算", "384 / 640"], ["已归档内容", "已排除"]] },
    { name: "陪伴状态帧", version: "CompanionStateFrame v2", tone: "amber", rows: [["人格", "温暖 · 克制"], ["情绪", "平静专注"], ["关系", "舒适节奏"], ["叙事世界", "未激活"]] },
  ];
  return (
    <div className="dev-panel">
      <DevPanelHeader title="上下文帧" description="运行时只读取版本化、可追溯的类型化帧，不直接拼接数据库状态。">
        <button className="dev-button" type="button"><GitCompareArrows size={14} /> 与上一轮对比</button>
      </DevPanelHeader>
      <div className="frame-flow" aria-label="上下文帧组装流程">
        <span>输入封装</span><ChevronRight size={14} /><span>策略帧</span><ChevronRight size={14} /><span>上下文组装</span><ChevronRight size={14} /><span>回应契约</span>
      </div>
      <div className="frame-grid">
        {frames.map((frame) => (
          <section className={`frame-block frame-block--${frame.tone}`} key={frame.name}>
            <header><div><CircleDot size={15} /><strong>{frame.name}</strong></div><span>{frame.version}</span></header>
            <dl>{frame.rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
            <footer><button type="button">查看数据结构</button><span>有效</span></footer>
          </section>
        ))}
      </div>
      <section className="contract-inspector">
        <header><div><ShieldCheck size={17} /><span><strong>契约校验</strong><small>必填字段完整，版本兼容。</small></span></div><Tag tone="mint">4 / 4 有效</Tag></header>
        <div className="contract-rules"><span><Check size={13} /> 不展示隐藏推理</span><span><Check size={13} /> 敏感证据已脱敏</span><span><Check size={13} /> 权限交集已应用</span><span><Check size={13} /> 来源版本已固定</span></div>
      </section>
    </div>
  );
}

function RoutesPanel() {
  const routes = [
    ["轮次理解", "deepseek-v4-flash", "思考关闭", "118 ms", "正常"],
    ["语义仲裁", "deepseek-v4-flash", "思考关闭", "142 ms", "正常"],
    ["生成回应", "deepseek-v4-pro", "自然模式", "1.42 s", "正常"],
    ["记忆抽取", "deepseek-v4-pro", "异步", "2.18 s", "排队中"],
    ["投影摘要", "deepseek-v4-pro", "异步", "未运行", "空闲"],
  ];
  return (
    <div className="dev-panel">
      <DevPanelHeader title="模型路由" description="按任务展示模型选择、思考策略、降级链路与本轮成本。">
        <button className="dev-button" type="button"><CloudCog size={14} /> 打开模型设置</button>
      </DevPanelHeader>
      <div className="route-summary">
        <div><Gauge size={18} /><span><strong>快速路径正常</strong><small>轮次理解 + 语义仲裁：共 260 ms</small></span></div>
        <div><span>本轮成本</span><strong>$0.00081</strong></div>
        <div><span>每日预估</span><strong>$0.14</strong></div>
      </div>
      <div className="dev-table dev-table--routes">
        <div className="dev-table__head"><span>任务</span><span>选定模型</span><span>策略</span><span>延迟</span><span>状态</span></div>
        {routes.map(([workload, model, policy, latency, status]) => (
          <button className="dev-table__row" type="button" key={workload}>
            <span><strong>{workload}</strong><small>{workload.toLowerCase().replaceAll(" ", "_")}</small></span>
            <span className="mono">{model}</span><span>{policy}</span><span>{latency}</span>
            <span><StatusDot tone={status === "正常" ? "positive" : "neutral"} />{status}</span>
          </button>
        ))}
      </div>
      <section className="fallback-chain">
        <header><strong>回应降级链路</strong><span>2 分钟前验证</span></header>
        <div><span><b>1</b>DeepSeek V4 Pro</span><ChevronRight size={15} /><span><b>2</b>DeepSeek Chat</span><ChevronRight size={15} /><span><b>3</b>安全回应模板</span></div>
      </section>
    </div>
  );
}

function MemoryPipelinePanel() {
  const stages = [
    { name: "对话封装", detail: "对话片段 1882 · 2 个轮次", status: "done", meta: "12 ms" },
    { name: "模型抽取", detail: "3 个候选 · 数据结构 v5", status: "done", meta: "异步 2.18 s" },
    { name: "四操作决策", detail: "新增 1 · 更新 1 · 忽略 1", status: "done", meta: "确定性" },
    { name: "准入流水线", detail: "已提交 1 · 待审核 1", status: "done", meta: "策略 v9" },
    { name: "主存储与事件箱", detail: "修订 418 · 4 个事件", status: "done", meta: "67 ms" },
    { name: "索引与投影", detail: "2 个消费者处理中", status: "active", meta: "异步" },
  ];
  return (
    <div className="dev-panel">
      <DevPanelHeader title="记忆流水线" description="从对话片段封装到主存储提交、索引与外部投影的完整证据链。">
        <button className="dev-button" type="button"><ExternalLink size={14} /> 打开审核队列</button>
      </DevPanelHeader>
      <div className="pipeline-flow">
        {stages.map((stage, index) => (
          <div className="pipeline-stage" key={stage.name}>
            <span className={`pipeline-stage__status is-${stage.status}`}>{stage.status === "active" ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}</span>
            <div><strong>{stage.name}</strong><span>{stage.detail}</span><small>{stage.meta}</small></div>
            {index < stages.length - 1 && <i />}
          </div>
        ))}
      </div>
      <div className="memory-debug-grid">
        <section>
          <header><div><BrainCircuit size={16} /><strong>候选 mem_c8e1</strong></div><Tag tone="amber">需要审核</Tag></header>
          <dl><div><dt>操作</dt><dd>更新</dd></div><div><dt>目标</dt><dd className="mono">mem_03</dd></div><div><dt>置信度</dt><dd>0.84</dd></div><div><dt>冲突度</dt><dd>0.31</dd></div></dl>
          <p>团子最近不再睡旧毛毯，转而睡在书架旁的纸箱里。</p>
          <footer><button type="button">检查证据</button><button type="button">对比目标记忆</button></footer>
        </section>
        <section>
          <header><div><Network size={16} /><strong>召回证据</strong></div><Tag tone="blue">RRF</Tag></header>
          <div className="retrieval-bars"><label><span>稠密向量</span><i><b style={{ width: "91%" }} /></i><em>0.91</em></label><label><span>FTS / BM25</span><i><b style={{ width: "78%" }} /></i><em>0.78</em></label><label><span>实体图</span><i><b style={{ width: "66%" }} /></i><em>0.66</em></label><label><span>时序</span><i><b style={{ width: "83%" }} /></i><em>0.83</em></label></div>
        </section>
      </div>
    </div>
  );
}

function AsyncPanel() {
  const jobs = [
    ["extract_episode", "episode_1882", "运行中", "43%", "worker-extract-01"],
    ["projection_apply", "memory:418", "运行中", "78%", "worker-project-01"],
    ["trace_compact", "2026-08-29", "排队中", "—", "worker-maint-01"],
    ["lifecycle_sweep", "profile_main", "已完成", "100%", "worker-life-01"],
  ];
  return (
    <div className="dev-panel">
      <DevPanelHeader title="异步任务" description="后台抽取、巩固、索引、投影与保留任务的队列和重放状态。">
        <button className="dev-button" type="button"><TimerReset size={14} /> 重试失败任务</button>
      </DevPanelHeader>
      <div className="dev-metric-grid dev-metric-grid--three"><Metric label="队列深度" value="3" note="正常" /><Metric label="最长延迟" value="2.4 s" note="SLO < 30 s" /><Metric label="死信" value="0" note="最近 24 小时" /></div>
      <div className="dev-table dev-table--jobs">
        <div className="dev-table__head"><span>任务</span><span>对象</span><span>状态</span><span>进度</span><span>工作进程</span></div>
        {jobs.map(([job, subject, status, progress, worker]) => <div className="dev-table__row" key={job}><span><strong>{job}</strong><small>第 1 次 / 共 5 次</small></span><span className="mono">{subject}</span><span><StatusDot tone={status === "运行中" ? "positive" : "neutral"} />{status}</span><span>{progress}</span><span className="mono">{worker}</span></div>)}
      </div>
      <section className="outbox-monitor"><header><div><Database size={17} /><span><strong>事件消费者</strong><small>至少一次投递 · 按事件编号保证幂等</small></span></div><Tag tone="mint">水位已对齐</Tag></header><div><span>向量索引 <b>修订 418</b></span><span>全文索引 <b>修订 418</b></span><span>投影路由 <b>修订 417</b></span><span>审计记录 <b>修订 418</b></span></div></section>
    </div>
  );
}

function StateSystemsPanel() {
  const [domain, setDomain] = useState<StateDomain>("persona");
  return (
    <div className="dev-panel">
      <DevPanelHeader title="状态系统" description="人格、情绪、关系与叙事世界外部系统的快照和投影贡献。">
        <Segmented value={domain} options={[{ value: "persona", label: "人格" }, { value: "affect", label: "情绪" }, { value: "relationship", label: "关系" }, { value: "worldops", label: "叙事世界" }]} onChange={setDomain} label="状态系统" />
      </DevPanelHeader>
      {domain === "persona" && <PersonaState />}
      {domain === "affect" && <AffectState />}
      {domain === "relationship" && <RelationshipState />}
      {domain === "worldops" && <WorldOpsState />}
    </div>
  );
}

function PersonaState() {
  return <StateSnapshot title="人格帧（PersonaFrame v2）" revision="persona:184" note="有界贡献 · 24 小时预算已使用 18%" rows={["温暖", "活泼", "克制", "好奇", "谨慎"]} values={[72, 48, 76, 61, 66]} labels={["温暖", "平衡", "克制", "专注", "谨慎"]} />;
}

function AffectState() {
  return <StateSnapshot title="情绪帧（AffectFrame v2）" revision="affect:912" note="短期状态 · 距下次衰减 8 分 42 秒" rows={["平静", "专注", "能量", "紧张", "开放"]} values={[82, 74, 43, 19, 67]} labels={["平静", "在场", "偏低", "稳定", "开放"]} />;
}

function RelationshipState() {
  return <StateSnapshot title="关系帧（RelationshipFrame v3）" revision="relationship:243" note="按用户隔离 · 不优化依赖指标" rows={["信任", "熟悉", "共同经历", "舒适节奏", "修复状态"]} values={[71, 68, 59, 81, 8]} labels={["已建立", "熟悉", "增长中", "舒适", "清晰"]} />;
}

function WorldOpsState() {
  return (
    <div className="worldops-state">
      <section className="state-overview"><header><div><Sparkles size={17} /><span><strong>叙事世界帧（WorldOpsFrame v1）</strong><small>worldops:77 · 服务端内容包 2026.08.3</small></span></div><Tag tone="neutral">未注入</Tag></header><p>当前对话未邀请共同叙事。客户端保留事件提示，不会把未激活内容伪装成共同经历。</p></section>
      <div className="worldops-timeline"><div className="is-complete"><i /><span><strong>内容包已接收</strong><small>content_pack_received · 「潮汐档案」· 编剧签名有效</small></span><time>18:02</time></div><div className="is-current"><i /><span><strong>已产生提示</strong><small>hinted · 一次轻量提示可用，尚未呈现</small></span><time>等待中</time></div><div><i /><span><strong>等待邀请</strong><small>invited · 需要用户明确参与</small></span><time>—</time></div><div><i /><span><strong>事件进行中</strong><small>active · 共同事件运行中</small></span><time>—</time></div></div>
    </div>
  );
}

function StateSnapshot({ title, revision, note, rows, values, labels }: { title: string; revision: string; note: string; rows: string[]; values: number[]; labels: string[] }) {
  return (
    <div className="state-snapshot-layout">
      <section className="state-overview"><header><div><Sparkles size={17} /><span><strong>{title}</strong><small>{revision}</small></span></div><Tag tone="mint">有效</Tag></header><p>{note}</p><div className="state-radar-placeholder" aria-label="状态维度雷达图"><i /><i /><i /><span>确定性快照</span></div></section>
      <section className="state-dimensions"><header><strong>状态维度</strong><span>归一化 0–100</span></header>{rows.map((row, index) => <label key={row}><span>{row}<small>{labels[index]}</small></span><i><b style={{ width: `${values[index]}%` }} /></i><em>{values[index]}</em></label>)}</section>
      <section className="contribution-log"><header><strong>最近贡献</strong><button type="button">查看全部</button></header><div><span className="contribution-delta is-positive">+0.018</span><span><strong>memory:417</strong><small>明确正向反馈 · 证据强度 0.92</small></span><time>22:14</time></div><div><span className="contribution-delta">-0.004</span><span><strong>decay_tick</strong><small>半衰期调整 · 确定性</small></span><time>22:00</time></div><div><span className="contribution-delta is-positive">+0.006</span><span><strong>episode:1880</strong><small>有界支持已接受</small></span><time>21:42</time></div></section>
    </div>
  );
}

function QualityPanel() {
  return (
    <div className="dev-panel">
      <DevPanelHeader title="评测与发布门禁" description="变更感知评测、黄金集、SLO 与发布基线差异。">
        <button className="dev-button" type="button"><Play size={14} /> 运行受影响评测</button>
      </DevPanelHeader>
      <div className="release-gate">
        <div className="release-gate__status"><CheckCircle2 size={28} /><span><strong>候选版本可以继续发布</strong><small>基线 main@8f3a2d1 · 候选 working-tree</small></span><Tag tone="mint">通过</Tag></div>
        <div className="release-gate__checks"><span><Check size={14} /> 8 个必需评测已通过</span><span><Check size={14} /> 0 个关键回归</span><span><Check size={14} /> 满足 SLO 预算</span><span><Check size={14} /> 迁移一致性已验证</span></div>
      </div>
      <div className="quality-grid">
        <section><header><strong>受影响评测</strong><span>8 / 8 通过</span></header>{[["conversation_e2e", "48 / 48", "pass"], ["interruptibility", "64 / 64", "pass"], ["surface_policy", "52 / 52", "pass"], ["memory_recall", "93 / 96", "warn"], ["persona_trajectory", "24 / 24", "pass"]].map(([name, score, status]) => <div key={name}><span><StatusDot tone={status === "pass" ? "positive" : "warning"} /><strong>{name}</strong></span><em>{score}</em></div>)}</section>
        <section><header><strong>相对基线变化</strong><span>正值通常更好</span></header>{[["召回 nDCG@10", "+1.8%"], ["多记忆 F1", "+0.6%"], ["错误记忆率", "−0.2%"], ["控制遵循率", "+0.0%"], ["首字延迟 p95", "−34 ms"]].map(([name, delta]) => <div key={name}><span>{name}</span><em className="is-positive">{delta}</em></div>)}</section>
      </div>
      <section className="golden-case"><header><div><FileClock size={17} /><span><strong>最接近失败的样本</strong><small>memory_recall / gradual-preference-017</small></span></div><Tag tone="amber">不阻塞</Tag></header><div><span>期望</span><p>夜间交流偏好应由 3 条跨 episode 证据联合召回。</p></div><div><span>实际</span><p>召回 2 条，较旧的一条被 temporal filter 提前裁剪。</p></div><footer><button type="button">打开追踪</button><button type="button">加入对比</button></footer></section>
    </div>
  );
}

function ConsolePanel() {
  const [selectedId, setSelectedId] = useState(debugCommands[0].id);
  const [dryRun, setDryRun] = useState(true);
  const [running, setRunning] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [outputs, setOutputs] = useState([{ id: 1, command: "memory.recall", time: "22:18:09", status: "ok", lines: ["从对话 conv_8fc2 生成查询", "4 个候选经 RRF 融合；2 个通过呈现策略", "Token 预算：384 / 640", "trace_id: recall_01K4M8R2"] }]);
  const selected = useMemo(() => debugCommands.find((command) => command.id === selectedId) ?? debugCommands[0], [selectedId]);
  const params = selected.id === "memory.explain" ? '{ "memory_id": "mem_03" }' : selected.id === "eval.run" ? '{ "suite": "affected", "baseline": "main" }' : '{ "conversation_id": "conv_8fc2", "limit": 8 }';

  const execute = () => {
    if (selected.risk === "L3" && !dryRun) { setConfirmOpen(true); return; }
    setRunning(true);
    window.setTimeout(() => {
      setOutputs((current) => [{ id: Date.now(), command: selected.id, time: "现在", status: "ok", lines: [dryRun ? "已生成预览计划；没有修改状态" : "命令执行完成", `profile=profile_main conversation=conv_8fc2`, `audit_id=admin_${Date.now().toString().slice(-6)}`] }, ...current]);
      setRunning(false);
    }, 700);
  };

  return (
    <div className="dev-panel">
      <DevPanelHeader title="受控控制台" description="只允许调用注册过的类型化命令，不提供系统命令、数据库查询、Python、任意代码执行或文件访问。">
        <span className="capability-pill"><LockKeyhole size={13} /> profile_main · 权限已限定</span>
      </DevPanelHeader>
      <div className="console-layout">
        <section className="command-registry">
          <header><strong>命令注册表</strong><span>{debugCommands.length} 个可用</span></header>
          {debugCommands.map((command) => <button key={command.id} className={selectedId === command.id ? "is-active" : ""} type="button" onClick={() => setSelectedId(command.id)}><span><strong>{command.label}</strong><small>{command.description}</small></span><Tag tone={command.risk === "L3" ? "amber" : command.risk === "L2" ? "blue" : "neutral"}>{command.risk}</Tag></button>)}
          <div className="forbidden-capabilities"><ShieldCheck size={15} /><span>设计上禁用</span><small>系统命令 · 数据库查询 · Python · 任意代码执行 · 原始密钥</small></div>
        </section>
        <section className="command-runner">
          <header><div><span className="dev-kicker">当前命令</span><h2>{selected.label}</h2></div><Tag tone={selected.risk === "L3" ? "amber" : "blue"}>{selected.risk} 风险</Tag></header>
          <label className="code-input"><span>类型化参数 · 数据结构校验通过</span><textarea value={params} readOnly spellCheck={false} /></label>
          <div className="runner-options"><label><Switch checked={dryRun} onChange={setDryRun} label="仅预览" /><span><strong>仅预览</strong><small>生成执行计划，不提交状态</small></span></label><span><LockKeyhole size={13} /> 必须审计</span></div>
          <button className="run-command" type="button" disabled={running} onClick={execute}>{running ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}{running ? "正在校验…" : dryRun ? "预览命令" : "执行命令"}</button>
        </section>
        <section className="console-output">
          <header><strong>输出</strong><button type="button" onClick={() => setOutputs([])}>清空</button></header>
          <div className="console-output__scroll">
            {outputs.length === 0 && <div className="console-empty">本次会话还没有命令输出。</div>}
            {outputs.map((output) => <article key={output.id}><header><span><CheckCircle2 size={14} /> {output.command}</span><time>{output.time}</time></header><pre>{output.lines.map((line) => `› ${line}`).join("\n")}</pre></article>)}
          </div>
        </section>
      </div>
      {confirmOpen && <RiskConfirmation onClose={() => setConfirmOpen(false)} onConfirm={() => { setConfirmOpen(false); setDryRun(true); execute(); }} />}
    </div>
  );
}

function RiskConfirmation({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section className="modal modal--risk" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="modal-icon modal-icon--warning"><TriangleAlert size={18} /></span><div><h2>高风险管理动作</h2><p>projection.rebuild 会替换测试空间的派生状态</p></div></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header>
        <div className="risk-summary"><AlertTriangle size={17} /><span>生产空间不可选；旧快照会保留用于回滚。建议先查看预览计划。</span></div>
        <footer><button className="button button--secondary" type="button" onClick={onClose}>取消</button><button className="button" type="button" onClick={onConfirm}>改为仅预览</button></footer>
      </section>
    </div>
  );
}
