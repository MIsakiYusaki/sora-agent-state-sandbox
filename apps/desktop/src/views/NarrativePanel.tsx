import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { NarrativeActionRequest, NarrativeEvent, NarrativeView } from "../transport/narrative";

const states: Record<string, string> = { dormant: "还未开始", hinted: "可以看看", invited: "等你选择", active: "正在经历", completed: "已经完成", archived: "已经结束" };
const memoryStates: Record<string, string> = { not_requested: "尚未形成已完成的共同经历", not_observed: "共同记忆结果尚未确认", evidence_only: "已保存经历证据，候选结果尚未确认", needs_review: "共同记忆等待审核", pending_review: "共同记忆等待审核", pending: "共同记忆候选待处理", committed: "共同记忆已保存", rejected: "这段经历没有写入长期记忆", deferred: "共同记忆审核已延期" };

export function NarrativePanel({ transport }: { transport: DesktopTransport }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<NarrativeView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const epoch = useRef(0);
  const writing = useRef(false);
  async function read() {
    if (writing.current) return;
    const current = ++epoch.current; setBusy(true); setError(null);
    try { const value = await transport.getNarrative(); if (epoch.current === current) setData(value); }
    catch { if (epoch.current === current) { setData(null); setError("故事读取失败，不能据此认定没有内容或经历。请重新读取。"); } }
    finally { if (epoch.current === current) setBusy(false); }
  }
  useEffect(() => {
    ++epoch.current; setData(null); setNotice(null); setError(null); writing.current = false;
    if (open) void read();
    return () => { ++epoch.current; };
  }, [transport, open]);

  async function choose(event: NarrativeEvent, action: "invite" | "choice", choiceId: string | null = null) {
    if (!data?.bundle_id || !data.canon_version || writing.current) return;
    const request: NarrativeActionRequest = { request_id: "narrative-" + crypto.randomUUID(), instance_id: event.id,
      expected_revision: event.revision, bundle_id: data.bundle_id, canon_version: data.canon_version, action, choice_id: choiceId };
    const current = ++epoch.current; writing.current = true; setBusy(true); setError(null); setNotice(null);
    try {
      const result = await transport.narrativeAction(request);
      if (result.receipt.request_id !== request.request_id || result.receipt.instance_id !== request.instance_id) throw new Error("narrative_receipt_scope_mismatch");
      if (epoch.current === current) { setData(result.view); setNotice(action === "invite" ? "已经展开这段故事，你可以按自己的意愿选择。" : "选择已保存，下面显示实际任务与经历记录。共同记忆会单独经过审核。"); }
    } catch {
      if (epoch.current === current) { setData(null); setError("选择结果未能确认，可能已保存、故事已更新或状态发生变化。请重新读取核对，不会自动重发。"); }
    } finally {
      writing.current = false;
      if (epoch.current === current) setBusy(false);
    }
  }

  return <section className="context-section narrative-panel" aria-label="一起经历">
    <h2>一起经历</h2><p>和 Sora 看看世界里正在发生的小事。可以参与，也可以忽略。</p>
    <button className="text-link" type="button" disabled={busy} aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收起故事与经历" : "打开故事与经历"}</button>
    {open && <>
      <button className="text-link" type="button" disabled={busy} onClick={() => void read()}>重新读取故事</button>
      {busy && <p role="status">正在核对故事与经历记录…</p>}{error && <p className="inline-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      {data && <>
        {data.source === "local_preview" && <p className="narrative-source">当前为本机开发预览故事，尚未接入正式内容更新。你的选择仍会在当前独立空间真实保存。</p>}
        {!data.available ? <p role="status">{data.unavailable_reason === "content_verification_failed" ? "故事内容未通过当前可信校验，暂时不能参与。" : "当前还没有可用的已签名故事内容。"}</p> : <>
          <p>{data.world_name} · 只有你参与且有证据的已完成经历，才会进入共同记忆审核。</p>
          {data.truncated && <p>当前只展示部分可读取的故事，不代表全部内容。</p>}
          {data.events.length === 0 && <p>当前没有可展示的故事。</p>}
          {data.events.map(event => <details className="narrative-event" key={event.id}>
            <summary>{event.title}<span>{states[event.state] ?? "状态待核对"}</span></summary>
            <p>{event.summary}</p>
            {event.can_invite && <button className="button button--secondary" type="button" disabled={busy} onClick={() => void choose(event, "invite")}>看看这段故事</button>}
            {event.choices.length > 0 && <fieldset disabled={busy}><legend>你的选择</legend>{event.choices.map(choice => <button className="button button--secondary" type="button" key={choice.id} onClick={() => void choose(event, "choice", choice.id)}>{choice.label}</button>)}</fieldset>}
            {event.selected_choice && <p>你已选择：{event.selected_choice}</p>}
            {event.tasks.map(task => <p key={task.id}>{task.completed === true ? "已完成" : task.completed === false ? "尚未完成" : "结果待核对"}：{task.title}</p>)}
            <p>{memoryStates[event.memory_state] ?? "共同记忆处理状态需要重新核对"}。</p>
            <details><summary>查看这段经历的记录</summary>{event.history.length === 0 ? <p>尚无状态变化记录。</p> : <ol>{event.history.map(item => <li key={item.id}><span>{states[item.state] ?? "状态发生变化"}</span><time dateTime={item.occurred_at}>{new Date(item.occurred_at).toLocaleString("zh-CN")}</time></li>)}</ol>}</details>
          </details>)}
        </>}
      </>}
    </>}
  </section>;
}
