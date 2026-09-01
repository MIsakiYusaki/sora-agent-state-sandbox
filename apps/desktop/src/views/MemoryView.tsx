import { Archive, Brain, ChevronRight, Eye, EyeOff, FileDown, Filter, ShieldCheck, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { UserMemoryPage, UserMemoryView } from "../transport";
import { IconButton, Metric, PageHeader, Tag } from "../components/ui";

const sensitivityLabels: Record<string, string> = { low: "低", medium: "中", high: "高" };
const lifecycleLabels: Record<string, string> = { hot: "热", warm: "温", cold: "冷", archived: "归档" };

function memoryKind(type: string): string {
  const labels: Record<string, string> = {
    preference: "长期偏好",
    personal_fact: "人物与事实",
    shared_experience: "共同经历",
    episode: "共同经历",
    gradual_preference: "渐变偏好",
  };
  return labels[type] ?? "记忆";
}

function lifecycle(memory: UserMemoryView): string {
  const tier = String(memory.lifecycle["tier"] ?? "");
  return lifecycleLabels[tier] ?? "可用";
}

export function MemoryView({
  page,
  loading,
  error,
  onVisibility,
  onForget,
}: {
  page: UserMemoryPage | null;
  loading: boolean;
  error: string | null;
  onVisibility: (memoryId: string, visible: boolean) => Promise<void>;
  onForget: (memoryId: string) => Promise<void>;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [forgetTarget, setForgetTarget] = useState<UserMemoryView | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const items = page?.items ?? [];
  const filtered = useMemo(
    () => items.filter((item) => `${item.summary}${item.tags.join(" ")}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())),
    [items, query],
  );
  const selected = items.find((item) => item.id === selectedId) ?? filtered[0] ?? null;

  const changeVisibility = async (memory: UserMemoryView) => {
    setBusy(memory.id);
    try {
      await onVisibility(memory.id, !memory.visible);
    } finally {
      setBusy(null);
    }
  };

  const confirmForget = async () => {
    if (!forgetTarget) return;
    setBusy(forgetTarget.id);
    try {
      await onForget(forgetTarget.id);
      setSelectedId(null);
      setForgetTarget(null);
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="page page--memory">
      <PageHeader
        eyebrow="Memory & Privacy"
        title="记忆与隐私"
        description="查看 Sora 实际保存的用户记忆，决定它能否出现在对话里，或发起不可逆的忘记请求。"
        actions={<><button className="button button--secondary" type="button" disabled title="安全导出接口尚未接入"><FileDown size={16} /> 导出</button><button className="button button--danger-quiet" type="button" disabled title="批量删除尚未接入"><Trash2 size={16} /> 管理删除</button></>}
      />
      <section className="metric-band">
        <Metric label="可用记忆" value={String(page?.summary.available ?? 0)} note="真实主存储" />
        <Metric label="可以影响表达" value={String(page?.summary.visible ?? 0)} note="可随时隐藏" />
        <Metric label="已隐藏" value={String(page?.summary.hidden ?? 0)} note="不会进入对话" />
        <Metric label="已归档" value={String(page?.summary.archived ?? 0)} note="仅深度召回" />
      </section>
      <div className="privacy-notice"><ShieldCheck size={17} /><span><strong>只显示经过治理的摘要。</strong>原始证据、内部评分和私密原文不会在普通界面展开。</span></div>
      <div className="toolbar-band">
        <label className="search-field search-field--wide"><Brain size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索记忆" placeholder="搜索记忆摘要或标签" /></label>
        <button className="button button--secondary" type="button" disabled><Filter size={16} /> 类型</button>
        <button className="button button--secondary" type="button" disabled><Archive size={16} /> 生命周期</button>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <section className={`memory-layout ${selected ? "has-detail" : ""}`}>
        <div className="memory-list">
          {loading && <div className="prototype-loading">正在读取本机记忆…</div>}
          {!loading && filtered.length === 0 && <div className="prototype-empty">目前没有可显示的记忆。</div>}
          {filtered.map((memory) => (
            <button className={`memory-row ${selected?.id === memory.id ? "is-active" : ""}`} type="button" key={memory.id} onClick={() => setSelectedId(memory.id)}>
              <span className="memory-kind memory-kind--blue"><Brain size={16} /></span>
              <span className="memory-row__content">
                <span><Tag tone="neutral">{memoryKind(memory.type)}</Tag><Tag tone={lifecycle(memory) === "热" ? "mint" : "neutral"}>{lifecycle(memory)}</Tag>{!memory.visible && <Tag tone="amber"><EyeOff size={11} /> 已隐藏</Tag>}</span>
                <strong>{memory.summary.slice(0, 48)}</strong>
                <small>{memory.summary}</small>
                <em>{memory.updated_at ? new Date(memory.updated_at).toLocaleString("zh-CN") : "更新时间未知"} · {memory.evidence_count} 条依据</em>
              </span>
              <ChevronRight size={17} />
            </button>
          ))}
        </div>
        {selected && (
          <aside className="memory-detail">
            <header><div><Tag tone="blue">{memoryKind(selected.type)}</Tag><h2>{selected.summary.slice(0, 48)}</h2></div><IconButton label="关闭详情" onClick={() => setSelectedId(null)}><X size={17} /></IconButton></header>
            <section><h3>保存的内容</h3><p>{selected.summary}</p></section>
            <section><h3>为什么会保留</h3><dl><div><dt>依据</dt><dd>{selected.evidence_count} 条经过治理的证据</dd></div><div><dt>可信度</dt><dd>{Math.round(selected.confidence * 100)}%</dd></div><div><dt>敏感级别</dt><dd>{sensitivityLabels[selected.sensitivity] ?? selected.sensitivity}</dd></div><div><dt>生命周期</dt><dd>{lifecycle(selected)}</dd></div></dl></section>
            <section><h3>在对话里</h3><p>{selected.visible ? "当前允许按治理规则自然影响回应；是否明确提起仍受隐私与同意规则限制。" : "当前已隐藏，不会进入普通对话上下文。"}</p></section>
            <footer>
              <button className="button button--secondary" type="button" disabled={busy === selected.id} onClick={() => void changeVisibility(selected)}>{selected.visible ? <EyeOff size={16} /> : <Eye size={16} />}{selected.visible ? "隐藏" : "恢复使用"}</button>
              <button className="button button--danger-quiet" type="button" disabled={busy === selected.id} onClick={() => setForgetTarget(selected)}><Trash2 size={16} /> 忘记</button>
            </footer>
          </aside>
        )}
      </section>
      {forgetTarget && (
        <div className="modal-backdrop" onMouseDown={() => setForgetTarget(null)}>
          <section className="modal modal--danger" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span className="modal-icon modal-icon--danger"><Trash2 size={18} /></span><div><h2>忘记这条记忆？</h2><p>会向真实记忆系统发起删除与投影传播。</p></div></div><IconButton label="关闭" onClick={() => setForgetTarget(null)}><X size={17} /></IconButton></header>
            <div className="confirmation-memory"><p>{forgetTarget.summary}</p></div>
            <footer><button className="button button--secondary" type="button" onClick={() => setForgetTarget(null)}>取消</button><button className="button button--danger" type="button" disabled={busy === forgetTarget.id} onClick={() => void confirmForget()}>确认忘记</button></footer>
          </section>
        </div>
      )}
    </main>
  );
}
