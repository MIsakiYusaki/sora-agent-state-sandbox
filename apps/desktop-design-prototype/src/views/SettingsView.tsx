import {
  Bell,
  Bot,
  Check,
  ChevronRight,
  Database,
  Download,
  KeyRound,
  Laptop,
  LoaderCircle,
  LockKeyhole,
  Palette,
  Plus,
  RotateCcw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  UserRound,
  Wrench,
  X,
} from "lucide-react";
import { useState } from "react";
import { providers } from "../data";
import { IconButton, PageHeader, Segmented, StatusDot, Switch, Tag } from "../components/ui";

type SettingsTab = "models" | "profile" | "appearance" | "notifications" | "data" | "advanced";

const tabs: ReadonlyArray<{ id: SettingsTab; label: string; icon: typeof Bot }> = [
  { id: "models", label: "模型与供应商", icon: Bot },
  { id: "profile", label: "Sora 与我", icon: UserRound },
  { id: "appearance", label: "外观", icon: Palette },
  { id: "notifications", label: "通知", icon: Bell },
  { id: "data", label: "数据与隐私", icon: Database },
  { id: "advanced", label: "高级", icon: Settings2 },
];

export function SettingsView({
  developerEnabled,
  onDeveloperEnabled,
  onOpenDeveloper,
}: {
  developerEnabled: boolean;
  onDeveloperEnabled: (enabled: boolean) => void;
  onOpenDeveloper: () => void;
}) {
  const [activeTab, setActiveTab] = useState<SettingsTab>("models");
  const [providerDialog, setProviderDialog] = useState(false);
  return (
    <main className="settings-page">
      <PageHeader eyebrow="Sora" title="设置" description="管理模型连接、个人数据和这台电脑上的体验。" />
      <div className="settings-shell">
        <nav className="settings-nav" aria-label="设置页面">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button key={id} className={activeTab === id ? "is-active" : ""} type="button" onClick={() => setActiveTab(id)}><Icon size={17} /><span>{label}</span><ChevronRight size={14} /></button>
          ))}
        </nav>
        <section className="settings-content">
          {activeTab === "models" && <ModelsSettings onAdd={() => setProviderDialog(true)} />}
          {activeTab === "profile" && <ProfileSettings />}
          {activeTab === "appearance" && <AppearanceSettings />}
          {activeTab === "notifications" && <NotificationSettings />}
          {activeTab === "data" && <DataSettings />}
          {activeTab === "advanced" && <AdvancedSettings enabled={developerEnabled} onEnabled={onDeveloperEnabled} onOpenDeveloper={onOpenDeveloper} />}
        </section>
      </div>
      {providerDialog && <ProviderDialog onClose={() => setProviderDialog(false)} />}
    </main>
  );
}

function SettingsHeading({ title, description, badge }: { title: string; description: string; badge?: string }) {
  return <div className="settings-heading"><div><h2>{title}</h2><p>{description}</p></div>{badge && <Tag tone="mint"><ShieldCheck size={12} />{badge}</Tag>}</div>;
}

