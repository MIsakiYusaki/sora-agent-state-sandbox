import { useEffect, useRef, useState } from "react";
import type { DesktopDebugSession } from "../contracts/generated";
import type { DesktopTransport } from "../transport";
import type { QualityKind, QualityPage, QualityReceipt, QualityRequest, QualityResolutionRequest } from "../transport/DesktopTransport";

const kinds = { runs: "质量运行", baselines: "评测基线", events: "质量事件" };
const resolutions = { investigated: "已调查", superseded: "已由后续记录替代", false_positive: "确认为误报" };
const statuses: Record<string, string> = { passed: "通过", failed: "失败", open: "待调查", resolved: "已结案", active: "有效", running: "运行中", protected: "受保护" };
const metricNames: Record<string, string> = { total_cases: "用例数", passed_cases: "通过数", failed_cases: "失败数", pass_rate: "通过率", accuracy: "准确率", precision: "精确率", recall: "召回率", f1: "F1", latency_ms: "耗时（ms）", duration_ms: "时长（ms）", score: "评分" };
const status = (value: string) => statuses[value] ?? value;

export function QualityInspector({ transport }: { transport: DesktopTransport }) {
  const [session, setSession] = useState<DesktopDebugSession | null>(null);
  const [kind, setKind] = useState<QualityKind>("runs");
  const [page, setPage] = useState<QualityPage | null>(null);
  const [baselines, setBaselines] = useState<QualityPage | null>(null);
  const [baselineId, setBaselineId] = useState("");
  const [resolution, setResolution] = useState<QualityResolutionRequest["resolution"]>("investigated");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<QualityReceipt | null>(null);
  const sessionRef = useRef<DesktopDebugSession | null>(null);
  const sequence = useRef(0);
  const pending = useRef(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    setSession(null); setPage(null); setBaselines(null); setBusy(false); setError(null); setReceipt(null); setConfirmed(false); setBaselineId("");
    pending.current = false;
    return () => {
      mounted.current = false; ++sequence.current; pending.current = false;
      const current = sessionRef.current; sessionRef.current = null;
      if (current) void transport.closeDebugSession(current.session_id).catch(() => undefined);
    };
  }, [transport]);

  const close = (expired = false) => {
    ++sequence.current; pending.current = false;
    const current = sessionRef.current; sessionRef.current = null;
    setSession(null); setPage(null); setBaselines(null); setBusy(false); setConfirmed(false); setReceipt(null); setBaselineId("");
    setError(expired ? "质量治理会话已到期。请重新开启并核对当前状态，未确认的结案不会自动重发。" : null);
    if (current) void transport.closeDebugSession(current.session_id).catch(() => undefined);
  };
  useEffect(() => {
    if (!session) return;
    const timer = window.setTimeout(() => close(true), Math.max(0, session.expires_at_ms - Date.now()));
    return () => window.clearTimeout(timer);
  }, [session, transport]);

  const current = (token: number, owner: DesktopDebugSession) => mounted.current && sequence.current === token && sessionRef.current === owner && owner.expires_at_ms > Date.now();
  const begin = () => {
    if (pending.current) return null;
    pending.current = true; setBusy(true); setError(null); setConfirmed(false);
    return ++sequence.current;
  };
  const finish = (token: number) => { if (mounted.current && sequence.current === token) { pending.current = false; setBusy(false); } };
  const load = async (request: QualityRequest, owner: DesktopDebugSession, token: number, previous: QualityPage | null = null) => {
    const [next, baselinePage] = await Promise.all([
      transport.inspectQuality(request),
      request.kind === "runs" ? transport.inspectQuality({ kind: "baselines" }) : Promise.resolve(null),
    ]);
    if (!current(token, owner)) return;
    if (next.kind !== request.kind || (request.record_id && next.selected?.id !== request.record_id) || (baselinePage && baselinePage.kind !== "baselines")) throw new Error("Quality response ownership mismatch");
    setPage(request.case_offset && previous && previous.selected?.id === request.record_id ? { ...next, cases: [...previous.cases ?? [], ...next.cases ?? []] } : next);
    if (baselinePage) setBaselines(baselinePage);
  };
  const read = async (request: QualityRequest) => {
    const owner = sessionRef.current;
    if (!owner) return;
    const token = begin(); if (token === null) return;
    const previous = page; setPage(null);
    try { await load(request, owner, token, previous); }
    catch { if (current(token, owner)) setError("质量记录读取失败或权限已失效。当前结果未确认，请重新读取。不会把失败显示为空记录或通过。"); }
    finally { finish(token); }
  };
  const open = async () => {
    const token = begin(); if (token === null) return;
    try {
      const opened = await transport.openQualitySession();
      if (!mounted.current || sequence.current !== token || opened.expires_at_ms <= Date.now()) {
        await transport.closeDebugSession(opened.session_id); return;
      }
      sessionRef.current = opened; setSession(opened);
      await load({ kind }, opened, token);
    } catch { if (mounted.current && sequence.current === token) setError("质量治理会话或记录未获得确认。请检查 Host 连接后重新读取；不会自动结案。"); }
    finally { finish(token); }
  };
  const resolve = async () => {
    const owner = sessionRef.current, target = page?.selected;
    if (!owner || !confirmed || !target?.revision || kind !== "events" || target.status !== "open") return;
    const token = begin(); if (token === null) return;
    setReceipt(null);
    try {
      const result = await transport.resolveQualityEvent({ event_id: target.id, expected_revision: target.revision, resolution, confirmed: true });
      if (!current(token, owner)) return;
      if (result.event_id !== target.id || !["resolved", "already_resolved"].includes(result.status) || !result.audit_event_id) throw new Error("Quality receipt mismatch");
      setReceipt(result); setPage(null);
      await load({ kind: "events" }, owner, token);
    } catch {
      if (current(token, owner)) { setPage(null); setError("结案或刷新未获得完整确认。请重新读取事件并核对审计回执；不会自动重复结案。"); }
    } finally { finish(token); }
  };
  const selected = page?.selected;
  return <section className="quality-inspector" aria-label="质量明细与事件治理">
    <header><h2>质量明细与事件治理</h2>{!session
      ? <button type="button" className="dev-button" disabled={busy} onClick={() => void open()}>开启质量治理会话（5 分钟）</button>
      : <div className="quality-tools"><button type="button" className="dev-button" disabled={busy} onClick={() => void read({ kind })}>重新读取质量记录</button><button type="button" className="dev-button" onClick={() => close()}>关闭质量治理会话</button></div>}</header>
    <p>读取此数据空间的真实运行、用例、基线与质量事件。仅展示受控指标和诊断代码；原始输入、预期输出和模型正文不在此披露。事件结案记录调查决定，不会修复算法或改变发布门禁。</p>
    {session && <><p>权限有效至 {new Date(session.expires_at_ms).toLocaleTimeString("zh-CN")}，离开此面板即撤销。</p><div className="quality-tools" role="group" aria-label="质量记录类型">{Object.entries(kinds).map(([key, label]) => <button type="button" className="dev-button" key={key} aria-pressed={key === kind} disabled={busy} onClick={() => { setKind(key as QualityKind); setReceipt(null); setBaselineId(""); void read({ kind: key as QualityKind }); }}>{label}</button>)}</div></>}
    {busy && <p role="status">正在核对真实质量记录…</p>}{error && <p role="alert">{error}</p>}
    {receipt && <p role="status">事件 {receipt.event_id}：{receipt.status === "resolved" ? "已结案" : "此前已结案"}；审计回执 {receipt.audit_event_id}。</p>}
    {page && <><p>已读取 {page.items.length} 条{kinds[kind]}{page.truncated ? "（仅列出最近 100 条，未覆盖全部历史）" : ""}</p>
      <div className="quality-records">{page.items.map(item => <button type="button" key={item.id} aria-pressed={selected?.id === item.id} disabled={busy} onClick={() => { setBaselineId(""); void read({ kind, record_id: item.id }); }}><strong>{item.label}</strong><span>{status(item.status)} · {item.created_at ?? "时间未报告"}</span><code>{item.id}</code></button>)}</div>
      {page.items.length === 0 && <p>当前查询确认没有记录。</p>}
    </>}
    {selected && <section className="quality-detail" aria-label="所选质量记录"><h3>记录明细</h3><dl><div><dt>记录</dt><dd>{selected.id}</dd></div><div><dt>类型</dt><dd>{selected.label}</dd></div><div><dt>状态</dt><dd>{status(selected.status)}</dd></div><div><dt>关联运行</dt><dd>{selected.related_run_id ?? "未报告"}</dd></div></dl>
      {kind === "events" && <dl><div><dt>严重程度</dt><dd>{selected.severity ?? "未报告"}</dd></div><div><dt>来源类型</dt><dd>{selected.source_type ?? "未报告"}</dd></div><div><dt>来源指纹</dt><dd>{selected.source_fingerprint ?? "未报告"}</dd></div><div><dt>结案时间</dt><dd>{selected.resolved_at ?? "未报告"}</dd></div></dl>}
      {selected.related_run_id && <button type="button" className="dev-button" disabled={busy} onClick={() => { setKind("runs"); setBaselineId(""); void read({ kind: "runs", record_id: selected.related_run_id! }); }}>查看来源运行</button>}
      <Metrics values={page!.summary} />
      {kind === "runs" && <><label className="quality-baseline">对比基线<select aria-label="对比基线" disabled={busy} value={baselineId} onChange={event => setBaselineId(event.target.value)}><option value="">选择真实基线</option>{baselines?.items.map(item => <option key={item.id} value={item.id}>{item.id} · {status(item.status)}</option>)}</select></label><button type="button" className="dev-button" disabled={busy || !baselineId} onClick={() => void read({ kind: "runs", record_id: selected.id, baseline_id: baselineId })}>读取基线对比</button>
        {baselines?.truncated && <p>仅提供最近 100 个基线。</p>}{page!.comparison && <section aria-label="基线比较结果"><h4>基线比较：{status(page!.comparison.status)}</h4><p>回归 {page!.comparison.regression_count} 项；未接受回归 {page!.comparison.unaccepted_regression_count} 项。基线通过率 {page!.comparison.baseline_pass_rate ?? "未报告"}；当前通过率 {page!.comparison.current_pass_rate ?? "未报告"}。</p><p>比较针对当前所选运行与基线，不代表整体发布验收通过。</p></section>}
        <h4>用例明细（已读取 {page!.cases?.length ?? "未报告"} / {page!.case_count ?? "未报告"}）</h4>
        <div className="quality-cases">{page!.cases?.map(item => <article key={item.id}><h5>{item.suite} / {item.name}</h5><p>{status(item.status)} · {item.severity} · 失败分类：{item.failure_category ?? "未报告"}</p><code>{item.id}</code><Metrics values={item.metrics} /></article>)}</div>
        {page!.next_case_offset != null && <button type="button" className="dev-button" disabled={busy} onClick={() => void read({ kind: "runs", record_id: selected.id, baseline_id: page!.comparison?.baseline_id, case_offset: page!.next_case_offset! })}>加载更多用例</button>}
      </>}
      {kind === "events" && selected.status === "open" && <fieldset disabled={busy}><legend>单条事件结案</legend><label>调查决定<select aria-label="质量事件结案决定" value={resolution} onChange={event => { setResolution(event.target.value as QualityResolutionRequest["resolution"]); setConfirmed(false); }}>{Object.entries(resolutions).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="quality-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已核对当前事件和调查决定，确认结案并保留审计记录</label><button type="button" className="dev-button" disabled={!confirmed || !selected.revision} onClick={() => void resolve()}>确认结案</button></fieldset>}
    </section>}
  </section>;
}

function Metrics({ values }: { values: Readonly<Record<string, number>> }) {
  const entries = Object.entries(values);
  return entries.length ? <dl className="quality-metrics">{entries.map(([key, value]) => <div key={key}><dt>{metricNames[key] ?? key}</dt><dd>{value}</dd></div>)}</dl> : <p>没有已报告的数值指标。</p>;
}
