import { useEffect, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { MemoryQueuePage } from "../transport/DesktopTransport";
import { AdminCommandPanel } from "./AdminCommandPanel";

const outcomeText = { applied: "已应用", skipped_duplicate: "幂等跳过重复事件", rejected_policy: "依据当前状态拒绝应用", unobserved: "未报告" };

export function MemoryQueuePanel({ transport }: { transport: DesktopTransport }) {
  const [kind, setKind] = useState<"index" | "projection">("index");
  const [status, setStatus] = useState<"all" | "pending" | "failed" | "dead_letter">("failed");
  const [page, setPage] = useState<MemoryQueuePage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [maintenance, setMaintenance] = useState(false);
  const [singleRecovery, setSingleRecovery] = useState(false);
  useEffect(() => {
    let current = true;
    setLoading(true); setError(false); setPage(null); setSelected(null); setSingleRecovery(false);
    void transport.getMemoryQueue(kind, status).then(value => {
      if (value.kind !== kind) throw new Error("queue scope mismatch");
      if (current) setPage(value);
    }).catch(() => { if (current) setError(true); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [transport, kind, status, epoch]);
  const row = page?.items.find(item => item.id === selected);
  return <section className="memory-queue-panel" aria-label="索引与投影事件定位"><h2>索引与投影事件定位</h2>
    <div className="governance-actions"><label>队列<select aria-label="记忆事件队列" value={kind} onChange={event => { setKind(event.target.value as typeof kind); setMaintenance(false); }}><option value="index">索引</option><option value="projection">投影</option></select></label><label>状态<select aria-label="记忆事件状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}><option value="all">全部状态</option><option value="pending">等待</option><option value="failed">失败</option><option value="dead_letter">死信</option></select></label><button className="dev-button" type="button" disabled={loading} onClick={() => setEpoch(value => value + 1)}>重新读取事件</button></div>
    <p>按真实事件定位后核对尝试与目标。载荷、记忆正文和错误原文不披露；错误指纹用于关联，不是故障原因。</p>
    {loading && <p role="status">正在读取事件…</p>}{error && <p role="alert">事件读取失败，不能据此认为队列为空。</p>}
    {page && <><p>本次读取 {page.items.length} 条{page.truncated ? "，达到上限；不是全部记录" : ""}</p><div className="governance-records">{page.items.map(item => <button className="governance-record" type="button" key={item.id} aria-pressed={selected === item.id} onClick={() => { setSelected(item.id); setSingleRecovery(false); }}><strong>{item.backend ?? item.target ?? "目标未投影"} · {item.status}</strong><span>尝试 {item.attempts ?? "未报告"} · {item.action ?? "动作未投影"}</span><code>{item.id}</code></button>)}</div>{page.items.length === 0 && <p>当前状态过滤下确实没有事件。</p>}</>}
    {row && <dl className="queue-record-detail"><div><dt>事件标识</dt><dd>{row.id}</dd></div><div><dt>状态</dt><dd>{row.status}</dd></div><div><dt>消费者结果</dt><dd>{outcomeText[row.outcome ?? "unobserved"] ?? "未识别，请核对回执"}</dd></div><div><dt>尝试次数</dt><dd>{row.attempts ?? "未报告"}</dd></div><div><dt>创建时间</dt><dd>{row.created_at ?? "未报告"}</dd></div><div><dt>下次尝试</dt><dd>{row.next_attempt_at ?? "没有报告计划"}</dd></div><div><dt>错误指纹</dt><dd>{row.error_fingerprint ?? "未记录错误"}</dd></div></dl>}
    {row?.can_requeue && row.revision && /^[a-f0-9]{64}$/.test(row.revision) && <><button className="dev-button" type="button" onClick={() => { setSingleRecovery(value => !value); setMaintenance(false); }}>{singleRecovery ? "关闭单条事件恢复" : "预览此事件重新入队"}</button><p>重新入队保留原事件载荷、幂等身份和累计尝试次数，增加三次尝试机会。消费者是否完成，需要重新读取事件核对。</p>{singleRecovery && <AdminCommandPanel key={`${kind}-${row.id}-${row.revision}`} transport={transport} initialCommandId="memory.event.requeue" initialParams={{ kind, event_id: row.id, expected_revision: row.revision }} />}</>}
    {row && !row.can_requeue && (row.status === "dead_letter" || row.status === "failed" || row.error_fingerprint) && <p>当前回执尚未允许单条重试，请重新读取并核对真实状态。</p>}
    <button className="dev-button" type="button" onClick={() => { setMaintenance(value => !value); setSingleRecovery(false); }}>{maintenance ? "关闭队列维护工具" : "打开队列维护工具"}</button>
    {maintenance && <><p>此工具预览并处理队列中符合重试条件的事件，不是强制重放选中行，也不会把死信伪装成已重试。执行后请重新读取事件核对。</p><AdminCommandPanel key={kind} transport={transport} initialCommandId={kind === "index" ? "memory.index.drain" : "memory.projection.drain"} /></>}
  </section>;
}