function ModelsSettings({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="settings-section">
      <SettingsHeading title="模型与供应商" description="为理解、回应和记忆整理选择合适的模型。API 密钥只保存在系统凭据库。" badge="本机安全存储" />
      <div className="settings-callout"><KeyRound size={17} /><span><strong>已读取这台电脑上的 DeepSeek 凭据。</strong>密钥不会显示在界面、日志或导出内容里。</span><button type="button">检查连接</button></div>
      <div className="provider-table">
        <div className="provider-table__header"><span>连接</span><span>运行分工</span><span>延迟</span><span>状态</span><span /></div>
        {providers.map((provider, index) => (
          <div className="provider-table__row" key={`${provider.name}-${provider.model}`}>
            <span className={`provider-logo provider-logo--${provider.accent}`}>{provider.name.slice(0, 1)}</span>
            <span className="provider-name"><strong>{provider.model}</strong><small>{provider.name}</small></span>
            <span className="provider-workload">{provider.workloads}</span>
            <span className="provider-latency">{provider.latency}</span>
            <span className="provider-status"><StatusDot tone="positive" />{provider.status}</span>
            <IconButton label={index < 2 ? "轮换密钥" : "编辑连接"}>{index < 2 ? <RotateCcw size={16} /> : <Wrench size={16} />}</IconButton>
          </div>
        ))}
      </div>
      <button className="add-row" type="button" onClick={onAdd}><Plus size={17} /><span><strong>连接新的模型服务</strong><small>使用内置目录或自定义兼容端点</small></span></button>
      <div className="settings-subsection">
        <div className="subsection-heading"><div><h3>任务分工</h3><p>快速理解优先低延迟模型；复杂回应和记忆整理可以使用更强模型。</p></div><button className="text-link" type="button">查看全部 13 类任务</button></div>
        <div className="routing-grid"><label><span>快速理解</span><select defaultValue="flash"><option value="flash">DeepSeek V4 Flash</option><option value="pro">DeepSeek V4 Pro</option></select></label><label><span>语义判断</span><select defaultValue="flash"><option value="flash">DeepSeek V4 Flash</option><option value="pro">DeepSeek V4 Pro</option></select></label><label><span>生成回应</span><select defaultValue="pro"><option value="flash">DeepSeek V4 Flash</option><option value="pro">DeepSeek V4 Pro</option></select></label><label><span>整理记忆</span><select defaultValue="pro"><option value="flash">DeepSeek V4 Flash</option><option value="pro">DeepSeek V4 Pro</option></select></label></div>
      </div>
    </div>
  );
}

function ProfileSettings() {
  return <div className="settings-section"><SettingsHeading title="Sora 与我" description="管理称呼和这台电脑上的独立数据空间。" /><div className="profile-card"><span className="avatar avatar--large"><img src="/sora-reference.png" alt="Sora" /></span><div><strong>Misaki 与 Sora</strong><small>只在这台电脑上使用 · 创建于 2026-06-15</small></div><button className="button button--secondary" type="button">编辑称呼</button></div><div className="settings-list"><div><span><strong>数据位置</strong><small>C:\Users\…\Project Sora\profiles\main</small></span><button className="button button--secondary" type="button">打开位置</button></div><div><span><strong>创建另一个独立空间</strong><small>对话、记忆和设置不会与当前空间混在一起</small></span><button className="button button--secondary" type="button"><Plus size={15} /> 新建</button></div></div></div>;
}

function AppearanceSettings() {
  const [theme, setTheme] = useState<"system" | "light" | "dark">("system");
  const [presence, setPresence] = useState(true);
  return <div className="settings-section"><SettingsHeading title="外观" description="Sora 的形象不会挤占日常对话空间。" /><div className="setting-row setting-row--stack"><div><strong>主题</strong><small>遵循系统或固定使用浅色/深色</small></div><Segmented value={theme} options={[{ value: "system", label: "跟随系统" }, { value: "light", label: "浅色" }, { value: "dark", label: "深色" }]} onChange={setTheme} label="主题" /></div><div className="setting-row"><div><strong>Sora 形象</strong><small>只在空白页和窄窗陪伴视图显示角色</small></div><Switch checked={presence} onChange={setPresence} label="显示 Sora 形象" /></div><div className="appearance-preview"><span className="appearance-preview__canvas"><i /><b /><em /></span><span><strong>对话布局</strong><small>紧凑导航 · 居中正文 · 按需展开侧栏</small></span></div></div>;
}

function NotificationSettings() {
  const [reply, setReply] = useState(true); const [world, setWorld] = useState(false); const [quiet, setQuiet] = useState(true);
  return <div className="settings-section"><SettingsHeading title="通知" description="只有你允许的内容才会打扰你。" /><div className="settings-list"><div><span><strong>回应完成</strong><small>窗口不在前台时通知</small></span><Switch checked={reply} onChange={setReply} label="回应完成通知" /></div><div><span><strong>故事事件邀请</strong><small>只通知已经发布并且符合你许可的事件</small></span><Switch checked={world} onChange={setWorld} label="故事事件通知" /></div><div><span><strong>安静时段</strong><small>22:30–08:30 仅保留安全与更新通知</small></span><Switch checked={quiet} onChange={setQuiet} label="安静时段" /></div></div></div>;
}

