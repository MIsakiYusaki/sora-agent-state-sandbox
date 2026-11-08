import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { CompanionGovernanceDecision, CompanionGovernanceRow, CompanionGovernanceView } from "../transport/companion";
import type { DesktopDebugSession } from "../contracts/generated";

const statusLabels: Record<string, string> = { active: "有效", inactive: "已停用", deactivated: "已撤销", committed: "已提交", pending_review: "待审核", rejected: "已拒绝", proposed: "已提案", deferred: "已延期", pending: "待处理", quarantined: "已隔离", forgotten: "已遗忘" };
export function CompanionGovernance({ transport, domain }: { transport: DesktopTransport; domain: "persona" | "relationship" }) {
  const [data, setData] = useState<CompanionGovernanceView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [session, setSession] = useState<DesktopDebugSession | null>(null);
  const [selected, setSelected] = useState<CompanionGovernanceRow | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [receipt, setReceipt] = useState<string | null>(null);
  const epoch = useRef(0);
  const currentSession = useRef<string | null>(null);
  const writing = useRef(false);
  async function read() {
    const current = ++epoch.current; setData(null); setError(null); setBusy(true); setSelected(null); setConfirmed(false);
    try { const result = await transport.getCompanionGovernance(domain); if (current === epoch.current && result.domain === domain) setData(result); }
    catch { if (current === epoch.current) setError("领域治理读取失败，不能据此认定没有提案或贡献。"); }
    finally { if (current === epoch.current) setBusy(false); }
  }
  useEffect(() => { void read(); return () => { ++epoch.current; }; }, [transport, domain]);
  useEffect(() => () => { const id = currentSession.current; if (id) void transport.closeDebugSession(id).catch(() => {}); }, [transport]);
  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => { setSession(null); setSelected(null); setConfirmed(false); setError("领域管理会话已到期，请重新开启并读取目标。不会自动重发操作。"); }, Math.max(0, session.expires_at_ms - Date.now()));
    return () => window.clearTimeout(timer);
  }, [session]);
  async function openSession() {
    if (writing.current) return;
    writing.current = true; setBusy(true); setError(null); setSelected(null); setConfirmed(false);
    const current = epoch.current;
    try {
      const result = await transport.openCompanionSession();
      if (current !== epoch.current) { await transport.closeDebugSession(result.session_id); return; }
      currentSession.current = result.session_id; setSession(result);
    } catch { if (current === epoch.current) setError("领域管理权限未能开启，请重新核对；没有执行操作。"); }
    finally { writing.current = false; if (current === epoch.current) setBusy(false); }
  }
  function preview(row: CompanionGovernanceRow) { setSelected(row); setConfirmed(false); setReceipt(null); }
  async function resolve() {
    if (!selected || !confirmed || !session || writing.current || session.expires_at_ms <= Date.now()) return;
    const request: CompanionGovernanceDecision = { domain, target_id: selected.id, expected_revision: selected.revision, action: selected.kind === "candidate" ? "reject_candidate" : "rollback_contribution", confirmed: true };
    writing.current = true; setBusy(true); setError(null);
    const current = epoch.current;
    try {
      const result = await transport.resolveCompanionGovernance(request);
      if (current === epoch.current) { setReceipt(`已生效；审计 ${result.audit_id}，耗时 ${result.duration_ms.toFixed(1)} ms。`); await read(); }
    } catch {
      if (current === epoch.current) { setSelected(null); setConfirmed(false); setData(null); setError("执行结果未能确认，可能已生效、修订冲突或权限已撤销。请重新读取并核对审计，不会自动重发。"); }
    } finally { writing.current = false; if (current === epoch.current) setBusy(false); }
  }
  return <section className="companion-governance" aria-label="真实领域提案与贡献">
    <header className="dev-panel-header"><div><h2>提案、贡献与审计</h2><p>当前身份的真实持久记录；来源仅显示指纹，不披露原文。此处读取不会触发领域更新。</p></div><button className="dev-button" type="button" disabled={busy} onClick={() => void read()}>重新读取领域治理</button></header>
    <div className="space-settings-tools"><button className="dev-button" type="button" disabled={busy} onClick={() => void openSession()}>开启5分钟领域管理</button><span>{session ? "专用管理会话已开启，所有操作仍须逐条核对确认。" : "当前仅查看记录；管理权限由原生窗口临时持有。"}</span></div>
    {busy && <p role="status">正在核对领域记录与操作…</p>}{error && <p className="inline-error" role="alert">{error}</p>}{receipt && <p role="status">{receipt}</p>}
    {selected && <section className="space-settings" role="group" aria-label="领域治理影响预览"><h3>核对治理影响</h3><p>目标：{selected.id}；领域字段：{selected.target}；当前状态：{selected.status}。</p><p>{selected.kind === "candidate" ? "拒绝这条待审核提案，保留历史及审计，不会生成领域贡献。" : "撤销这条有效贡献，并按其余有效记录重建领域状态；这会影响后续回应，不会删除原始对话。"}</p><p>精确修订：{selected.revision}</p><label className="space-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />我已核对目标和影响，确认执行</label><div className="space-settings-tools"><button className="dev-button" type="button" disabled={busy || !confirmed || !session} onClick={() => void resolve()}>执行这次治理</button><button className="dev-button" type="button" disabled={busy} onClick={() => setSelected(null)}>取消治理</button></div></section>}
    {data && <><p>本次读取 {data.duration_ms.toFixed(1)} ms。{data.truncated ? "每类仅显示最近100条，当前覆盖不完整。" : "以下为本次实际读取的记录。"}</p>
      {(["candidates", "contributions"] as const).map(kind => <section key={kind}><h3>{kind === "candidates" ? "成长与关系提案" : "已形成的领域贡献"}</h3>{data[kind].length === 0 ? <p>当前读取结果中没有此类记录。</p> : data[kind].map(row => <details key={row.id} className="space-settings"><summary>{statusLabels[row.status] ?? "其他状态"} · {row.target} · {row.id}</summary><dl><dt>记录标识</dt><dd>{row.id}</dd><dt>实际状态</dt><dd>{row.status}</dd><dt>关联提案</dt><dd>{row.candidate_id ?? "未报告"}</dd><dt>创建时间</dt><dd>{row.created_at ?? "未报告"}</dd><dt>治理原因码</dt><dd>{row.reason}</dd><dt>来源指纹</dt><dd>{row.source_fingerprints.join("、") || "未报告"}</dd><dt>记录修订</dt><dd>{row.revision}</dd></dl>{(row.kind === "candidate" ? row.status === "pending_review" || (domain === "relationship" && row.status === "deferred") : row.status === "active") && <button className="dev-button" type="button" disabled={busy || !session || selected !== null} onClick={() => preview(row)}>{row.kind === "candidate" ? "预览拒绝此提案" : "预览撤销此贡献"}</button>}</details>)}</section>)}
      <section><h3>领域审计</h3>{data.audits.length === 0 ? <p>当前读取结果中没有可展示的审计记录。</p> : data.audits.map((row, index) => <details key={`${row.id}:${index}`} className="space-settings"><summary>{row.action} · {row.created_at ?? "未报告时间"}</summary><p>目标：{row.entity_id}</p><p>审计：{row.id}</p></details>)}</section>
    </>}
  </section>;
}
