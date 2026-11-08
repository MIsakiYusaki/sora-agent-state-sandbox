import { useEffect, useRef, useState } from "react";
import type { DesktopDebugSession } from "../contracts/generated";
import type { DesktopTransport } from "../transport";
import type { GovernanceKind, GovernancePage, GovernanceRow, ReviewResolutionRequest } from "../transport/DesktopTransport";

const kindLabels = { reviews: "审核队列", candidates: "候选", quarantines: "隔离记录" };
const actionLabels = { approve: "批准", reject: "拒绝", defer: "延期七天" };
export function MemoryGovernancePanel({ transport, initialKind = "reviews" }: { transport: DesktopTransport; initialKind?: GovernanceKind }) {
  const [session, setSession] = useState<DesktopDebugSession | null>(null);
  const sessionRef = useRef<DesktopDebugSession | null>(null);
  const mounted = useRef(true);
  const [kind, setKind] = useState<GovernanceKind>(initialKind);
  const [page, setPage] = useState<GovernancePage | null>(null);
  const [selected, setSelected] = useState<GovernanceRow | null>(null);
  const [action, setAction] = useState<ReviewResolutionRequest["action"]>("approve");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<string | null>(null);
  const request = useRef(0);
  const pending = useRef(false);
  useEffect(() => {
    mounted.current = true;
    pending.current = false; setSession(null); setPage(null); setSelected(null); setBusy(false); setConfirmed(false); setReceipt(null); setError(null);
    return () => { mounted.current = false; ++request.current; pending.current = false; const current = sessionRef.current; sessionRef.current = null; if (current) void transport.closeDebugSession(current.session_id).catch(() => undefined); };
  }, [transport]);
  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => {
      ++request.current;
      pending.current = false;
      setBusy(false);
      sessionRef.current = null; setSession(null); setConfirmed(false); setSelected(null); setPage(null);
      setError("审核会话已到期。请重新开启并读取当前状态，不会自动重发决定。");
      void transport.closeDebugSession(session.session_id).catch(() => undefined);
    }, Math.max(0, session.expires_at_ms - Date.now()));
    return () => window.clearTimeout(timer);
  }, [session, transport]);
  const read = async (nextKind: GovernanceKind, offset = 0) => {
    const currentRequest = ++request.current;
    pending.current = true;
    const previous = page;
    setBusy(true); setError(null); setPage(null); setSelected(null); setConfirmed(false);
    try {
      const next = await transport.getMemoryGovernance(nextKind, offset);
      if (!mounted.current || currentRequest !== request.current) return;
      if (next.items.some(item => item.kind !== nextKind)) throw new Error("governance response kind mismatch");
      setPage(offset && previous ? { ...next, items: [...previous.items, ...next.items.filter(item => !previous.items.some(old => old.id === item.id))] } : next);
    }
    catch { if (mounted.current && currentRequest === request.current) setError("治理记录读取失败或权限已到期。请重新读取；不显示旧队列冒充当前状态。"); }
    finally { if (mounted.current && currentRequest === request.current) { pending.current = false; setBusy(false); } }
  };
  const open = async () => {
    if (pending.current) return;
    pending.current = true; const opening = ++request.current;
    setBusy(true); setError(null);
    try {
      const opened = await transport.openReviewSession();
      if (!mounted.current || opening !== request.current) { await transport.closeDebugSession(opened.session_id); return; }
      sessionRef.current = opened; setSession(opened); await read(kind);
    } catch { if (mounted.current && opening === request.current) setError("审核会话未开启，请核对 Host 连接。不会自动处理任何记忆。"); }
    finally { if (mounted.current && opening === request.current) { pending.current = false; setBusy(false); } }
  };
  const resolve = async () => {
    if (!selected?.revision || !confirmed || !session || busy || pending.current) return;
    pending.current = true; const resolving = ++request.current;
    setBusy(true); setError(null); setConfirmed(false); setReceipt(null);
    try {
      const result = await transport.resolveMemoryReview({ review_id: selected.id, expected_revision: selected.revision, action, confirmed: true });
      if (!mounted.current || resolving !== request.current) return;
      if (result.review_id !== selected.id) throw new Error("review receipt ownership mismatch");
      setReceipt(`审核记录 ${result.review_id}：${result.status}；决定 ${result.resolution ?? "未报告"}；时间 ${result.resolved_at ?? "未报告"}。`);
      await read(kind);
    } catch { if (mounted.current && resolving === request.current) { setSelected(null); setError("决定未获得完整确认，目标可能已变化。请先重新读取核对，不会自动重试。"); } }
    finally { if (mounted.current && resolving === request.current) { pending.current = false; setBusy(false); } }
  };
  const dismiss = async () => {
    const target = selected;
    if (!target?.can_dismiss || !target.revision || !confirmed || !session || pending.current) return;
    pending.current = true; const resolving = ++request.current;
    setBusy(true); setError(null); setConfirmed(false); setReceipt(null);
    try {
      const result = await transport.dismissQuarantine({ quarantine_id: target.id, expected_revision: target.revision, confirmed: true });
      if (!mounted.current || resolving !== request.current) return;
      if (result.quarantine_id !== target.id || !["dismissed", "already_dismissed"].includes(result.status) || !result.audit_id) throw new Error("quarantine receipt mismatch");
      setReceipt(`隔离记录 ${result.quarantine_id}：候选已拒绝，隔离处置已记录；审计 ${result.audit_id}；时间 ${result.resolved_at}。`);
      await read("quarantines");
    } catch { if (mounted.current && resolving === request.current) { setSelected(null); setPage(null); setError("隔离处置结果未获得完整确认，请重新读取并核对审计记录；不会自动重复处理。"); } }
    finally { if (mounted.current && resolving === request.current) { pending.current = false; setBusy(false); } }
  };
  return <section className="memory-governance" aria-label="记忆治理记录">
    <header><h2>记忆治理记录</h2>{!session ? <button className="dev-button" type="button" disabled={busy} onClick={() => void open()}>开启记忆审核会话（5 分钟）</button> : <button className="dev-button" type="button" disabled={busy} onClick={() => void read(kind)}>重新读取队列</button>}</header>
    <p>读取当前数据空间的低敏候选摘要和治理原因。此会话允许逐条审核或隔离处置。原始证据不披露；敏感内容保持受保护，不能在这里盲批。</p>
    {session && <p role="status">审核权限有效至 {new Date(session.expires_at_ms).toLocaleTimeString("zh-CN")}，离开此面板即撤销。</p>}
    {session && <div role="group" aria-label="治理队列">{Object.entries(kindLabels).map(([key, label]) => <button type="button" key={key} aria-pressed={kind === key} disabled={busy} onClick={() => { setKind(key as GovernanceKind); void read(key as GovernanceKind); }}>{label}</button>)}</div>}
    {error && <p role="alert">{error}</p>}{receipt && <p role="status">{receipt}</p>}{busy && <p role="status">正在核对真实治理状态…</p>}
    {page && <><p>{page.items.length} 条已读取记录{page.truncated ? "（还有更多记录；队列变化后请重新读取以核对）" : ""}</p><div className="governance-records">{page.items.map(item => <button className="governance-record" type="button" key={item.id} aria-pressed={selected?.id === item.id} disabled={busy} onClick={() => { setSelected(item); setConfirmed(false); setAction(item.can_approve ? "approve" : "reject"); }}><strong>{item.summary ?? "受保护的内容（不展示原文）"}</strong><span>{item.status} · {item.memory_type ?? kindLabels[item.kind]}</span><code>{item.id}</code></button>)}</div>{page.next_offset != null && <button className="dev-button" type="button" disabled={busy} onClick={() => void read(kind, page.next_offset!)}>加载更多记录</button>}{page.items.length === 0 && <p>当前查询确实没有记录。</p>}</>}
    {selected && <section className="governance-detail"><h3>核对记录</h3><p>{selected.summary ?? "摘要受保护；需要单独授权披露的内容不能在此直接批准。"}</p><dl><div><dt>记录</dt><dd>{selected.id}</dd></div><div><dt>状态</dt><dd>{selected.status}</dd></div><div><dt>治理原因</dt><dd>{selected.reason_codes.join("、") || "未投影原因代码"}</dd></div><div><dt>证据数量</dt><dd>{selected.evidence_count ?? "未披露"}</dd></div></dl>
      {selected.kind === "quarantines" && <><dl><div><dt>风险类型</dt><dd>{selected.risk_type ?? "未报告"}</dd></div><div><dt>严重程度</dt><dd>{selected.severity ?? "未报告"}</dd></div></dl><p>结束隔离只会拒绝关联候选，保留处置审计，不会公开内容或写入长期记忆。如需保留这项信息，请以允许保存的内容重新提出并接受 Gate 校验。</p>{selected.can_dismiss && <fieldset disabled={busy}><legend>拒绝此候选并结束隔离</legend><label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已核对该条风险原因，确认拒绝关联候选并记录隔离处置</label><button className="dev-button" type="button" disabled={!confirmed || !selected.revision} onClick={() => void dismiss()}>确认拒绝候选并结束隔离</button></fieldset>}</>}
      {selected.kind === "reviews" && selected.status === "open" && <fieldset disabled={busy}><legend>决定（只处理当前这一条）</legend><label>操作<select aria-label="审核决定" value={action} onChange={event => { setAction(event.target.value as ReviewResolutionRequest["action"]); setConfirmed(false); }}>{Object.entries(actionLabels).map(([key, label]) => <option key={key} value={key} disabled={key === "approve" && !selected.can_approve}>{label}</option>)}</select></label><label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已核对该条摘要、原因与决定；批准可能写入长期记忆，拒绝/延期会改变此候选状态</label><button className="dev-button" type="button" disabled={!confirmed || !selected.revision} onClick={() => void resolve()}>确认{actionLabels[action]}</button></fieldset>}
    </section>}
  </section>;
}