function DataSettings() {
  return <div className="settings-section"><SettingsHeading title="数据与隐私" description="导出和删除都在本机完成，执行前会让你预览范围。" /><div className="settings-list"><div><span><strong>导出我的数据</strong><small>包括对话、记忆和陪伴状态</small></span><button className="button button--secondary" type="button"><Download size={15} /> 导出</button></div><div><span><strong>导出诊断信息</strong><small>默认移除私密原文、API 密钥和调试权限</small></span><button className="button button--secondary" type="button">预览并导出</button></div><div><span><strong>诊断记录保留时间</strong><small>默认保留 30 天，可以继续缩短</small></span><button className="text-link" type="button">管理</button></div><div className="setting-row--danger"><span><strong>删除当前 Sora 数据</strong><small>需要重新输入当前空间名称确认</small></span><button className="button button--danger-quiet" type="button"><Trash2 size={15} /> 删除</button></div></div></div>;
}

function AdvancedSettings({ enabled, onEnabled, onOpenDeveloper }: { enabled: boolean; onEnabled: (enabled: boolean) => void; onOpenDeveloper: () => void }) {
  return <div className="settings-section"><SettingsHeading title="高级" description="面向开发、测试和故障排查，普通使用不需要开启。" /><div className="developer-setting"><div className="developer-setting__icon"><Laptop size={21} /></div><div><strong>调试模式</strong><p>查看运行链路并执行受控命令。不会开放命令行、数据库查询或任意代码执行。</p><span>{enabled ? <><StatusDot tone="positive" /> 已启用</> : "默认关闭"}</span></div><Switch checked={enabled} onChange={onEnabled} label="调试模式" /></div>{enabled && <div className="settings-list"><div><span><strong>运行时检查器</strong><small>跟踪当前数据空间、对话和关联编号</small></span><button className="button" type="button" onClick={onOpenDeveloper}>打开</button></div><div><span><strong>调试权限</strong><small>15 分钟无活动自动撤销</small></span><Tag tone="mint"><LockKeyhole size={12} /> 范围已限定</Tag></div><div><span><strong>所有操作先预览</strong><small>中高风险管理动作不会直接执行</small></span><Switch checked={true} onChange={() => undefined} label="所有操作先预览" /></div></div>}</div>;
}

function ProviderDialog({ onClose }: { onClose: () => void }) {
  const [mode, setMode] = useState<"catalog" | "custom">("catalog"); const [verifying, setVerifying] = useState(false);
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="modal modal--provider" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="modal-icon"><Bot size={18} /></span><div><h2>连接模型服务</h2><p>密钥会通过 Windows 系统安全窗口录入</p></div></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header><Segmented value={mode} options={[{ value: "catalog", label: "内置目录" }, { value: "custom", label: "自定义服务" }]} onChange={setMode} label="连接方式" />{mode === "catalog" ? <div className="provider-catalog"><button className="catalog-item is-selected" type="button"><span className="provider-logo provider-logo--blue">D</span><span><strong>DeepSeek</strong><small>支持对话与关闭思考模式</small></span><Check size={16} /></button><label><span>模型</span><select defaultValue="flash"><option value="flash">DeepSeek V4 Flash</option><option value="pro">DeepSeek V4 Pro</option><option value="chat">DeepSeek Chat</option></select></label></div> : <div className="provider-form-grid"><label><span>连接名称</span><input defaultValue="我的模型服务" /></label><label><span>兼容协议</span><select><option>OpenAI Chat Completions</option><option>OpenAI Responses</option><option>Anthropic Messages</option><option>Gemini Native</option></select></label><label className="span-2"><span>服务地址</span><input defaultValue="https://api.example.com/v1" /></label><label><span>模型 ID</span><input placeholder="model-name" /></label><label><span>用途</span><select><option>对话</option><option>向量</option><option>重排</option></select></label></div>}<div className="native-credential-note"><KeyRound size={16} /><span>下一步会打开系统安全窗口。密钥不会进入界面、日志或诊断信息。</span></div><footer><button className="button button--secondary" type="button" onClick={onClose}>取消</button><button className="button" type="button" onClick={() => { setVerifying(true); window.setTimeout(onClose, 900); }}>{verifying ? <LoaderCircle size={16} className="spin" /> : <ChevronRight size={16} />}{verifying ? "验证中" : "继续"}</button></footer></section></div>;
}
