import {
  CircleAlert,
  History,
  LoaderCircle,
  Menu,
  PanelRight,
  SlidersHorizontal,
  WifiOff,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Composer, type ComposerMode } from "./components/Composer";
import { ContextDrawer } from "./components/ContextDrawer";
import { MessageList } from "./components/MessageList";
import { Rail } from "./components/Rail";
import { ThreadDrawer } from "./components/ThreadDrawer";
import type { SpaceId } from "./data";
import {
  conversationReducer,
  initialConversationState,
  type ConversationMessage,
} from "./conversation/state";
import type { DesktopStatus, GatewayEvent, HistoryItem, ShellError } from "./contracts/generated";
import {
  createDesktopTransport,
  type ConversationControlUpdate,
  type ConversationControlsView,
  type ConversationSummaryView,
  type DesktopTransport,
  type UserMemoryPage,
} from "./transport";
import { ControlsView } from "./views/ControlsView";
import { DeveloperView } from "./views/DeveloperView";
import { HistoryView } from "./views/HistoryView";
import { MemoryView } from "./views/MemoryView";
import { SettingsView } from "./views/SettingsView";

function initialTheme(): "light" | "dark" {
  const stored = window.localStorage.getItem("sora.theme");
  if (stored === "light" || stored === "dark") return stored;
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function App({ transport: injectedTransport }: { readonly transport?: DesktopTransport } = {}) {
  const transport = useMemo(() => injectedTransport ?? createDesktopTransport(), [injectedTransport]);
  const [state, dispatch] = useReducer(conversationReducer, initialConversationState);
  const [space, setSpace] = useState<SpaceId>("conversation");
  const [theme, setTheme] = useState<"light" | "dark">(initialTheme);
  const [threadDrawerOpen, setThreadDrawerOpen] = useState(() => window.innerWidth >= 760);
  const [contextDrawerOpen, setContextDrawerOpen] = useState(() => window.innerWidth >= 1280);
  const [developerEnabled, setDeveloperEnabledState] = useState(() => window.localStorage.getItem("sora.developer") === "true");
  const [bootAttempt, setBootAttempt] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [conversations, setConversations] = useState<ReadonlyArray<ConversationSummaryView>>([]);
  const [conversationIndexLoading, setConversationIndexLoading] = useState(true);
  const [historySelectedId, setHistorySelectedId] = useState<string | null>(null);
  const [historyPreview, setHistoryPreview] = useState<ReadonlyArray<HistoryItem>>([]);
  const [memoryPage, setMemoryPage] = useState<UserMemoryPage | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [controls, setControls] = useState<ConversationControlsView | null>(null);
  const [controlsLoading, setControlsLoading] = useState(false);
  const [controlsError, setControlsError] = useState<string | null>(null);
  const historyTriggerRef = useRef<HTMLButtonElement>(null);
  const contextTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem("sora.theme", theme);
  }, [theme]);

  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 760) {
        setThreadDrawerOpen(false);
        setContextDrawerOpen(false);
      } else if (window.innerWidth >= 1280) {
        setContextDrawerOpen(true);
      }
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  const refreshIndex = useCallback(async () => {
    setConversationIndexLoading(true);
    try {
      const index = await transport.listConversations();
      setConversations(index.items);
    } finally {
      setConversationIndexLoading(false);
    }
  }, [transport]);

  const refreshStatus = useCallback(async () => {
    const status = await transport.status();
    dispatch({ type: "status.refreshed", status });
  }, [transport]);

  useEffect(() => {
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    const boot = async () => {
      try {
        const [status, history] = await Promise.all([
          transport.status(),
          transport.loadHistory({
            schema_version: "DesktopHistoryRequest.v1",
            conversation_id: state.conversationId,
            limit: 50,
            before_sequence: null,
          }),
          refreshIndex(),
        ]);
        if (disposed) return;
        dispatch({ type: "boot.ready", status, history });
        unsubscribe = await transport.subscribe((envelope) => {
          if (envelope.kind === "gateway_event" && isGatewayEvent(envelope.payload)) {
            dispatch({ type: "gateway.event", event: envelope.payload });
            if (envelope.payload.event_type === "turn.committed") void refreshIndex();
          }
          if (envelope.kind === "shell_error") {
            dispatch({ type: "turn.failed", error: envelope.payload as unknown as ShellError });
          }
        });
      } catch (cause) {
        if (!disposed) dispatch({ type: "boot.failed", error: shellError(cause) });
      }
    };
    void boot();
    return () => {
      disposed = true;
      unsubscribe?.();
    };
  }, [bootAttempt, state.conversationId, transport, refreshIndex]);

  const retryBoot = () => {
    dispatch({ type: "boot.loading" });
    setBootAttempt((attempt) => attempt + 1);
  };

  const reconnect = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await refreshStatus();
    } catch (cause) {
      dispatch({ type: "turn.failed", error: shellError(cause) });
    } finally {
      setRefreshing(false);
    }
  };

  const submit = async () => {
    const text = state.draft.trim();
    const mode = composerMode(state.status, state.boot, state.pendingSubmission !== null, state.activeTurn !== null, refreshing, state.error);
    if (!text || mode !== "ready") return;
    const localMessageId = `local_${crypto.randomUUID()}`;
    const message: ConversationMessage = {
      id: localMessageId,
      role: "user",
      content: text,
      turnId: localMessageId,
      sequence: Number.MAX_SAFE_INTEGER,
      createdAtMs: Date.now(),
      delivery: "pending",
    };
    dispatch({ type: "turn.submitting", message });
    try {
      const accepted = await transport.submitTurn({
        schema_version: "DesktopSubmitTurnRequest.v1",
        conversation_id: state.conversationId,
        text,
        branch_id: "main",
        expected_conversation_version: state.conversationVersion,
      });
      dispatch({ type: "turn.accepted", accepted, localMessageId });
    } catch (cause) {
      dispatch({ type: "turn.failed", error: shellError(cause), turnId: localMessageId });
    }
  };

  const cancel = async () => {
    if (!state.activeTurn) return;
    try {
      await transport.cancelTurn({
        schema_version: "DesktopCancelTurnRequest.v1",
        conversation_id: state.conversationId,
        turn_id: state.activeTurn.turnId,
        reason: "user_cancelled",
      });
    } catch (cause) {
      dispatch({ type: "turn.failed", error: shellError(cause), turnId: state.activeTurn.turnId });
    }
  };

  const selectConversation = (conversationId: string) => {
    if (conversationId !== state.conversationId) {
      dispatch({ type: "conversation.selected", conversationId });
    }
    setSpace("conversation");
    if (window.innerWidth < 900) setThreadDrawerOpen(false);
  };

  const newConversation = () => {
    dispatch({ type: "conversation.selected", conversationId: `conversation_${crypto.randomUUID().replaceAll("-", "")}` });
    setSpace("conversation");
    setThreadDrawerOpen(false);
  };

  const openHistory = async () => {
    setSpace("history");
    setContextDrawerOpen(false);
    await refreshIndex().catch(() => undefined);
    const selectedId = conversations.find((item) => item.conversation_id === state.conversationId)?.conversation_id
      ?? conversations[0]?.conversation_id
      ?? null;
    if (selectedId) await selectHistoryPreview(selectedId);
  };

  const selectHistoryPreview = async (conversationId: string) => {
    setHistorySelectedId(conversationId);
    setHistoryPreview([]);
    try {
      const page = await transport.loadHistory({
        schema_version: "DesktopHistoryRequest.v1",
        conversation_id: conversationId,
        limit: 50,
        before_sequence: null,
      });
      setHistoryPreview(page.items);
    } catch {
      setHistoryPreview([]);
    }
  };

  const navigate = (next: SpaceId) => {
    if (next === "developer" && !developerEnabled) {
      setSpace("settings");
      return;
    }
    setSpace(next);
    if (next === "history") void openHistory();
    if (next !== "conversation") {
      setThreadDrawerOpen(false);
      setContextDrawerOpen(false);
    }
  };

  useEffect(() => {
    if (space !== "memory") return;
    setMemoryLoading(true);
    setMemoryError(null);
    void transport.listMemories()
      .then(setMemoryPage)
      .catch((cause) => setMemoryError(errorMessage(cause)))
      .finally(() => setMemoryLoading(false));
  }, [space, transport]);

  useEffect(() => {
    if (space !== "controls") return;
    setControlsLoading(true);
    setControlsError(null);
    void transport.getConversationControls(state.conversationId)
      .then(setControls)
      .catch((cause) => setControlsError(errorMessage(cause)))
      .finally(() => setControlsLoading(false));
  }, [space, state.conversationId, transport]);

  const setDeveloperEnabled = (enabled: boolean) => {
    setDeveloperEnabledState(enabled);
    window.localStorage.setItem("sora.developer", String(enabled));
    if (!enabled && space === "developer") setSpace("settings");
  };

  const mode = composerMode(state.status, state.boot, state.pendingSubmission !== null, state.activeTurn !== null, refreshing, state.error);
  const phase = phaseView(state.status, state.boot);
  const firstRunProviderSetup = state.status?.phase === "provider_setup_required"
    && state.messages.length === 0
    && conversations.length === 0;

  return (
    <div className={`app-root theme-${theme} ${space === "developer" ? "is-developer" : ""}`}>
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <Rail active={space} theme={theme} developerEnabled={developerEnabled} onNavigate={navigate} onTheme={() => setTheme((current) => current === "light" ? "dark" : "light")} />
      <div className="app-surface" id="main-content" tabIndex={-1}>
        {space === "conversation" && (
          <ThreadDrawer
            open={threadDrawerOpen}
            conversations={conversations}
            activeConversationId={state.conversationId}
            loading={conversationIndexLoading}
            modal={window.innerWidth < 900}
            triggerRef={historyTriggerRef}
            onClose={() => setThreadDrawerOpen(false)}
            onNew={newConversation}
            onSelect={selectConversation}
          />
        )}
        <div className="app-view">
          {space === "conversation" && (
            <section className={`conversation-workspace ${contextDrawerOpen ? "has-context" : ""}`} aria-label="Sora 对话">
              <section className="conversation-main">
                <header className="conversation-topbar">
                  <div className="conversation-topbar__start">
                    <button className="icon-button" type="button" aria-label={threadDrawerOpen ? "收起对话列表" : "展开对话列表"} aria-expanded={threadDrawerOpen} ref={historyTriggerRef} onClick={() => setThreadDrawerOpen((open) => !open)}><Menu size={18} /></button>
                    <span className="avatar"><img src="/sora-reference.png" alt="Sora" /></span>
                    <div className="conversation-identity"><strong>Sora</strong><span className={`presence presence--${phase.tone}`}><i />{phase.label}</span></div>
                  </div>
                  <div className="conversation-topbar__actions">
                    <button className="icon-button" type="button" aria-label="历史与分支" title="历史与分支" onClick={() => void openHistory()}><History size={18} /></button>
                    <button className="icon-button" type="button" aria-label="本轮对话控制" title="本轮对话控制" onClick={() => navigate("controls")}><SlidersHorizontal size={18} /></button>
                    <button className={contextDrawerOpen ? "icon-button is-active" : "icon-button"} type="button" aria-label={contextDrawerOpen ? "关闭聊天信息" : "查看这次聊天"} aria-expanded={contextDrawerOpen} ref={contextTriggerRef} onClick={() => setContextDrawerOpen((open) => !open)}><PanelRight size={18} /></button>
                  </div>
                </header>
                <ConnectionBand boot={state.boot} status={state.status} error={state.error} refreshing={refreshing} onProvider={() => navigate("settings")} />
                {state.boot === "loading" && state.messages.length === 0 ? <div className="boot-state" role="status"><LoaderCircle size={19} className="spin" /><span>正在连接 Sora</span></div> : <MessageList messages={state.messages} activeTurn={state.activeTurn} emptyMode={firstRunProviderSetup ? "provider_setup" : "conversation"} />}
                {state.error && state.boot !== "failed" && <ConversationErrorNotice error={state.error} onReload={retryBoot} onDismiss={() => dispatch({ type: "error.dismissed" })} />}
                <footer className="composer-zone"><Composer value={state.draft} mode={mode} onChange={(value) => dispatch({ type: "draft.changed", value })} onSubmit={() => void submit()} onCancel={() => void cancel()} onReconnect={state.boot === "failed" ? retryBoot : () => void reconnect()} onConnectProvider={() => navigate("settings")} /><span className="composer-note">Sora 可能会犯错。重要信息请核实。</span></footer>
              </section>
              <ContextDrawer open={contextDrawerOpen} modal={window.innerWidth < 1280} onClose={() => setContextDrawerOpen(false)} triggerRef={contextTriggerRef} />
            </section>
          )}
          {space === "history" && <HistoryView conversations={conversations} selectedId={historySelectedId} preview={historyPreview} loading={conversationIndexLoading} onSelect={(id) => void selectHistoryPreview(id)} onContinue={selectConversation} onNew={newConversation} />}
          {space === "memory" && <MemoryView page={memoryPage} loading={memoryLoading} error={memoryError} onVisibility={async (memoryId, visible) => { const updated = await transport.setMemoryVisibility(memoryId, visible); setMemoryPage((current) => current ? { ...current, items: current.items.map((item) => item.id === updated.id ? updated : item), summary: { ...current.summary, visible: current.summary.visible + (visible ? 1 : -1), hidden: current.summary.hidden + (visible ? -1 : 1) } } : current); }} onForget={async (memoryId) => { await transport.forgetMemory(memoryId); const next = await transport.listMemories(); setMemoryPage(next); }} />}
          {space === "controls" && <ControlsView controls={controls} loading={controlsLoading} error={controlsError} onBack={() => setSpace("conversation")} onUpdate={async (request: ConversationControlUpdate) => { try { const next = await transport.updateConversationControl(state.conversationId, request); setControls(next); setControlsError(null); } catch (cause) { setControlsError(errorMessage(cause)); throw cause; } }} />}
          {space === "settings" && <SettingsView transport={transport} profileId={state.status?.profile_id ?? null} theme={theme} developerEnabled={developerEnabled} onTheme={setTheme} onDeveloperEnabled={setDeveloperEnabled} onOpenDeveloper={() => setSpace("developer")} onProviderConfigurationChanged={refreshStatus} />}
          {space === "developer" && <DeveloperView profileId={state.status?.profile_id ?? null} conversationId={state.conversationId} onExit={() => setSpace("conversation")} />}
        </div>
      </div>
    </div>
  );
}

