import { useEffect, useRef, useState } from "react";
import { LockKeyhole, RefreshCw, ShieldCheck } from "lucide-react";
import type { DesktopAdminActionView, DesktopAdminCommandSpec, DesktopAdminRegistry, DesktopDebugSession } from "../contracts/generated";
import type { DesktopTransport } from "../transport";

const statusText: Record<string, string> = {
  previewed: "已预览，尚未执行", executing: "执行已受理，结果待核对", executed: "已执行并审计",
  execution_unknown: "执行结果待核对，禁止自动重试", expired: "预览已过期", dry_run_blocked: "全局 dry-run 已阻止写入",
};

export function AdminCommandPanel({ transport, initialCommandId = "", initialParams }: { readonly transport: DesktopTransport; readonly initialCommandId?: string; readonly initialParams?: Readonly<Record<string, unknown>> }) {
  const [session, setSession] = useState<DesktopDebugSession | null>(null);
  const [registry, setRegistry] = useState<DesktopAdminRegistry | null>(null);
  const [selectedId, setSelectedId] = useState(initialCommandId);
  const [params, setParams] = useState("{}");
  const [preview, setPreview] = useState<DesktopAdminActionView | null>(null);
  const [history, setHistory] = useState<ReadonlyArray<DesktopAdminActionView> | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState<"session" | "preview" | "execute" | "receipt" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const sessionRef = useRef<DesktopDebugSession | null>(null);
  const requestKey = useRef<string | null>(null);
  const selected = registry?.commands.find((command) => command.command_id === selectedId) ?? null;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (sessionRef.current) void transport.closeDebugSession(sessionRef.current.session_id).catch(() => undefined);
    };
  }, [transport]);

  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => {
      if (sessionRef.current?.session_id !== session.session_id) return;
      sessionRef.current = null;
      setSession(null);
      setConfirmed(false);
      setError("调试会话已过期。重新开启后核对回执，命令不会自动重发。");
      void transport.closeDebugSession(session.session_id).catch(() => undefined);
    }, Math.max(0, session.expires_at_ms - Date.now()));
    return () => window.clearTimeout(timer);
  }, [session, transport]);

  const remember = (action: DesktopAdminActionView) => {
    setPreview(action);
    setSelectedId(action.command_id);
    setParams(JSON.stringify(action.params, null, 2));
    requestKey.current = action.idempotency_key;
    setHistory((current) => [action, ...(current ?? []).filter((item) => item.action_id !== action.action_id)].slice(0, 20));
  };

  const choose = (command: DesktopAdminCommandSpec) => {
    setSelectedId(command.command_id);
    const suggested = command.command_id === initialCommandId && initialParams ? initialParams : command.default_params;
    setParams(suggested === null ? "" : JSON.stringify(suggested, null, 2));
    setPreview(null);
    setConfirmed(false);
    requestKey.current = null;
    setError(null);
  };

  const open = async () => {
    if (pending) return;
    setPending("session");
    setError(null);
    let opened: DesktopDebugSession | null = null;
    try {
      opened = await transport.openDebugSession();
      if (!mounted.current) {
        await transport.closeDebugSession(opened.session_id);
        return;
      }
      sessionRef.current = opened;
      setSession(opened);
      const commands = await transport.getAdminCommands();
      if (!mounted.current) return;
      setRegistry(commands);
      setHistory(commands.history ?? null);
      const first = commands.commands.find(command => command.command_id === selectedId) ?? commands.commands.find(command => command.available) ?? commands.commands[0];
      if (first) choose(first);
    } catch {
      if (opened) void transport.closeDebugSession(opened.session_id).catch(() => undefined);
      sessionRef.current = null;
      if (mounted.current) {
        setSession(null);
        setError("维护会话未开启。请检查真实 Host 连接后重新开启，不会执行任何命令。");
      }
    } finally { if (mounted.current) setPending(null); }
  };

  const close = async () => {
    const current = sessionRef.current;
    sessionRef.current = null;
    setSession(null);
    setConfirmed(false);
    if (current) await transport.closeDebugSession(current.session_id).catch(() => undefined);
  };

  const makePreview = async () => {
    if (pending || !session || !selected?.available) return;
    let values: unknown;
    try {
      values = JSON.parse(params);
      if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error();
    } catch { setError("参数必须是 JSON 对象，请按当前参数模式填写。"); return; }
    // A known preview is refreshed only by this explicit click. A lost preview
    // response reuses its key instead of creating duplicate records.
    if (!requestKey.current || preview !== null) requestKey.current = crypto.randomUUID();
    setPending("preview");
    setError(null);
    setConfirmed(false);
    try {
      const action = await transport.previewAdminCommand({
        schema_version: "DesktopAdminPreviewRequest.v1", command_id: selected.command_id,
        params: values as Record<string, unknown>, idempotency_key: requestKey.current,
      });
      if (mounted.current) remember(action);
    } catch { if (mounted.current) setError("预览未完成。核对参数模式和会话状态后再预览；没有自动执行。"); }
    finally { if (mounted.current) setPending(null); }
  };

  const execute = async () => {
    if (pending || !session || !confirmed || !preview?.owned_by_current_client || preview.execution_status !== "previewed") return;
    setPending("execute");
    setError(null);
    setConfirmed(false);
    try {
      const action = await transport.executeAdminCommand({
        schema_version: "DesktopAdminExecuteRequest.v1", action_id: preview.action_id,
        idempotency_key: preview.idempotency_key, expected_revision: preview.expected_revision, confirmed: true,
      });
      if (mounted.current) remember(action);
    } catch (cause) {
      if (mounted.current) {
        const failure = typeof cause === "object" && cause !== null ? cause as { code?: unknown; safe_details?: { reason?: unknown } } : null;
        if (failure?.code === "INVALID_REQUEST") {
          setPreview(null);
          requestKey.current = null;
          if (failure.safe_details?.reason === "debug_session_expired") {
            sessionRef.current = null;
            setSession(null);
            setError("调试会话已失效。重新开启并核对回执，旧请求不会自动重发。");
          } else setError("执行未被接受。请重新预览当前影响后确认，没有自动重试。");
        } else setError("执行请求没有获得完整回执。请读取审计状态核对，勿重复执行或自动重发。");
      }
    }
    finally { if (mounted.current) setPending(null); }
  };

  const readReceipt = async (actionId: string) => {
    if (pending || !session) return;
    setPending("receipt");
    setError(null);
    setConfirmed(false);
    try { const action = await transport.getAdminAction(actionId); if (mounted.current) remember(action); }
    catch { if (mounted.current) setError("回执暂不可读。请检查会话后重新读取，不会重发执行请求。"); }
    finally { if (mounted.current) setPending(null); }
  };

  const previewValid = preview?.owned_by_current_client && preview.execution_status === "previewed" && (!preview.expires_at || Date.parse(preview.expires_at) > Date.now());
  return <div className="dev-panel admin-command-panel">
    <header className="dev-panel-header"><div><h1>受控控制台</h1><p>先读取真实影响预览，再确认执行。维护动作有独立回执，不会自动重试。</p></div><div className="dev-panel-header__actions">
      {session ? <button className="dev-button" type="button" disabled={pending !== null} onClick={() => void close()}>结束维护会话</button>
        : <button className="dev-button" type="button" disabled={pending !== null} onClick={() => void open()}><LockKeyhole size={15} aria-hidden="true" />{pending === "session" ? "正在开启" : "开启维护会话"}</button>}
    </div></header>
    {session && <p role="status">维护会话有效至 {new Date(session.expires_at_ms).toLocaleTimeString("zh-CN")}。关闭面板会撤销权限。</p>}
    {error && <p role="alert">{error}</p>}
    <div className="console-layout">
      <section className="command-registry"><header><strong>真实命令注册表</strong><span>{registry ? `${registry.commands.filter((command) => command.available).length} 个已接通` : "尚未读取"}</span></header>
        {registry && <label className="admin-command-selector"><span>选择维护动作</span><select aria-label="选择维护动作" value={selectedId} disabled={pending !== null} onChange={(event) => { const command = registry.commands.find((item) => item.command_id === event.target.value); if (command) choose(command); }}>{registry.commands.map((command) => <option key={command.command_id} value={command.command_id}>{command.description}{command.available ? "" : "（未开放）"}</option>)}</select></label>}
        {!registry && <div className="console-empty">开启短期维护会话，读取当前配置的命令参数与回执。</div>}
        {registry?.commands.map((command) => <button key={command.command_id} type="button" disabled={pending !== null} aria-pressed={selectedId === command.command_id} className={selectedId === command.command_id ? "is-active" : ""} onClick={() => choose(command)}>
          <span><strong>{command.command_id}</strong><small>{command.description}</small></span><span>{command.available ? command.level : "未开放"}</span>
        </button>)}
        <div className="forbidden-capabilities"><ShieldCheck size={16} aria-hidden="true" /><span>禁用任意代码和文件命令</span><small>Shell / SQL / Python / 原始密钥 / 隐藏推理</small></div>
      </section>
      <section className="command-runner"><header><div><h2>{selected?.description ?? "请选择维护动作"}</h2></div></header>
        {selected && !selected.available && <p>{selected.unavailable_reason}</p>}
        <label className="code-input"><span>类型化参数（JSON）</span><textarea aria-label="命令参数" spellCheck={false} value={params} disabled={pending !== null || !selected?.available} onChange={(event) => { setParams(event.target.value); setPreview(null); setConfirmed(false); requestKey.current = null; }} /></label>
        <details><summary>参数模式</summary><pre>{selected?.parameter_schema ? JSON.stringify(selected.parameter_schema, null, 2) : "该命令的桌面参数模式尚未提供，不能按空参数执行。"}</pre></details>
        <button className="dev-button" type="button" disabled={pending !== null || !session || !selected?.available} onClick={() => void makePreview()}>{pending === "preview" ? "正在预览" : "预览影响"}</button>
        {preview && <div className="payload-preview"><span>真实影响预览</span><pre>{JSON.stringify(preview.impact_summary, null, 2)}</pre><p>执行状态：{statusText[preview.execution_status] ?? "未识别状态，请核对回执"}</p>{preview.active_turns !== null && <p>当前活动轮次：{preview.active_turns}</p>}<p>回滚类型：{preview.rollback_type ?? "后端未声明"}</p>{preview.dry_run && <p>全局 dry-run 已开启，写入可能被阻止。</p>}</div>}
        <label className="runner-options"><input type="checkbox" checked={confirmed} disabled={!session || !previewValid || pending !== null} onChange={(event) => setConfirmed(event.target.checked)} /><span>我已核对该命令的参数、影响范围和回滚说明</span></label>
        <button className="run-command" type="button" disabled={pending !== null || !session || !selected?.available || !previewValid || !confirmed} onClick={() => void execute()}>{pending === "execute" ? "等待执行回执" : "确认执行"}</button>
      </section>
      <section className="console-output"><header><strong>真实维护回执</strong><span>当前配置，最近 30 天，最多 20 条</span></header><div className="console-output__scroll">
        {history === null ? <div className="console-empty">尚未读取维护历史，不显示模拟输出。</div> : history.length === 0 ? <div className="console-empty">当前配置没有可读取的维护回执。</div> : history.map((action) => <article key={action.action_id}>
          <strong>{action.command_id}</strong><p>{statusText[action.execution_status] ?? "状态未识别"}</p>{action.result_summary.status === "requeued" && <p>事件已重新入队，等待消费者处理。请重新读取事件核对最终结果。</p>}{action.owned_by_current_client === false && <p>此回执来自此前客户端，仅供核对，不能继续执行原预览。</p>}{action.owned_by_current_client == null && <p>执行归属未观测，该回执只能用于核对。</p>}<p>回执 {action.action_id}</p><p>审计 {action.audit_event_id ?? "尚未产生执行审计"}</p><pre>{JSON.stringify(action.result_summary, null, 2)}</pre>
          <button className="dev-button" type="button" disabled={!session || pending !== null} onClick={() => void readReceipt(action.action_id)}><RefreshCw size={14} aria-hidden="true" />读取此回执</button>
        </article>)}
      </div></section>
    </div>
  </div>;
}
