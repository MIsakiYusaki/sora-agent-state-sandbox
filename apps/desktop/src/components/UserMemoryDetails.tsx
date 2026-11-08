import { useEffect, useState } from "react";
import type { DesktopTransport, UserMemoryView } from "../transport";
import type { UserMemoryDetail } from "../transport/DesktopTransport";

export function UserMemoryDetails({ memory, transport, onCorrected, onOpenConversation }: { memory: UserMemoryView; transport: DesktopTransport; onCorrected: (memory: UserMemoryView) => Promise<void>; onOpenConversation?: (id: string) => void }) {
  const [detail, setDetail] = useState<UserMemoryDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [editing, setEditing] = useState(false);
  const [summary, setSummary] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let current = true;
    setDetail(null); setLoading(true); setError(null); setEditing(false); setConfirmed(false);
    void transport.getMemoryDetail(memory.id).then(value => {
      if (value.memory.id !== memory.id || value.schema_version !== "UserMemoryDetail.v1") throw new Error("scope mismatch");
      if (current) setDetail(value);
    }).catch(() => { if (current) setError("实际来源读取失败，不能用通用说明替代。请重新读取。"); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [transport, memory.id, epoch]);
  const save = async () => {
    if (!confirmed || !detail || saving || !summary.trim()) return;
    setSaving(true); setConfirmed(false); setError(null);
    try {
      const result = await transport.correctMemory(memory.id, { summary: summary.trim(), expected_revision: detail.revision, confirmed: true });
      setEditing(false); setDetail(null);
      await onCorrected(result.memory);
    } catch {
      setDetail(null); setEditing(false);
      setError("纠错未获得完整确认，记忆可能已经变化。请重新读取核对，不会自动重发。");
    } finally { setSaving(false); }
  };
  return <section className="user-memory-details" aria-label="实际记忆来源与纠错">
    <h3>实际来源</h3>
    {loading && <p role="status">正在读取这条记忆关联的来源…</p>}
    {error && <p role="status">{error}</p>}
    <button className="text-link" type="button" disabled={loading || saving} onClick={() => setEpoch(value => value + 1)}>重新读取来源</button>
    {detail && <><p>以下来自这条记忆实际关联的证据元数据，不展示原始证据正文。</p><ul>{detail.sources.map((source, index) => <li key={index}>{source.kind === "manual" ? "手动记下或纠正" : source.kind === "conversation" ? "对话记录" : "其他已登记来源"}{source.created_at ? ` · ${new Date(source.created_at).toLocaleDateString("zh-CN")}` : " · 时间未报告"}{source.conversation_id && onOpenConversation && <button className="text-link" type="button" onClick={() => onOpenConversation(source.conversation_id!)}>查看原对话</button>}</li>)}</ul>{detail.sources.length === 0 && <p>这条记忆当前没有可读取的有效来源。</p>}
      {detail.can_correct ? <button className="button button--secondary" type="button" disabled={saving} onClick={() => { setSummary(detail.memory.summary); setEditing(true); setConfirmed(false); }}>纠正记下的内容</button> : <p>这条记忆已隐藏。当前纠错兼容路径会创建新版本，为避免无意恢复使用，这里不会修改隐藏记录；隐藏与忘记仍可使用。</p>}
    </>}
    {editing && detail && <fieldset disabled={saving}><legend>纠正摘要</legend><p>会生成可追溯的新版本，保留旧版本并登记本次纠错来源。不要填写密钥或密码。</p><label>正确的内容<textarea aria-label="正确的记忆摘要" value={summary} maxLength={4000} onChange={event => { setSummary(event.target.value); setConfirmed(false); }} /></label><label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认这段内容正确，并同意更新长期记忆</label><div><button className="button" type="button" disabled={!confirmed || !summary.trim()} onClick={() => void save()}>确认纠错</button><button className="button button--secondary" type="button" onClick={() => setEditing(false)}>取消</button></div></fieldset>}
  </section>;
}