function ConnectionBand({ boot, status, error, refreshing, onProvider }: { boot: "loading" | "ready" | "failed"; status: DesktopStatus | null; error: ShellError | null; refreshing: boolean; onProvider: () => void }) {
  if (boot === "failed" && error?.code === "PROTOCOL_MISMATCH") return <div className="connection-band connection-band--danger" role="alert"><CircleAlert size={16} /><span>应用与对话服务版本不兼容。新对话已暂停，请安装匹配组件或回滚到兼容版本；草稿仍保留。</span></div>;
  if (boot === "failed" && error?.retryable === false) return <div className="connection-band connection-band--danger" role="alert"><CircleAlert size={16} /><span>对话服务需要修复后才能继续。历史与草稿仍保留，当前不会反复重试。</span></div>;
  if (boot === "failed") return <div className="connection-band connection-band--danger" role="alert"><WifiOff size={16} /><span>暂时无法载入对话。草稿仍保留在输入框中。</span></div>;
  if (boot !== "ready" || !status) return null;
  if (status.phase === "provider_setup_required" || (status.gateway_ready && !status.provider_ready)) return <div className="connection-band connection-band--warning" role="status"><CircleAlert size={16} /><span>{status.status_message || "尚未连接可用于文字对话的模型。"}</span><button type="button" onClick={onProvider}>打开模型设置</button></div>;
  if (!status.gateway_ready || ["starting", "degraded", "restarting", "stopped"].includes(status.phase)) return <div className="connection-band connection-band--danger" role="status">{refreshing ? <LoaderCircle size={16} className="spin" /> : <WifiOff size={16} />}<span>{status.status_message || (status.recoverable === false ? "当前处于只读安全降级状态。历史与草稿可查看，发送和状态写入已暂停。" : "对话服务暂时不可用。历史和草稿仍可查看。")}</span></div>;
  return null;
}

