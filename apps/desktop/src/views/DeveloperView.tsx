import { Activity, ArrowLeft, Braces, BrainCircuit, Layers3, LockKeyhole, Route, SearchCode, TerminalSquare, TestTube2, Workflow } from "lucide-react";
import { useState } from "react";
import { IconButton, StatusDot, Tag } from "../components/ui";

type DeveloperTab = "timeline" | "frames" | "routes" | "memory" | "async" | "state" | "quality" | "console";
const tabs: ReadonlyArray<{ id: DeveloperTab; label: string; icon: typeof Activity; description: string }> = [
  { id: "timeline", label: "单轮时间线", icon: Activity, description: "等待真实 Turn Snapshot / Event API，之后展示同一轮次的调用瀑布。" },
  { id: "frames", label: "上下文帧", icon: Braces, description: "等待类型化 Context Frame 快照接口；不会从普通对话状态猜测内部帧。" },
  { id: "routes", label: "模型路由", icon: Route, description: "Provider 配置已真实接通；逐轮路由、成本和降级链路仍需调试快照接口。" },
  { id: "memory", label: "记忆流水线", icon: BrainCircuit, description: "普通用户记忆控制已接通；候选、Gate、Outbox 和投影只会通过受控调试接口显示。" },
  { id: "async", label: "异步任务", icon: Workflow, description: "等待后台工作队列的低敏、身份隔离快照接口。" },
  { id: "state", label: "状态系统", icon: Layers3, description: "等待 Persona、Affect、Relationship 与 WorldOps 的版本化调试投影。" },
  { id: "quality", label: "评测与发布", icon: TestTube2, description: "等待 Module 10 Release Evidence 与 Gate 的只读调试接口。" },
  { id: "console", label: "受控控制台", icon: TerminalSquare, description: "等待能力注册表和审计命令接口；任意 Shell、SQL、Python、eval 与文件系统始终禁止。" },
];

export function DeveloperView({
  profileId,
  conversationId,
  onExit,
}: {
  profileId: string | null;
  conversationId: string;
  onExit: () => void;
}) {
  const [activeTab, setActiveTab] = useState<DeveloperTab>("timeline");
  const active = tabs.find((item) => item.id === activeTab) ?? tabs[0];
  const ActiveIcon = active.icon;
  return (
    <main className="developer-page">
      <header className="developer-header">
        <div className="developer-header__identity">
          <IconButton label="返回对话" onClick={onExit}><ArrowLeft size={17} /></IconButton>
          <span className="developer-header__mark"><SearchCode size={18} /></span>
          <div><strong>运行时检查器</strong><span><StatusDot tone="warning" /> 真实调试通道尚未接入</span></div>
        </div>
        <dl className="developer-context">
          <div><dt>配置</dt><dd>{profileId ?? "读取中"}</dd></div>
          <div><dt>对话</dt><dd>{conversationId}</dd></div>
          <div><dt>关联 ID</dt><dd>等待当前轮次</dd></div>
        </dl>
      </header>
      <div className="developer-shell">
        <nav className="developer-nav" aria-label="运行时检查器">
          <div className="developer-nav__label">调试工具</div>
          {tabs.map(({ id, label, icon: Icon }) => <button key={id} className={activeTab === id ? "is-active" : ""} type="button" onClick={() => setActiveTab(id)}><Icon size={16} /><span>{label}</span></button>)}
          <div className="developer-capability"><LockKeyhole size={15} /><span><strong>能力尚未签发</strong><small>只显示接口状态</small></span></div>
        </nav>
        <section className="developer-content">
          <div className="dev-panel">
            <header className="dev-panel-header"><div><h1>{active.label}</h1><p>{active.description}</p></div><Tag tone="amber">未接入</Tag></header>
            <section className="developer-unavailable">
              <ActiveIcon size={30} />
              <h2>这里没有模拟数据</h2>
              <p>原型定义的页面结构已经迁入生产端。等对应的 capability-gated Snapshot/Event/Command 契约完成后，这个视图才会显示真实数据。</p>
              <dl>
                <div><dt>当前身份</dt><dd>{profileId ?? "正在读取"}</dd></div>
                <div><dt>当前对话</dt><dd>{conversationId}</dd></div>
                <div><dt>安全边界</dt><dd>不展示原始密钥、隐藏推理或任意执行入口</dd></div>
              </dl>
            </section>
          </div>
        </section>
      </div>
    </main>
  );
}
