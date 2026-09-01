import { ArrowLeft, BellOff, Check, Clock3, MessageCircle, Save, Sparkles, VolumeX } from "lucide-react";
import { useEffect, useState } from "react";
import type { ConversationControlUpdate, ConversationControlsView } from "../transport";
import { PageHeader, Segmented, Tag } from "../components/ui";

type Depth = "brief" | "natural" | "deep";
type FollowUp = "none" | "allowed";
type Initiative = "follow" | "extend";
type MemoryMode = "natural" | "explicit" | "off";

function valueOf(controls: ConversationControlsView | null, name: string, fallback: unknown): unknown {
  return controls?.controls[name]?.value ?? fallback;
}

export function ControlsView({
  controls,
  loading,
  error,
  onBack,
  onUpdate,
}: {
  controls: ConversationControlsView | null;
  loading: boolean;
  error: string | null;
  onBack: () => void;
  onUpdate: (request: ConversationControlUpdate) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [depth, setDepth] = useState<Depth>("natural");
  const [followUp, setFollowUp] = useState<FollowUp>("allowed");
  const [initiative, setInitiative] = useState<Initiative>("follow");
  const [memoryMode, setMemoryMode] = useState<MemoryMode>("natural");
  const [quiet, setQuiet] = useState(false);

  useEffect(() => {
    const replyDepth = String(valueOf(controls, "reply_depth", "balanced"));
    const follow = String(valueOf(controls, "follow_up", "one"));
    const topic = String(valueOf(controls, "topic_shift", "same_topic"));
    const surface = valueOf(controls, "state_surface", "implicit");
    setDepth(replyDepth === "brief" ? "brief" : replyDepth === "deep" ? "deep" : "natural");
    setFollowUp(follow === "none" ? "none" : "allowed");
    setInitiative(topic === "adjacent" || topic === "open" ? "extend" : "follow");
    const memoryValue = typeof surface === "object" && surface !== null
      ? String((surface as Record<string, unknown>)["memory"] ?? "implicit")
      : String(surface);
    setMemoryMode(memoryValue === "explicit" || memoryValue === "explicit_consent" ? "explicit" : memoryValue === "none" || memoryValue === "silent" ? "off" : "natural");
    setQuiet(Boolean(valueOf(controls, "response_mute", false)));
  }, [controls]);

  const update = async (name: string, request: ConversationControlUpdate, optimistic: () => void) => {
    optimistic();
    setBusy(name);
    try {
      await onUpdate({ ...request, scope: "session" });
    } finally {
      setBusy(null);
    }
  };

  const depthLabel = depth === "brief" ? "短一点" : depth === "deep" ? "认真聊聊" : "自然回复";
  const followUpLabel = followUp === "none" ? "不追问" : "可以问我";
  const initiativeLabel = initiative === "follow" ? "跟着我的话题" : "可以顺着延伸";
  const memoryLabel = memoryMode === "off" ? "这次不用过去的事" : memoryMode === "explicit" ? "可以明确提起" : "自然接上过去";

  return (
    <main className="page page--controls">
      <PageHeader
        eyebrow="这次聊天"
        title="这次想怎么聊"
        description="这些选择会直接写入当前会话的可打断性控制，不是只改界面显示。"
        actions={<button className="button button--secondary preference-back" type="button" aria-label="返回对话" onClick={onBack}><ArrowLeft size={15} /><span>返回对话</span></button>}
      />
      <section className="scope-strip preference-intro">
        <div><strong>仅用于这次聊天</strong><span>会由真实运行时在下一条消息中执行</span></div>
        <Tag tone={loading ? "neutral" : "mint"}>{loading ? "正在读取" : "即时生效"}</Tag>
      </section>
      {error && <div className="inline-error" role="alert">{error}</div>}

      <section className="control-group">
        <header><span className="control-icon control-icon--blue"><MessageCircle size={18} /></span><div><h2>回应节奏</h2><p>让 Sora 按你现在需要的方式回应。</p></div></header>
        <div className="control-row"><div><strong>回复多长</strong><small>由运行时的 reply_depth 控制</small></div><Segmented value={depth} options={[{ value: "brief", label: "短一点" }, { value: "natural", label: "自然" }, { value: "deep", label: "认真聊聊" }]} onChange={(value) => void update("depth", { control: "reply_depth", value: value === "natural" ? "balanced" : value }, () => setDepth(value))} label="回复长度" /></div>
        <div className="control-row"><div><strong>要不要问我问题</strong><small>“别追问”会把本轮问题预算设为 0</small></div><Segmented value={followUp} options={[{ value: "none", label: "别追问" }, { value: "allowed", label: "可以问" }]} onChange={(value) => void update("follow", { control: "follow_up", value: value === "none" ? "none" : "one" }, () => setFollowUp(value))} label="是否追问" /></div>
        <div className="control-row"><div><strong>话题怎么往下走</strong><small>决定是否允许从当前话题自然延伸</small></div><Segmented value={initiative} options={[{ value: "follow", label: "跟着我" }, { value: "extend", label: "可以延伸" }]} onChange={(value) => void update("topic", { control: "topic_shift", value: value === "follow" ? "same_topic" : "adjacent" }, () => setInitiative(value))} label="话题推进方式" /></div>
      </section>

      <section className="control-group">
        <header><span className="control-icon control-icon--violet"><Sparkles size={18} /></span><div><h2>提到过去的事</h2><p>决定这次聊天里，Sora 可以怎样使用经过治理的记忆。</p></div></header>
        <div className="control-row control-row--stacked"><div><strong>怎么接上过去</strong><small>自然接上不会主动宣称“我记得你”</small></div><Segmented value={memoryMode} options={[{ value: "natural", label: "自然接上" }, { value: "explicit", label: "可以明确提起" }, { value: "off", label: "这次不用" }]} onChange={(value) => void update("memory", { control: "state_surface", value: value === "natural" ? "implicit" : value === "explicit" ? { memory: "explicit" } : { memory: "none" } }, () => setMemoryMode(value))} label="过去内容的使用方式" /></div>
        {memoryMode === "explicit" && <div className="consent-note"><Sparkles size={16} /><span><strong>可以明确提起。</strong>高敏内容仍会受同意与披露规则限制。</span></div>}
      </section>

      <section className="control-group">
        <header><span className="control-icon control-icon--mint"><Clock3 size={18} /></span><div><h2>暂时安静一下</h2><p>需要一点空间时，可以暂停 Sora 的回应。</p></div></header>
        <div className="quick-control-grid">
          <button className={quiet ? "is-active" : ""} type="button" disabled={busy !== null} onClick={() => void update("mute", { control: "response_mute", value: !quiet }, () => setQuiet((current) => !current))}><VolumeX size={17} /><span><strong>{quiet ? "可以继续说话了" : "先别说话"}</strong><small>{quiet ? "恢复正常回应" : "直到我再次打开回应"}</small></span></button>
          <button type="button" disabled title="需要独立主动提醒计划接口"><BellOff size={17} /><span><strong>今晚不主动提醒</strong><small>主动提醒计划接口尚未接入</small></span></button>
        </div>
      </section>

      <div className="control-summary">
        <Check size={17} />
        <span><strong>这次聊天：</strong>{quiet ? "暂停回应" : `${depthLabel} · ${followUpLabel} · ${initiativeLabel} · ${memoryLabel}`}</span>
        <Tag tone={busy ? "neutral" : "mint"}>{busy ? "保存中" : "已生效"}</Tag>
        <button className="text-link" type="button" disabled title="持久默认值需要单独确认流程"><Save size={13} /> 设为我的默认方式</button>
      </div>
    </main>
  );
}