function ConversationErrorNotice({ error, onReload, onDismiss }: { error: ShellError; onReload: () => void; onDismiss: () => void }) {
  if (error.code === "TURN_CONFLICT") {
    const currentVersion = error.safe_details?.["current_version"];
    const expectedVersion = error.safe_details?.["expected_version"];
    const versionDetail = typeof currentVersion === "number" && typeof expectedVersion === "number"
      ? `当前窗口版本 ${expectedVersion}，本机最新版本 ${currentVersion}。`
      : "这个对话已在另一个窗口或实例中更新。";
    return <div className="error-banner error-banner--blocking" role="alert"><CircleAlert size={17} /><span><strong>对话版本有更新。</strong>{versionDetail} 草稿仍保留，重新载入不会静默覆盖。</span><button className="button button--secondary" type="button" onClick={onReload}>重新载入最新内容</button></div>;
  }
  if (error.code === "STORAGE_UNAVAILABLE") {
    return <div className="error-banner error-banner--blocking" role="alert"><CircleAlert size={17} /><span><strong>本机数据暂不可写。</strong>历史仍可查看；发送、记忆和设置写入已经暂停，避免错误地宣称内容已保存。</span>{error.retryable && <button className="button button--secondary" type="button" onClick={onReload}>重新检查</button>}</div>;
  }
  return <div className="error-banner" role="alert"><CircleAlert size={17} /><span>{error.message}</span><button className="icon-button" type="button" aria-label="关闭错误提示" onClick={onDismiss}><X size={16} /></button></div>;
}

