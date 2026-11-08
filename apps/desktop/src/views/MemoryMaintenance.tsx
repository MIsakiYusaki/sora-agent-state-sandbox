import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { MemoryMaintenanceView } from "../transport/maintenance";

export function MemoryMaintenance({ transport }: { transport: DesktopTransport }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MemoryMaintenanceView | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const epoch = useRef(0), writing = useRef(false);
  const date = (value: string) => new Date(value).toLocaleString("zh-CN");
  async function read() {
    if (writing.current) return;
    const current = ++epoch.current; setBusy(true); setError(null); setConfirm(false);
    try { const value = await transport.getMemoryMaintenance(); if (epoch.current === current) setData(value); }
    catch { if (epoch.current === current) { setData(null); setError("自动整理状态读取失败，请重新读取后再操作。"); } }
    finally { if (epoch.current === current) setBusy(false); }
  }
  useEffect(() => {
    ++epoch.current; setData(null); setConfirm(false); setError(null); writing.current = false;
    if (open) void read();
    return () => { ++epoch.current; };
  }, [transport, open]);
  async function save() {
    if (!data || writing.current) return;
    const paused = !data.paused, current = ++epoch.current;
    writing.current = true; setBusy(true); setError(null);
    try {
      const value = await transport.setMemoryMaintenance({ paused, expected_revision: data.revision, confirmed: true });
      if (value.paused !== paused || value.revision === data.revision) throw new Error("maintenance_receipt_mismatch");
      if (epoch.current === current) { setData(value); setConfirm(false); }
    } catch { if (epoch.current === current) { setData(null); setConfirm(false); setError("更改结果尚未确认，请重新读取核对。不会自动重发。"); } }
    finally { writing.current = false; if (epoch.current === current) setBusy(false); }
  }
  return <section className="memory-maintenance" aria-label="自动整理记忆">
    <button className="text-link" type="button" disabled={busy} aria-expanded={open} onClick={() => setOpen(value => !value)}>自动整理记忆{open ? " · 收起" : " · 查看与设置"}</button>
    {open && <>
      <p>定期整理记忆的常用程度、处理已到期的内容，并维护遗忘标记。你手动要求忘记的内容始终优先。</p>
      <p>暂停只影响周期整理；新对话的记忆审核、手动忘记和已确认的隐私设置仍会执行。</p>
      {busy && <p role="status">正在核对维护状态…</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {data && <>
        <p role="status">{data.paused ? "周期整理已暂停" : !data.running ? "周期整理已开启，服务当前未运行" : data.last_error ? "周期整理遇到问题，服务会重试" : "周期整理已开启"}</p>
        <p>{data.last_completed_at ? `最近完成：${date(data.last_completed_at)}` : "尚无已完成的周期整理记录。"}</p>
        {data.last_result && <p>最近整理了 {data.last_result.scored_count ?? 0} 条记忆，处理了 {data.last_result.expired_memory_count ?? 0} 条到期记忆和 {data.last_result.expired_evidence_count ?? 0} 条到期依据。</p>}
        {!data.paused && data.next_due_at && <p>下次检查时间：{date(data.next_due_at)}。客户端运行时才会执行。</p>}
        {confirm ? <div className="maintenance-confirm" role="region" aria-label="确认周期整理更改"><p>{data.paused ? "恢复定期整理记忆？这不会自动批准新的语义巩固。" : "暂停定期整理记忆？你可以随时回来恢复。"}</p><button className="button button--primary" type="button" disabled={busy} onClick={() => void save()}>{data.paused ? "确认恢复" : "确认暂停"}</button><button className="button button--secondary" type="button" disabled={busy} onClick={() => setConfirm(false)}>取消更改</button></div>
          : <button className="button button--secondary" type="button" disabled={busy} onClick={() => setConfirm(true)}>{data.paused ? "恢复周期整理" : "暂停周期整理"}</button>}
      </>}
      <button className="text-link" type="button" disabled={busy} onClick={() => void read()}>重新读取维护状态</button>
    </>}
  </section>;
}
