import { useRef, useState } from "react";
import type { Workload, WorkloadBinding, WorkloadTargetInput } from "../contracts/generated";
import type { DesktopTransport, ProviderConfigurationView } from "../transport";

const workloads: readonly Workload[] = ["turn_interpretation", "semantic_arbitration", "response_generation", "interruptibility_resolution", "memory_extraction", "memory_recall_planning", "memory_surface", "memory_consolidation", "persona_candidate", "affect_appraisal", "relationship_evidence", "worldops_interpretation", "domain_background", "embedding_documents", "embedding_query", "memory_rerank"];
const targetKey = (target: WorkloadTargetInput) => JSON.stringify([target.provider_profile_id, target.model_id]);

export function ProviderRoutesEditor({ transport, configuration, onSaved }: { transport: DesktopTransport; configuration: ProviderConfigurationView; onSaved: () => Promise<void> }) {
  const [workload, setWorkload] = useState<Workload>("response_generation");
  const [editing, setEditing] = useState(false);
  const [primary, setPrimary] = useState("");
  const [fallbacks, setFallbacks] = useState<string[]>(["", "", ""]);
  const [crossProvider, setCrossProvider] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const bindings = configuration.bindings as unknown as readonly WorkloadBinding[];
  const binding = bindings.find(item => item.workload === workload);
  const targets = configuration.profiles.filter(profile => profile.status === "verified").flatMap(profile => (configuration.models[profile.profile_id] ?? []).map(model => ({ target: { provider_profile_id: profile.profile_id, model_id: model.model_id }, label: `${profile.display_name} / ${model.model_id}` })));
  const readTarget = (key: string) => targets.find(item => targetKey(item.target) === key)?.target;
  const choose = (value: Workload) => {
    const current = bindings.find(item => item.workload === value);
    setWorkload(value); setPrimary(current ? targetKey(current.primary) : "");
    setFallbacks(current?.fallbacks?.length ? current.fallbacks.map(targetKey) : [""]);
    setRevision(current?.revision ?? null); setCrossProvider(current?.cross_provider_fallback ?? false);
    setEditing(true); setConfirmed(false); setError(null); setNotice(null);
  };
  const save = async () => {
    if (saving.current || busy || !confirmed) return;
    const target = readTarget(primary);
    if (!target) { setError("请先选择已验证的主模型。"); return; }
    const selected = fallbacks.filter(Boolean).map(readTarget);
    if (selected.some(item => !item) || new Set([primary, ...fallbacks.filter(Boolean)]).size !== selected.length + 1) { setError("主模型与降级模型不能重复，且必须仍然可用。"); return; }
    saving.current = true; setBusy(true); setError(null); setNotice(null);
    try {
      await transport.bindModelWorkload({ workload, primary: target, fallbacks: selected as WorkloadTargetInput[], requirements: binding?.requirements ?? [], cross_provider_fallback: crossProvider, expected_revision: revision });
      await onSaved(); setEditing(false); setConfirmed(false); setNotice("路由已更新并重新读取。下一次相应用途的调用使用新绑定。");
    } catch { setConfirmed(false); setError("路由未获得完整确认，可能存在配置冲突或能力不匹配。请重新读取并检查当前绑定，不会自动重试。"); }
    finally { saving.current = false; setBusy(false); }
  };
  return <section className="provider-routes-editor" aria-label="实际模型路由设置">
    <h3>实际使用的模型</h3><p>连接或验证模型不会替换已有绑定。请逐项选择主模型与降级顺序；能力要求由服务端验证。</p>
    <label>模型用途<select aria-label="模型用途路由" value={workload} disabled={busy} onChange={event => choose(event.target.value as Workload)}>{workloads.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
    <p>当前主模型：{binding ? `${binding.primary.provider_profile_id} / ${binding.primary.model_id}` : "未绑定"}</p>
    <p>当前降级顺序：{binding?.fallbacks?.map(item => item.model_id).join(" → ") || "无"}</p>
    {!editing ? <button className="button button--secondary" type="button" onClick={() => choose(workload)}>修改实际路由</button> : <fieldset disabled={busy}>
      <label>主模型<select aria-label="路由主模型" value={primary} onChange={event => { setPrimary(event.target.value); setConfirmed(false); }}><option value="">选择已验证模型</option>{targets.map(item => <option key={targetKey(item.target)} value={targetKey(item.target)}>{item.label}</option>)}</select></label>
      {fallbacks.map((value, index) => <label key={index}>降级模型 {index + 1}<select aria-label={`降级模型 ${index + 1}`} value={value} onChange={event => { setFallbacks(current => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item)); setConfirmed(false); }}><option value="">不用</option>{targets.map(item => <option key={targetKey(item.target)} value={targetKey(item.target)}>{item.label}</option>)}</select></label>)}
      <button className="button button--secondary" type="button" onClick={() => { setFallbacks(current => [...current, ""]); setConfirmed(false); }}>添加降级模型</button>
      <label><input type="checkbox" checked={crossProvider} onChange={event => { setCrossProvider(event.target.checked); setConfirmed(false); }} />允许按以上顺序跨供应商降级</label>
      <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我已核对模型用途、主模型和降级顺序</label>
      <button className="button" type="button" disabled={!confirmed || !primary} onClick={() => void save()}>{busy ? "保存中" : "确认更新路由"}</button>
      <button className="button button--secondary" type="button" onClick={() => setEditing(false)}>取消</button>
    </fieldset>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <button className="text-link" type="button" disabled={busy} onClick={() => { setBusy(true); setConfirmed(false); void onSaved().then(() => { setEditing(false); setError(null); setNotice("已重新读取实际路由，没有重发上次修改。"); }).catch(() => setError("实际配置读取失败，请检查连接后重试。")).finally(() => setBusy(false)); }}>重新读取路由</button>
  </section>;
}