function composerMode(status: DesktopStatus | null, boot: "loading" | "ready" | "failed", pending: boolean, active: boolean, refreshing: boolean, error: ShellError | null = null): ComposerMode {
  if (pending) return "submitting";
  if (active) return "streaming";
  if (error?.code === "PROVIDER_REQUIRED" || error?.code === "PROVIDER_UNAVAILABLE") return "provider_missing";
  if (error?.code === "TURN_CONFLICT" || error?.code === "STORAGE_UNAVAILABLE" || error?.code === "PROTOCOL_MISMATCH") return "repair_required";
  if (boot === "failed" && error?.retryable === false) return "repair_required";
  if (boot !== "ready" || !status || refreshing) return "offline";
  if (status.phase === "provider_setup_required" || (status.gateway_ready && !status.provider_ready)) return "provider_missing";
  if (status.recoverable === false) return "repair_required";
  if (!status.gateway_ready || ["starting", "degraded", "restarting", "stopped"].includes(status.phase)) return "offline";
  return "ready";
}

function phaseView(status: DesktopStatus | null, boot: "loading" | "ready" | "failed") {
  if (boot === "failed") return { label: "连接失败", tone: "danger" } as const;
  if (boot === "loading" || !status) return { label: "正在连接", tone: "neutral" } as const;
  if (status.phase === "ready") return { label: "在你身边", tone: "positive" } as const;
  if (status.phase === "provider_setup_required") return { label: "需要连接模型", tone: "warning" } as const;
  if (status.phase === "restarting" || status.phase === "starting") return { label: "正在恢复", tone: "neutral" } as const;
  return { label: status.phase === "stopped" ? "已离线" : "连接受限", tone: "danger" } as const;
}

function isGatewayEvent(payload: unknown): payload is GatewayEvent {
  if (typeof payload !== "object" || payload === null) return false;
  const record = payload as Readonly<Record<string, unknown>>;
  return record["schema_version"] === "GatewayEvent.v1" && typeof record["turn_id"] === "string" && typeof record["sequence"] === "number" && typeof record["event_type"] === "string";
}

function shellError(cause: unknown): ShellError {
  if (typeof cause === "object" && cause !== null && "schema_version" in cause) return cause as ShellError;
  return { schema_version: "ShellError.v1", code: "INTERNAL_ERROR", message: errorMessage(cause), retryable: true, retry_after_ms: null, correlation_id: null, action: "retry", safe_details: {} };
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "操作失败，请稍后重试";
}
