import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { NotificationPreferences, NotificationSettingsView } from "../transport/DesktopTransport";
const timeText = (minutes: number) => `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
const timeValue = (text: string) => /^\d\d:\d\d$/.test(text) ? Number(text.slice(0,2)) * 60 + Number(text.slice(3)) : -1;

export function NotificationSettings({ transport, profileId }: { transport: DesktopTransport; profileId: string | null }) {
  const [saved, setSaved] = useState<NotificationSettingsView | null>(null);
  const [draft, setDraft] = useState<NotificationPreferences | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  const sequence=useRef(0), mounted=useRef(false), pending=useRef(false);
  const current=(id: number)=>mounted.current && sequence.current===id;
  const begin=()=>{if(pending.current)return null; pending.current=true;setBusy(true);setError(null);setNotice(null);return ++sequence.current;};
  const finish=(id:number)=>{if(current(id)){pending.current=false;setBusy(false);}};
  const valid=(value:NotificationSettingsView)=>Number.isInteger(value.preferences.quiet_start) && Number.isInteger(value.preferences.quiet_end) && value.preferences.quiet_start>=0 && value.preferences.quiet_end>=0 && value.preferences.quiet_start<1440 && value.preferences.quiet_end<1440;
  const read=async()=>{
    const id=begin();if(id===null)return;setSaved(null);setDraft(null);
    try {const next=await transport.getNotificationSettings();if(!current(id))return;if(!valid(next))throw new Error("invalid settings");setSaved(next);setDraft(next.preferences);}
    catch {if(current(id))setError("通知偏好或系统状态读取失败，当前状态未确认，请重新读取。");}
    finally {finish(id);}
  };
  useEffect(()=>{mounted.current=true;pending.current=false;setBusy(false);setSaved(null);setDraft(null);setError(null);setNotice(null);if(profileId)void read();return()=>{mounted.current=false;++sequence.current;pending.current=false;};},[transport,profileId]);
  const save=async()=>{
    if(!saved||!draft)return;const id=begin();if(id===null)return;
    try {const next=await transport.saveNotificationSettings(draft,saved.revision);if(!current(id))return;if(!valid(next))throw new Error("invalid settings");setSaved(next);setDraft(next.preferences);setNotice("通知偏好已保存；是否显示通知仍由 Windows 权限和专注设置决定。");}
    catch {if(current(id)){setSaved(null);setDraft(null);setError("保存结果未获得完整确认，请重新读取实际偏好，不会自动重复保存。");}}
    finally {finish(id);}
  };
  const test=async()=>{
    const id=begin();if(id===null)return;
    try {const result=await transport.testNotification();if(!current(id))return;setNotice(result.status==="submitted_to_windows"?"Windows 已接收测试通知请求；是否弹出由系统设置决定。":result.status==="quiet"?"当前处于安静时段，测试通知没有发送。":"通知偏好处于关闭状态，没有发送测试通知。");}
    catch {if(current(id))setError("Windows 未确认接收测试通知。请核对系统通知设置后重试。");}
    finally {finish(id);}
  };
  const changed = !!saved && !!draft && JSON.stringify(saved.preferences)!==JSON.stringify(draft);
  return <section className="space-settings" aria-label="Windows 通知偏好"><div className="settings-heading"><div><h2>通知</h2><p>在客户端不在前台时，回应完整保存后发送 Windows 提示。</p></div></div><p>提示仅显示“Sora 已回应”，不包含对话正文。通知偏好按独立空间保存，关闭客户端后不会继续运行提醒服务。</p>
    <button className="button button--secondary" type="button" disabled={busy||!profileId} onClick={()=>void read()}>重新读取通知设置</button>
    {busy&&<p role="status">正在核对通知设置…</p>}{error&&<p className="inline-error" role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {saved&&draft&&<><p>Windows 通知状态：{saved.os_permission==="enabled"?"允许接收；弹出仍受系统专注设置影响":saved.os_permission==="disabled"?"系统已禁用，请在 Windows 通知设置中开启后重新读取":"尚未确认。保存偏好后可发送测试通知，再重新读取状态。"}</p><fieldset disabled={busy}><legend>回应完成提醒</legend><label className="space-confirm"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>允许在后台完成回应时提示我</label><label className="space-confirm"><input type="checkbox" checked={draft.quiet_enabled} onChange={e=>setDraft({...draft,quiet_enabled:e.target.checked})}/>启用安静时段（按此电脑本地时间）</label>
      <label>开始时间<input type="time" aria-label="安静时段开始" value={timeText(draft.quiet_start)} onChange={e=>{const v=timeValue(e.target.value);if(v>=0&&v<1440)setDraft({...draft,quiet_start:v});}}/></label><label>结束时间<input type="time" aria-label="安静时段结束" value={timeText(draft.quiet_end)} onChange={e=>{const v=timeValue(e.target.value);if(v>=0&&v<1440)setDraft({...draft,quiet_end:v});}}/></label>
      {draft.quiet_enabled&&<p>{draft.quiet_start===draft.quiet_end?"起止时间相同：全天保持安静。":draft.quiet_start>draft.quiet_end?"安静时段跨过午夜，在次日结束。":"安静时段在当天结束。"}</p>}<div className="space-settings-tools"><button className="button" type="button" disabled={!changed} onClick={()=>void save()}>保存通知偏好</button><button className="button button--secondary" type="button" disabled={changed||!saved.preferences.enabled||saved.os_permission==="disabled"} onClick={()=>void test()}>发送测试通知</button></div><p>{changed?"修改尚未保存，后台仍使用上次确认的偏好。":"当前显示已读取的偏好。"}测试通知也会遵守安静时段。</p>
    </fieldset></>}
  </section>;
}
