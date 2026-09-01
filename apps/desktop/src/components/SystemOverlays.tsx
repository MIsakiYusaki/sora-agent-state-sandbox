import {
  ArrowRight,
  Bot,
  Check,
  ChevronLeft,
  CircleAlert,
  Download,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  UploadCloud,
  X,
} from "lucide-react";
import { useState } from "react";
import type { PrototypeState } from "../data";
import { IconButton, StatusDot, Tag } from "./ui";

export function SystemOverlays({
  state,
  onState,
  onOpenSettings,
}: {
  state: PrototypeState;
  onState: (state: PrototypeState) => void;
  onOpenSettings: () => void;
}) {
  if (state === "first-run") return <FirstRun onFinish={() => onState("daily")} />;
  if (state === "version-conflict") return <VersionConflict onClose={() => onState("daily")} />;
  if (state === "update-ready") return <UpdateReady onClose={() => onState("daily")} />;
  if (state === "safe-mode") return <SafeMode onClose={() => onState("daily")} onSettings={onOpenSettings} />;
  return null;
}

function FirstRun({ onFinish }: { onFinish: () => void }) {
  const [step, setStep] = useState(0);
  const [checking, setChecking] = useState(false);
  const steps = ["欢迎", "连接模型", "数据边界", "完成"];
  const next = () => {
    if (step === 1 && !checking) {
      setChecking(true);
      window.setTimeout(() => { setChecking(false); setStep(2); }, 800);
      return;
    }
    if (step === steps.length - 1) onFinish();
    else setStep((current) => current + 1);
  };

  return (
    <div className="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
      <aside className="onboarding-aside">
        <div className="onboarding-brand"><span>✦</span><strong>Project Sora</strong></div>
        <div className="onboarding-presence"><img src="/sora-reference.png" alt="Sora" /><i /></div>
        <blockquote>“先把属于你的空间准备好。之后，我们再慢慢认识彼此。”</blockquote>
        <small>所有个人数据默认保存在这台设备。</small>
      </aside>
      <main className="onboarding-main">
        <ol className="onboarding-steps">
          {steps.map((label, index) => <li className={index === step ? "is-active" : index < step ? "is-complete" : ""} key={label}><i>{index < step ? <Check size={12} /> : index + 1}</i><span>{label}</span></li>)}
        </ol>
        <div className="onboarding-content">
          {step === 0 && <WelcomeStep />}
          {step === 1 && <ProviderStep checking={checking} />}
          {step === 2 && <PrivacyStep />}
          {step === 3 && <ReadyStep />}
        </div>
        <footer className="onboarding-actions">
          <button className="button button--secondary" type="button" disabled={step === 0 || checking} onClick={() => setStep((current) => Math.max(0, current - 1))}><ChevronLeft size={16} /> 上一步</button>
          <button className="button" type="button" disabled={checking} onClick={next}>{checking ? <LoaderCircle className="spin" size={16} /> : step === 3 ? <Sparkles size={16} /> : <ArrowRight size={16} />}{checking ? "验证连接" : step === 3 ? "开始对话" : "继续"}</button>
        </footer>
      </main>
    </div>
  );
}

function WelcomeStep() {
  return <section className="setup-step"><span className="setup-step__icon"><Sparkles size={24} /></span><h1 id="onboarding-title">欢迎来到 Sora。</h1><p>这是一个陪伴空间，也是一套由你掌控边界的本机系统。我们先用三步完成最必要的设置。</p><div className="setup-points"><div><ShieldCheck size={18} /><span><strong>本机优先</strong><small>对话、记忆和状态默认留在你的设备</small></span></div><div><KeyRound size={18} /><span><strong>自带模型密钥</strong><small>密钥由系统凭据库保存，不进入界面或日志</small></span></div><div><Bot size={18} /><span><strong>逐项可控</strong><small>记忆、主动性与显式引用都有独立开关</small></span></div></div></section>;
}

function ProviderStep({ checking }: { checking: boolean }) {
  return <section className="setup-step"><span className="setup-step__icon"><KeyRound size={24} /></span><h1 id="onboarding-title">连接你的模型服务</h1><p>检测到环境中已有 DeepSeek 凭据引用。我们只验证可用性，不会读取或显示密钥。</p><div className="detected-provider"><span className="provider-logo provider-logo--blue">D</span><span><strong>DeepSeek</strong><small>2 个可用模型 · 1 个凭据引用</small></span><Tag tone={checking ? "amber" : "mint"}>{checking ? "验证中" : "已检测"}</Tag></div><div className="setup-form-row"><label><span>快速响应</span><select defaultValue="flash"><option value="flash">DeepSeek V4 Flash</option><option value="chat">DeepSeek Chat</option></select></label><label><span>高质量回应</span><select defaultValue="pro"><option value="pro">DeepSeek V4 Pro</option><option value="chat">DeepSeek Chat</option></select></label></div><button className="text-link setup-link" type="button">改用其他供应商或自定义端点</button></section>;
}

