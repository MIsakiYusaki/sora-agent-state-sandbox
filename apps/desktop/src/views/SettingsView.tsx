import { Bell, Bot, ChevronRight, Database, Laptop, LockKeyhole, Palette, Settings2, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { ProviderSettings } from "../components/ProviderSettings";
import { SpaceSettings } from "./SpaceSettings";
import { CompanionSettings } from "./CompanionSettings";
import { NotificationSettings } from "./NotificationSettings";
import { PrivacySettings } from "./PrivacySettings";
import { PageHeader, Segmented, StatusDot, Switch, Tag } from "../components/ui";
import type { DesktopTransport } from "../transport";

type SettingsTab = "models" | "profile" | "appearance" | "notifications" | "data" | "advanced";
const tabs: ReadonlyArray<{ id: SettingsTab; label: string; icon: typeof Bot }> = [
  { id: "models", label: "模型与供应商", icon: Bot },
  { id: "profile", label: "Sora 与我", icon: UserRound },
  { id: "appearance", label: "外观", icon: Palette },
  { id: "notifications", label: "通知", icon: Bell },
  { id: "data", label: "数据与隐私", icon: Database },
  { id: "advanced", label: "高级", icon: Settings2 },
];

function Heading({ title, description, badge }: { title: string; description: string; badge?: string }) {
  return <div className="settings-heading"><div><h2>{title}</h2><p>{description}</p></div>{badge && <Tag tone="mint"><ShieldCheck size={12} />{badge}</Tag>}</div>;
}

export function SettingsView({
  transport,
  profileId,
  theme,
  developerEnabled,
  onTheme,
  onDeveloperEnabled,
  onOpenDeveloper,
  onProviderConfigurationChanged,
  initialTab = "models",
  switchingAllowed = true,
}: {
  transport: DesktopTransport;
  profileId: string | null;
  theme: "light" | "dark";
  developerEnabled: boolean;
  onTheme: (theme: "light" | "dark") => void;
  onDeveloperEnabled: (enabled: boolean) => void;
  onOpenDeveloper: () => void;
  onProviderConfigurationChanged: () => Promise<void>;
  initialTab?: SettingsTab;
  switchingAllowed?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<SettingsTab>(initialTab);
  useEffect(() => setActiveTab(initialTab), [initialTab]);
  return (
    <main className="settings-page">
      <PageHeader eyebrow="Sora" title="设置" description="管理模型连接、个人数据和这台电脑上的体验。" />
      <div className="settings-shell">
        <nav className="settings-nav" aria-label="设置页面">
          {tabs.map(({ id, label, icon: Icon }) => <button key={id} className={activeTab === id ? "is-active" : ""} type="button" onClick={() => setActiveTab(id)}><Icon size={17} /><span>{label}</span><ChevronRight size={14} /></button>)}
        </nav>
        <section className="settings-content">
          {activeTab === "models" && <ProviderSettings transport={transport} onConfigurationChanged={onProviderConfigurationChanged} />}
          {activeTab === "profile" && <><CompanionSettings key={profileId} transport={transport} /><SpaceSettings transport={transport} profileId={profileId} switchingAllowed={switchingAllowed} /></>}
          {activeTab === "appearance" && (
            <div className="settings-section">
              <Heading title="外观" description="保持原型的 Quiet Companion Desk 布局。" />
              <div className="setting-row setting-row--stack"><div><strong>主题</strong><small>立即应用并保存在这台电脑的客户端设置中</small></div><Segmented value={theme} options={[{ value: "light", label: "浅色" }, { value: "dark", label: "深色" }]} onChange={onTheme} label="主题" /></div>
              <div className="appearance-preview"><span className="appearance-preview__canvas"><i /><b /><em /></span><span><strong>对话布局</strong><small>56px 导航 · 居中正文 · 按需展开侧栏</small></span></div>
            </div>
          )}
          {activeTab === "notifications" && <NotificationSettings transport={transport} profileId={profileId} />}
          {activeTab === "data" && <PrivacySettings transport={transport} profileId={profileId} />}
          {activeTab === "advanced" && (
            <div className="settings-section">
              <Heading title="高级" description="面向开发、测试和故障排查，普通使用不需要开启。" />
              <div className="developer-setting"><div className="developer-setting__icon"><Laptop size={21} /></div><div><strong>调试模式</strong><p>查看真实运行快照和当前会话事件；逐轮上下文帧与受控命令会按桥接状态明确标注。</p><span>{developerEnabled ? <><StatusDot tone="positive" /> 已启用</> : "默认关闭"}</span></div><Switch checked={developerEnabled} onChange={onDeveloperEnabled} label="调试模式" /></div>
              {developerEnabled && <div className="settings-list"><div><span><strong>运行时检查器</strong><small>只显示当前配置、对话与轮次的真实健康快照和事件</small></span><button className="button" type="button" onClick={onOpenDeveloper}>打开</button></div><div><span><strong>调试权限</strong><small>任意 Shell、SQL、Python、文件系统和原始密钥始终禁止</small></span><Tag tone="mint"><LockKeyhole size={12} /> 范围已限定</Tag></div></div>}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function UnavailableSettings({ title, description }: { title: string; description: string }) {
  return <div className="settings-section"><Heading title={title} description={description} /><div className="settings-callout"><ShieldCheck size={17} /><span><strong>当前安装包不会伪装这些操作已经完成。</strong>相关控件会在真实宿主契约接入后开放。</span></div></div>;
}
