import { PanelLeftOpen } from "lucide-react";
import { useEffect, useState } from "react";
import type { PrototypeState, SpaceId } from "./data";
import { Rail } from "./components/Rail";
import { SystemOverlays } from "./components/SystemOverlays";
import { ThreadDrawer } from "./components/ThreadDrawer";
import { ConversationView } from "./views/ConversationView";
import { ControlsView } from "./views/ControlsView";
import { DeveloperView } from "./views/DeveloperView";
import { HistoryView } from "./views/HistoryView";
import { MemoryView } from "./views/MemoryView";
import { SettingsView } from "./views/SettingsView";

const scenarioLabels: ReadonlyArray<{ id: PrototypeState; label: string; description: string }> = [
  { id: "daily", label: "日常对话", description: "正常连接和完整上下文" },
  { id: "first-run", label: "首次启动", description: "模型、隐私与 Profile 设置" },
  { id: "empty", label: "空对话", description: "第一次打开 Conversation Canvas" },
  { id: "no-provider", label: "无模型", description: "没有可用的文字模型路由" },
  { id: "offline", label: "Gateway 离线", description: "草稿保留、历史可读" },
  { id: "recovering", label: "连接恢复中", description: "后台重连和版本校验" },
  { id: "version-conflict", label: "版本冲突", description: "多窗口 revision 冲突" },
  { id: "update-ready", label: "更新就绪", description: "签名验证后的应用更新" },
  { id: "safe-mode", label: "安全降级", description: "模型失败时冻结写入" },
];

function getInitialTheme(): "light" | "dark" {
  const requested = new URLSearchParams(window.location.search).get("theme");
  if (requested === "dark" || requested === "light") return requested;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function getInitialSpace(): SpaceId {
  const requested = new URLSearchParams(window.location.search).get("space") as SpaceId | null;
  return requested && ["conversation", "history", "memory", "controls", "settings", "developer"].includes(requested) ? requested : "conversation";
}

function getInitialState(): PrototypeState {
  const requested = new URLSearchParams(window.location.search).get("state") as PrototypeState | null;
  return requested && scenarioLabels.some((scenario) => scenario.id === requested) ? requested : "daily";
}

export function App() {
  const [space, setSpace] = useState<SpaceId>(getInitialSpace);
  const [theme, setTheme] = useState<"light" | "dark">(getInitialTheme);
  const [prototypeState, setPrototypeState] = useState<PrototypeState>(getInitialState);
  const [threadDrawerOpen, setThreadDrawerOpen] = useState(window.innerWidth >= 760);
  const [contextOpen, setContextOpen] = useState(window.innerWidth >= 1280);
  const [developerEnabled, setDeveloperEnabled] = useState(() => getInitialSpace() === "developer");

  useEffect(() => {
    document.documentElement.style.colorScheme = theme;
    document.documentElement.dataset.theme = theme;
    const parameters = new URLSearchParams(window.location.search);
    parameters.set("space", space);
    parameters.set("theme", theme);
    parameters.set("state", prototypeState);
    window.history.replaceState(null, "", `${window.location.pathname}?${parameters.toString()}`);
  }, [space, theme, prototypeState]);

  const navigate = (next: SpaceId) => {
    if (next === "developer" && !developerEnabled) {
      setSpace("settings");
      return;
    }
    setSpace(next);
  };

  const reconnect = () => {
    setPrototypeState("recovering");
    window.setTimeout(() => setPrototypeState("daily"), 1300);
  };

  return (
    <div className={`app-root theme-${theme} ${space === "developer" ? "is-developer" : ""}`}>
      <Rail active={space} theme={theme} developerEnabled={developerEnabled} onNavigate={navigate} onTheme={() => setTheme((current) => current === "light" ? "dark" : "light")} />
      <div className="app-surface">
        {space === "conversation" && <ThreadDrawer open={threadDrawerOpen} onClose={() => setThreadDrawerOpen(false)} />}
        <div className="app-view">
          {space === "conversation" && (
            <ConversationView
              prototypeState={prototypeState}
              threadDrawerOpen={threadDrawerOpen}
              contextOpen={contextOpen}
              onThreadDrawer={() => setThreadDrawerOpen((current) => !current)}
              onContext={() => setContextOpen((current) => !current)}
              onOpenControls={() => setSpace("controls")}
              onOpenHistory={() => setSpace("history")}
              onReconnect={reconnect}
            />
          )}
          {space === "history" && <HistoryView />}
          {space === "memory" && <MemoryView />}
          {space === "controls" && <ControlsView onBack={() => setSpace("conversation")} />}
          {space === "settings" && <SettingsView developerEnabled={developerEnabled} onDeveloperEnabled={setDeveloperEnabled} onOpenDeveloper={() => setSpace("developer")} />}
          {space === "developer" && <DeveloperView onExit={() => setSpace("conversation")} />}
        </div>
      </div>

      {space === "conversation" && !threadDrawerOpen && (
        <button className="drawer-reopen" type="button" aria-label="展开对话列表" title="展开对话列表" onClick={() => setThreadDrawerOpen(true)}><PanelLeftOpen size={17} /></button>
      )}

      <SystemOverlays state={prototypeState} onState={setPrototypeState} onOpenSettings={() => setSpace("settings")} />
    </div>
  );
}