function PrivacyStep() {
  return <section className="setup-step"><span className="setup-step__icon"><ShieldCheck size={24} /></span><h1 id="onboarding-title">确认数据边界</h1><p>下面是安全默认值。之后可以在“设置 → 数据与隐私”里逐项调整。</p><div className="privacy-options"><label><span><strong>默认自然接上过去</strong><small>让回应更连贯，但不会主动说“我记得你……”</small></span><input type="checkbox" defaultChecked /></label><label><span><strong>提到私密内容前先确认</strong><small>明确引用或导出前再次征求同意</small></span><input type="checkbox" defaultChecked /></label><label><span><strong>匿名改进诊断</strong><small>默认关闭；不会上传对话正文</small></span><input type="checkbox" /></label></div><div className="local-data-note"><ShieldCheck size={16} /><span>你的数据会保存在本机独立目录，可随时导出或完整删除。</span></div></section>;
}

function ReadyStep() {
  return <section className="setup-step"><span className="setup-step__icon setup-step__icon--ready"><Check size={25} /></span><h1 id="onboarding-title">你的空间准备好了。</h1><p>模型连接和安全默认值均已就绪。第一次对话不需要定义关系，也不用填写一长串偏好。</p><div className="ready-summary"><div><span>对话模型</span><strong>DeepSeek V4 Flash + Pro</strong><StatusDot tone="positive" /></div><div><span>数据位置</span><strong>保存在这台电脑</strong><StatusDot tone="positive" /></div><div><span>提到过去</span><strong>默认自然接上</strong><StatusDot tone="positive" /></div></div></section>;
}

function VersionConflict({ onClose }: { onClose: () => void }) {
  const [resolving, setResolving] = useState(false);
  return (
    <div className="modal-backdrop">
      <section className="modal modal--conflict" role="dialog" aria-modal="true">
        <header><div><span className="modal-icon modal-icon--warning"><TriangleAlert size={19} /></span><div><h2>发现较新的本机版本</h2><p>这个对话在另一个窗口中发生了更新</p></div></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header>
        <div className="conflict-versions"><section><span>当前窗口</span><strong>Revision 184</strong><small>22:17:48 · 有 1 条未发送草稿</small></section><section><span>本机最新</span><strong>Revision 186</strong><small>22:18:12 · 新增 2 条消息</small></section></div>
        <div className="conflict-note"><CircleAlert size={16} /><span>草稿不会丢失。重新载入后会恢复到输入框。</span></div>
        <footer><button className="button button--secondary" type="button" onClick={onClose}>暂不处理</button><button className="button" type="button" onClick={() => { setResolving(true); window.setTimeout(onClose, 700); }}>{resolving ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}重新载入最新版本</button></footer>
      </section>
    </div>
  );
}

function UpdateReady({ onClose }: { onClose: () => void }) {
  return (
    <aside className="update-toast" role="status">
      <span className="update-toast__icon"><Download size={18} /></span>
      <div><strong>Project Sora 0.3.0 已就绪</strong><p>安全更新和对话恢复改进将在重启后生效。</p><span><ShieldCheck size={13} /> 签名已验证 · 18.4 MB</span></div>
      <div className="update-toast__actions"><button className="text-link" type="button" onClick={onClose}>稍后</button><button className="button" type="button" onClick={onClose}>重启并更新</button></div>
      <IconButton label="关闭" onClick={onClose}><X size={15} /></IconButton>
    </aside>
  );
}

function SafeMode({ onClose, onSettings }: { onClose: () => void; onSettings: () => void }) {
  return (
    <div className="safe-mode-band" role="alert">
      <span><UploadCloud size={17} /></span>
      <div><strong>安全降级模式</strong><p>模型服务连续失败。历史与草稿可用；发送、抽取和状态更新已暂停，避免产生不完整数据。</p></div>
      <button className="button button--secondary" type="button" onClick={onSettings}>检查模型</button>
      <button className="button" type="button" onClick={onClose}>重试恢复</button>
      <IconButton label="关闭提示" onClick={onClose}><X size={16} /></IconButton>
    </div>
  );
}
