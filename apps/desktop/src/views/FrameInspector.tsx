import { useEffect, useId, useRef, useState } from "react";
import type { DesktopDebugFrameDetail, DesktopDebugFrameRequest, FrameFieldView } from "../contracts/generated";
import type { DesktopTransport } from "../transport";

const frameIds: DesktopDebugFrameRequest["frame_id"][] = ["turn_frame", "turn_interpretation", "context_use_policy", "interruptibility_frame", "dialogue_control_frame", "recollection_frame", "persona_frame", "affect_frame", "relationship_frame", "worldops_frame", "origin_frame", "session_frame", "arbitration_result"];
const sources = { generation_sealed: "生成前已封印的实际输入", delivery_snapshot: "历史交付打包快照（不保证等同模型输入）", control_snapshot: "历史控制快照（不保证完整生成输入）", unobserved: "未观测到本帧" };

export function FrameInspector({ transport, conversationId, profileHash, turnId, correlationId, frameId, previousTurnId }: {
  transport: DesktopTransport; conversationId: string; profileHash: string; turnId: string | null;
  correlationId: string | null; frameId: string | null; previousTurnId: string | null;
}) {
  const [loadedDetail, setDetail] = useState<DesktopDebugFrameDetail | null>(null);
  const [previous, setPrevious] = useState<DesktopDebugFrameDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [compare, setCompare] = useState(false);
  const [query, setQuery] = useState("");
  const [showMissing, setShowMissing] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [samplingBusy, setSamplingBusy] = useState(false);
  const [samplingError, setSamplingError] = useState(false);
  const [observation, setObservation] = useState<DesktopDebugFrameDetail["observation"] | null>(null);
  const [clock, setClock] = useState(Date.now());
  const id = useId();
  const [observationEpoch, setObservationEpoch] = useState(0);
  const observationVersion = useRef(0);
  const samplingPending = useRef(false);
  useEffect(() => {
    let current = true;
    const version = ++observationVersion.current;
    samplingPending.current = false; setSamplingBusy(false);
    setObservation(null);
    void transport.getFrameObservation().then(value => { if (current && version === observationVersion.current) { setObservation(value); setClock(Date.now()); } }).catch(() => undefined);
    return () => { current = false; ++observationVersion.current; };
  }, [transport, profileHash, observationEpoch]);
  useEffect(() => {
    let current = true;
    setDetail(null); setPrevious(null); setError(false);
    if (!turnId || !frameId || !frameIds.includes(frameId as DesktopDebugFrameRequest["frame_id"])) { setLoading(false); return; }
    setLoading(true);
    const observationAtRead = observationVersion.current;
    const request = { conversation_id: conversationId, turn_id: turnId, frame_id: frameId as DesktopDebugFrameRequest["frame_id"] };
    const verify = (value: DesktopDebugFrameDetail, requestedTurn: string) => {
      if (value.schema_version !== "DesktopDebugFrameDetail.v1" || value.conversation_id !== conversationId || value.turn_id !== requestedTurn || value.profile_id_hash !== profileHash || value.projection.frame_id !== frameId || !(value.projection.source in sources)) throw new Error("Frame scope mismatch");
      if (requestedTurn === turnId && correlationId && value.correlation_id !== correlationId) throw new Error("Correlation mismatch");
      return value;
    };
    void (async () => {
      try {
        const result = verify(await transport.getDebugFrame(request), turnId);
        if (!current) return;
        setDetail(result);
        if (observationAtRead === observationVersion.current) setObservation(result.observation);
        if (compare && previousTurnId) {
          const other = verify(await transport.getDebugFrame({ ...request, turn_id: previousTurnId }), previousTurnId);
          if (current) setPrevious(other);
        }
      } catch { if (current) setError(true); }
      finally { if (current) setLoading(false); }
    })();
    return () => { current = false; };
  }, [transport, conversationId, profileHash, turnId, correlationId, frameId, previousTurnId, compare, epoch]);

  const changeObservation = async (enabled: boolean) => {
    if (samplingPending.current) return;
    samplingPending.current = true;
    const version = ++observationVersion.current;
    setSamplingBusy(true); setSamplingError(false);
    try {
      const result = await transport.setFrameObservation(enabled);
      if (version === observationVersion.current) { setObservation(result); setClock(Date.now()); }
    } catch {
      if (version === observationVersion.current) { setObservation(null); setSamplingError(true); }
    } finally {
      if (version === observationVersion.current) { samplingPending.current = false; setSamplingBusy(false); }
    }
  };

  const detail = loadedDetail?.turn_id === turnId && loadedDetail?.conversation_id === conversationId && loadedDetail?.profile_id_hash === profileHash && loadedDetail?.projection.frame_id === frameId ? loadedDetail : null;
  const samplingActive = observation?.active === true && observation.expires_at_ms !== null && observation.expires_at_ms > clock;
  useEffect(() => {
    if (!observation?.active || observation.expires_at_ms === null || observation.expires_at_ms <= Date.now()) return;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.max(0, observation.expires_at_ms - Date.now()));
    return () => window.clearTimeout(timer);
  }, [observation, clock]);
  const fields = (detail?.projection.fields ?? []).filter(field => (showMissing || field.state !== "missing") && field.path.toLowerCase().includes(query.toLowerCase()));
  return <section className="frame-values" aria-label="本轮脱敏帧字段" aria-busy={loading}>
    <header><h2>本轮实际字段</h2><button className="dev-button" type="button" disabled={loading || !turnId || !frameId} onClick={() => setEpoch(value => value + 1)}>重新读取字段</button></header>
    <p>只显示版本化白名单中的状态、band、预算和来源指纹。原始证据、自由文本与隐藏推理不在此接口中。</p>
    <div className="frame-sampling"><button className="dev-button" type="button" disabled={samplingBusy || loading} onClick={() => void changeObservation(!samplingActive)}>{samplingActive ? "停止新轮次帧采集" : "开启新轮次帧采集（5 分钟）"}</button>
      {!observation && <><p>采集状态尚未确认；不能据此认定已经停止。</p><button className="dev-button" type="button" disabled={samplingBusy} onClick={() => setObservationEpoch(value => value + 1)}>重新读取采集状态</button><button className="dev-button" type="button" disabled={samplingBusy} onClick={() => {
        void changeObservation(false);
      }}>停止帧采集（状态未确认）</button></>}
      <p>采集默认关闭。开启后返回对话发送消息，生成前值才会保存；到期或关闭开发者模式后停止。</p>
      {samplingError && <p role="alert">采集状态变更未获确认，请重新读取或重试；不视为已开启或已停止。</p>}
    </div>
    <div className="frame-value-tools"><label htmlFor={`${id}-query`}>筛选字段</label><input id={`${id}-query`} value={query} onChange={event => setQuery(event.target.value)} placeholder="例如：budgets、trait_bands" />
      <label><input type="checkbox" checked={compare} disabled={!previousTurnId} onChange={event => setCompare(event.target.checked)} />对照上一条已保存轮次</label>
      <label><input type="checkbox" checked={showMissing} onChange={event => setShowMissing(event.target.checked)} />显示来源未包含的字段</label></div>
    {loading && <p role="status">正在读取本轮字段，不使用上一轮缓存补齐。</p>}
    {error && <p role="alert">帧字段读取失败或身份不匹配。未确认的数据不显示；可以重新读取。</p>}
    {!turnId && <p>当前没有可检查的真实轮次。</p>}
    {detail && !error && <>
      <p>{sources[detail.projection.source]}</p>
      <details className="frame-provenance"><summary>来源、版本与投影修订</summary><dl className="frame-value-source"><div><dt>数据来源</dt><dd>{sources[detail.projection.source]}</dd></div><div><dt>领域所有者</dt><dd>{detail.projection.owner}</dd></div><div><dt>帧版本</dt><dd>{detail.projection.frame_schema_version ?? "未声明或未允许的版本"}</dd></div><div><dt>未披露顶层字段</dt><dd>{detail.projection.protected_field_count}</dd></div>
        <div><dt>投影修订（非完整帧校验和）</dt><dd>{detail.projection.projection_revision}</dd></div><div><dt>关联轮次</dt><dd>{detail.turn_id}</dd></div></dl></details>
      {compare && previous && <p>比较的是允许披露的字段，不代表完整帧一致。{previous.projection.source !== detail.projection.source ? "两条记录来源阶段不同，请勿当作同阶段版本变化。" : "两条记录来自相同采集阶段。"}</p>}
      <div className="frame-value-table"><table><caption>脱敏字段值{compare ? "与前一记录对照" : ""}</caption><thead><tr><th scope="col">字段</th><th scope="col">本轮值</th><th scope="col">观测状态</th>{compare && <th scope="col">前一记录</th>}</tr></thead><tbody>{fields.map(field => {
        const old = previous?.projection.fields.find(item => item.path === field.path);
        const changed = !!old && JSON.stringify([old.state, old.value]) !== JSON.stringify([field.state, field.value]);
        return <tr key={field.path}><th scope="row"><code>{field.path}</code>{field.kind === "count" && <small>集合数量，非原文</small>}{field.kind === "reference_hash" && <small>来源指纹，非原始标识</small>}</th><td>{fieldValue(field)}</td><td>{fieldState(field)}{changed && <span className="frame-value-changed">有变化</span>}</td>{compare && <td>{old ? fieldValue(old) : "未读取"}</td>}</tr>;
      })}</tbody></table></div>
      {fields.length === 0 && <p>{query ? "没有匹配的已声明字段。" : "本帧没有已观测的白名单值。可以显示缺失字段检查覆盖，不会补成零。"}</p>}
      <p>该来源的 token 估计：{detail.budget?.token_estimate ?? "未观测"}；回应预留：{detail.budget?.reply_budget_tokens ?? "未观测"}。</p>
    </>}
  </section>;
}

function fieldValue(field: FrameFieldView): string {
  if (field.state !== "observed") return "未披露";
  if (field.value === null) return "已报告空值 (null)";
  if (field.value === undefined) return "未观测";
  if (typeof field.value === "boolean") return field.value ? "是" : "否";
  return String(field.value);
}
function fieldState(field: FrameFieldView): string {
  return { observed: "已观测", missing: "来源未包含该字段", protected: "受保护", invalid: "类型或枚举未通过" }[field.state];
}
