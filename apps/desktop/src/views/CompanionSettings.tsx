import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { CompanionActionRequest, CompanionView, ComfortRhythm, SurfacePermission } from "../transport/companion";
import { Switch } from "../components/ui";

const rhythms: Record<ComfortRhythm, string> = {
  gentle_low_assumption: "慢慢来，少一些猜测", warm_low_pressure: "温和陪伴，不给压力", warm_flexible: "亲切自然，灵活回应",
};
const repairs: Record<string, string> = {
  rupture_detected: "有需要处理的不愉快", acknowledged: "已经正视问题", cooling_off: "先缓一缓",
  repair_in_progress: "正在沟通和修复", repaired: "我确认已经修复", accepted_difference: "接受彼此的不同", unresolved: "还没有解决",
};
const repairActions: Record<string, string> = {
  acknowledged: "一起正视这个问题", cooling_off: "先缓一缓", repair_in_progress: "继续沟通和修复",
  repaired: "我确认已经修复", accepted_difference: "我接受这个不同", unresolved: "还没有解决",
};
type Pending = { request: CompanionActionRequest; description: string };

export function CompanionSettings({ transport }: { transport: DesktopTransport }) {
  const [data, setData] = useState<CompanionView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [rhythm, setRhythm] = useState<ComfortRhythm>("gentle_low_assumption");
  const generation = useRef(0);
  const inFlight = useRef(false);

  async function read() {
    if (inFlight.current) return;
    const current = ++generation.current;
    inFlight.current = true; setBusy(true); setError(null); setPending(null);
    try {
      const result = await transport.getCompanion();
      if (generation.current === current) setData(result);
    } catch {
      if (generation.current === current) { setData(null); setError("陪伴设置读取失败，请重新读取后再操作。"); }
    } finally {
      if (generation.current === current) { inFlight.current = false; setBusy(false); }
    }
  }
  useEffect(() => {
    inFlight.current = false; setData(null); setNotice(null); void read();
    return () => { ++generation.current; };
  }, [transport]);

  async function apply(request: CompanionActionRequest) {
    if (inFlight.current) return;
    const current = ++generation.current;
    inFlight.current = true; setBusy(true); setError(null); setNotice(null); setPending(null);
    try {
      const result = await transport.companionAction(request);
      if (generation.current === current) {
        setData(result);
        setNotice(request.action === "agreements/propose" ? "已生成待确认约定，请核对下面的内容；确认后才生效。" : "已保存，后续对话会使用更新后的设置。");
      }
    } catch {
      if (generation.current === current) {
        setData(null);
        setError("操作未能确认，设置可能已更新或发生冲突。请重新读取核对，不会自动重发。");
      }
    } finally {
      if (generation.current === current) { inFlight.current = false; setBusy(false); }
    }
  }
  function toggle(control: "persona_growth" | "relationship_learning", value: boolean, description: string) {
    if (!data) return;
    setPending({ description, request: { action: "controls", payload: { control, value, expected_revision: control === "persona_growth" ? data.persona.revision : data.relationship.revision, confirmed: true } } });
  }
  function surface(control: "persona_surface" | "relationship_surface", value: SurfacePermission) {
    if (!data) return;
    setPending({ description: `将${control === "persona_surface" ? "Sora 谈及自身变化" : "Sora 谈及相处感受"}的方式改为“${{ silent: "不主动提起", implicit: "自然融入回应", explicit: "可以明确提起" }[value]}”。`,
      request: { action: "controls", payload: { control, value, expected_revision: control === "persona_surface" ? data.persona.revision : data.relationship.revision, confirmed: true } } });
  }
  const disabled = busy || pending !== null;
  return <section className="space-settings companion-settings" aria-label="陪伴与相处设置">
    <div className="settings-heading"><div><h2>陪伴与相处</h2><p>决定我们的互动如何影响 Sora，以及哪些变化可以在对话中提起。</p></div></div>
    <button className="button button--secondary" type="button" disabled={busy} onClick={() => void read()}>重新读取陪伴设置</button>
    {busy && <p role="status">正在核对真实陪伴状态…</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {pending && <section className="space-settings" role="group" aria-label="核对陪伴设置更改">
      <h3>核对这次更改</h3><p>{pending.description}</p>
      <div className="space-settings-tools"><button className="button" type="button" disabled={busy} onClick={() => void apply(pending.request)}>确认更改</button><button className="button button--secondary" type="button" disabled={busy} onClick={() => setPending(null)}>取消更改</button></div>
    </section>}
    {data && <>
      <div className="setting-row"><div><strong>暂停从我们的互动中成长</strong><small>暂停新增成长，不会撤销既有变化，也不会停止正常回应。</small></div><Switch label="暂停从我们的互动中成长" checked={data.persona.growth_paused} disabled={disabled} onChange={value => toggle("persona_growth", value, value ? "暂停把我们的互动用于新增成长。" : "恢复根据我们的互动逐步成长。")} /></div>
      {data.persona.global_growth_paused && <p>当前 Sora 的整体成长也处于暂停状态；恢复此处的设置后，整体暂停仍然有效。</p>}
      <div className="setting-row"><div><strong>暂停调整相处方式</strong><small>保留已经形成的相处历史，暂停新的关系学习。</small></div><Switch label="暂停调整相处方式" checked={data.relationship.learning_paused} disabled={disabled} onChange={value => toggle("relationship_learning", value, value ? "暂停根据新互动调整我们的相处方式。" : "恢复根据新互动调整我们的相处方式。")} /></div>
      {(["persona_surface", "relationship_surface"] as const).map(control => <label className="setting-row" key={control}><span>{control === "persona_surface" ? "谈及自身变化" : "谈及相处感受"}</span><select disabled={disabled} value={control === "persona_surface" ? data.persona.surface_permission : data.relationship.surface_permission} onChange={event => surface(control, event.target.value as SurfacePermission)}><option value="silent">不主动提起</option><option value="implicit">自然融入回应</option><option value="explicit">可以明确提起</option></select></label>)}
      <section className="space-settings"><h3>我们希望怎样相处</h3><p>当前约定：{data.active_agreement.user_confirmed ? data.active_agreement.comfort_rhythm ? rhythms[data.active_agreement.comfort_rhythm] : "已有明确确认的约定" : "自然相处，尚未确认新的约定"}。约定始终以你明确确认的内容为准。</p>
        <label className="setting-row"><span>陪伴节奏</span><select disabled={disabled} value={rhythm} onChange={event => setRhythm(event.target.value as ComfortRhythm)}>{Object.entries(rhythms).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <div className="space-settings-tools"><button className="button button--secondary" type="button" disabled={disabled} onClick={() => void apply({ action: "agreements/propose", payload: { rhythm } })}>拟定约定</button>{data.active_agreement.status === "active" && data.active_agreement.user_confirmed && <button className="button button--secondary" type="button" disabled={disabled} onClick={() => setPending({ description: "撤销当前生效的整份相处约定，后续回应恢复自然相处。其他对话偏好保留。", request: { action: "agreements/revoke", payload: { expected_revision: data.active_agreement.revision, confirmed: true } } })}>撤销当前约定</button>}</div>
        {data.agreements.map(item => <div className="space-settings" key={item.id}><p>{item.can_confirm && item.value ? `待确认：${rhythms[item.value as ComfortRhythm] ?? item.value}` : "有一份包含受保护内容的待确认约定，此处不能直接确认。"}</p>{item.expires_at && <p>有效至 {new Date(item.expires_at).toLocaleString("zh-CN")}</p>}{item.can_confirm && <button className="button" type="button" disabled={disabled} onClick={() => setPending({ description: `确认相处约定“${rhythms[item.value as ComfortRhythm] ?? item.value}”。`, request: { action: "agreements/confirm", payload: { candidate_id: item.id, expected_revision: item.revision, confirmed: true } } })}>核对并确认约定</button>}</div>)}
      </section>
      <section className="space-settings"><h3>不愉快与修复</h3>{data.repair ? <><p>当前：{repairs[data.repair.status] ?? "需要重新核对的修复状态"}。只有你明确确认，才会把修复标记为完成。</p><div className="space-settings-tools">{data.repair.allowed_transitions.filter(state => state in repairActions).map(state => <button className="button button--secondary" type="button" disabled={disabled} key={state} onClick={() => setPending({ description: `将本次沟通状态改为“${repairActions[state]}”。`, request: { action: "repair", payload: { repair_id: data.repair!.id, expected_status: data.repair!.status, to_state: state as Extract<CompanionActionRequest, { action: "repair" }>["payload"]["to_state"], confirmed: true } } })}>{repairActions[state]}</button>)}</div></> : <p>当前没有待处理的修复记录。后续对话产生记录后，可在这里查看并决定下一步。</p>}</section>
    </>}
  </section>;
}
