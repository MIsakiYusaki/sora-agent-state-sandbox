import { ArrowLeft, BellOff, Check, Clock3, MessageCircle, Save, Sparkles, VolumeX } from "lucide-react";
import { useState } from "react";
import { PageHeader, Segmented, Tag } from "../components/ui";

export function ControlsView({ onBack }: { onBack: () => void }) {
  const [depth, setDepth] = useState<"brief" | "natural" | "deep">("natural");
  const [followUp, setFollowUp] = useState<"none" | "allowed">("none");
  const [initiative, setInitiative] = useState<"follow" | "extend">("extend");
  const [memoryMode, setMemoryMode] = useState<"natural" | "explicit" | "off">("natural");
  const [quiet, setQuiet] = useState(false);

  const depthLabel = depth === "brief" ? "短一点" : depth === "deep" ? "认真聊聊" : "自然回复";
  const followUpLabel = followUp === "none" ? "不追问" : "可以问我";
  const initiativeLabel = initiative === "follow" ? "跟着我的话题" : "可以顺着延伸";
  const memoryLabel = memoryMode === "off" ? "这次不用过去的事" : memoryMode === "explicit" ? "可以明确提起" : "自然接上过去";

  return (
    <main className="page page--controls">
      <PageHeader
        eyebrow="这次聊天"
        title="这次想怎么聊"
        description="按你此刻的需要调整，离开这段对话后会恢复默认方式。"
        actions={<button className="button button--secondary preference-back" type="button" aria-label="返回对话" onClick={onBack}><ArrowLeft size={15} /><span>返回对话</span></button>}
      />

      <section className="scope-strip preference-intro">
        <div><strong>仅用于这次聊天</strong><span>关闭当前对话后自动恢复你的默认方式</span></div>
        <Tag tone="mint">即时生效</Tag>
      </section>

      <section className="control-group">
        <header><span className="control-icon control-icon--blue"><MessageCircle size={18} /></span><div><h2>回应节奏</h2><p>让 Sora 按你现在需要的方式回应。</p></div></header>
        <div className="control-row"><div><strong>回复多长</strong><small>随时可以改，不会打断当前话题</small></div><Segmented value={depth} options={[{ value: "brief", label: "短一点" }, { value: "natural", label: "自然" }, { value: "deep", label: "认真聊聊" }]} onChange={setDepth} label="回复长度" /></div>
        <div className="control-row"><div><strong>要不要问我问题</strong><small>“别追问”时，Sora 仍会认真回应，只是不在结尾追加问题</small></div><Segmented value={followUp} options={[{ value: "none", label: "别追问" }, { value: "allowed", label: "可以问" }]} onChange={setFollowUp} label="是否追问" /></div>
        <div className="control-row"><div><strong>话题怎么往下走</strong><small>你可以自己带着聊，也可以让 Sora 适度延伸</small></div><Segmented value={initiative} options={[{ value: "follow", label: "跟着我" }, { value: "extend", label: "可以延伸" }]} onChange={setInitiative} label="话题推进方式" /></div>
      </section>

      <section className="control-group">
        <header><span className="control-icon control-icon--violet"><Sparkles size={18} /></span><div><h2>提到过去的事</h2><p>决定这次聊天里，Sora 可以怎样使用你们过去的对话。</p></div></header>
        <div className="control-row control-row--stacked"><div><strong>怎么接上过去</strong><small>“自然接上”只让对话更连贯，不会主动说“我记得你……”</small></div><Segmented value={memoryMode} options={[{ value: "natural", label: "自然接上" }, { value: "explicit", label: "可以明确提起" }, { value: "off", label: "这次不用" }]} onChange={setMemoryMode} label="过去内容的使用方式" /></div>
        {memoryMode === "explicit" && <div className="consent-note"><Sparkles size={16} /><span><strong>可以明确提起。</strong>遇到私密或让人不舒服的内容，Sora 仍会先问你。</span></div>}
      </section>

      <section className="control-group">
        <header><span className="control-icon control-icon--mint"><Clock3 size={18} /></span><div><h2>暂时安静一下</h2><p>需要一点空间时，不必进入复杂设置。</p></div></header>
        <div className="quick-control-grid">
          <button className={quiet ? "is-active" : ""} type="button" onClick={() => setQuiet((current) => !current)}><VolumeX size={17} /><span><strong>{quiet ? "可以继续说话了" : "先别说话"}</strong><small>{quiet ? "恢复正常回应" : "直到我再次开口"}</small></span></button>
          <button type="button"><BellOff size={17} /><span><strong>今晚不主动提醒</strong><small>明早自动恢复</small></span></button>
        </div>
      </section>

      <div className="control-summary">
        <Check size={17} />
        <span><strong>这次聊天：</strong>{depthLabel} · {followUpLabel} · {initiativeLabel} · {memoryLabel}</span>
        <Tag tone="mint">已生效</Tag>
        <button className="text-link" type="button"><Save size={13} /> 设为我的默认方式</button>
      </div>
    </main>
  );
}
