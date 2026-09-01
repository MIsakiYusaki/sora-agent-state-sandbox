import {
  ArrowLeft,
  Bell,
  Bot,
  Database,
  Palette,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import type { DesktopTransport } from "../transport";
import { ProviderSettings } from "./ProviderSettings";

const pendingSections = [
  { label: "Sora 与我", icon: UserRound },
  { label: "外观", icon: Palette },
  { label: "通知", icon: Bell },
  { label: "数据与隐私", icon: Database },
] as const;

export function SettingsPanel({
  transport,
  onBack,
  onProviderConfigurationChanged,
}: {
  readonly transport: DesktopTransport;
  readonly onBack: () => void;
  readonly onProviderConfigurationChanged: () => Promise<void>;
}) {
  return (
    <section className="settings-page page" aria-labelledby="settings-title">
      <header className="page-header">
        <button className="back-button" type="button" onClick={onBack}>
          <ArrowLeft size={17} aria-hidden="true" />
          返回对话
        </button>
        <div>
          <h1 id="settings-title">设置</h1>
          <p>管理这台电脑上的模型连接。其他设置会在功能准备完成后逐项开放。</p>
        </div>
        <span className="page-status"><ShieldCheck size={15} aria-hidden="true" /> 本机安全存储</span>
      </header>

      <div className="settings-shell">
        <nav className="settings-nav" aria-label="设置分类">
          <button className="settings-tab is-active" type="button" aria-current="page">
            <Bot size={17} aria-hidden="true" />
            <span>模型与连接</span>
          </button>
          <div className="settings-nav__pending" aria-label="尚未开放的设置">
            {pendingSections.map(({ label, icon: Icon }) => (
              <span key={label}>
                <Icon size={17} aria-hidden="true" />
                <span>{label}</span>
                <small>准备中</small>
              </span>
            ))}
          </div>
        </nav>
        <div className="settings-content">
          <ProviderSettings
            transport={transport}
            onConfigurationChanged={onProviderConfigurationChanged}
          />
        </div>
      </div>
    </section>
  );
}
