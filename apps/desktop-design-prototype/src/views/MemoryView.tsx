import { Archive, Brain, ChevronRight, Eye, EyeOff, FileDown, Filter, LockKeyhole, Search, ShieldCheck, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { memories, type MemoryItem } from "../data";
import { IconButton, Metric, PageHeader, Tag } from "../components/ui";

export function MemoryView() {
  const [selected, setSelected] = useState<MemoryItem | null>(memories[0]);
  const [query, setQuery] = useState("");
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(new Set());
  const [forgetTarget, setForgetTarget] = useState<MemoryItem | null>(null);
  const filtered = useMemo(() => memories.filter((item) => `${item.title}${item.gist}`.includes(query)), [query]);

  return (
    <main className="page page--memory">
      <PageHeader
        eyebrow="Memory & Privacy"
        title="记忆与隐私"
        description="查看 Sora 保存了什么、为什么保存，以及它是否可以出现在对话里。"
        actions={<><button className="button button--secondary" type="button"><FileDown size={16} /> 导出</button><button className="button button--danger-quiet" type="button"><Trash2 size={16} /> 管理删除</button></>}
      />
      <section className="metric-band">
        <Metric label="可用记忆" value="128" note="过去 30 天 +14" />
        <Metric label="正在影响表达" value="23" note="均可解释" />
        <Metric label="待确认" value="3" note="不会自动使用" />
        <Metric label="已归档" value="41" note="仅深度召回" />
      </section>
      <div className="privacy-notice"><ShieldCheck size={17} /><span><strong>原始证据默认不注入模型。</strong>普通对话只使用经过治理的 gist；高敏内容需要单独许可。</span><button type="button">查看隐私规则</button></div>
      <div className="toolbar-band">
        <label className="search-field search-field--wide"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索记忆" placeholder="搜索记忆、人物或共同经历" /></label>
        <button className="button button--secondary" type="button"><Filter size={16} /> 类型</button>
        <button className="button button--secondary" type="button"><Archive size={16} /> 生命周期</button>
      </div>
      <section className={`memory-layout ${selected ? "has-detail" : ""}`}>
        <div className="memory-list">
          {filtered.map((memory) => {
            const isHidden = hiddenIds.has(memory.id);
            return (
              <button className={`memory-row ${selected?.id === memory.id ? "is-active" : ""}`} type="button" key={memory.id} onClick={() => setSelected(memory)}>
                <span className={`memory-kind memory-kind--${memory.kind === "共同经历" ? "violet" : memory.kind === "长期偏好" ? "blue" : "mint"}`}><Brain size={16} /></span>
                <span className="memory-row__content"><span><Tag tone="neutral">{memory.kind}</Tag><Tag tone={memory.lifecycle === "热" ? "mint" : "neutral"}>{memory.lifecycle}</Tag>{isHidden && <Tag tone="amber"><EyeOff size={11} /> 已隐藏</Tag>}</span><strong>{memory.title}</strong><small>{memory.gist}</small><em>{memory.updated} · {memory.source}</em></span>
                <ChevronRight size={17} />
              </button>
            );
          })}
        </div>
        {selected && (
          <aside className="memory-detail">
            <header><div><Tag tone="blue">{selected.kind}</Tag><h2>{selected.title}</h2></div><IconButton label="关闭详情" onClick={() => setSelected(null)}><X size={17} /></IconButton></header>
            <section><h3>保存的内容</h3><p>{selected.gist}</p></section>
            <section><h3>为什么保存</h3><dl className="detail-kv"><div><dt>证据</dt><dd>{selected.source}</dd></div><div><dt>置信度</dt><dd>{Math.round(selected.confidence * 100)}%</dd></div><div><dt>敏感度</dt><dd><LockKeyhole size={13} /> {selected.sensitivity}</dd></div><div><dt>最后更新</dt><dd>{selected.updated}</dd></div></dl></section>
            <section><h3>如何使用</h3><p>{selected.surfaced ? "允许隐式影响回应，但不会主动逐字引用。" : "仅用于内部关联，当前不允许在对话中显式呈现。"}</p><button className="text-link" type="button">查看最近一次使用记录</button></section>
            <footer>
              <button className="button button--secondary" type="button" onClick={() => setHiddenIds((current) => { const next = new Set(current); next.has(selected.id) ? next.delete(selected.id) : next.add(selected.id); return next; })}>{hiddenIds.has(selected.id) ? <Eye size={16} /> : <EyeOff size={16} />}{hiddenIds.has(selected.id) ? "恢复呈现" : "隐藏"}</button>
              <button className="button button--danger-quiet" type="button" onClick={() => setForgetTarget(selected)}><Trash2 size={16} /> 忘记</button>
            </footer>
          </aside>
        )}
      </section>
      {forgetTarget && <ForgetDialog memory={forgetTarget} onClose={() => setForgetTarget(null)} />}
    </main>
  );
}

function ForgetDialog({ memory, onClose }: { memory: MemoryItem; onClose: () => void }) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section className="modal modal--danger" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="modal-icon modal-icon--danger"><Trash2 size={18} /></span><div><h2>忘记这条记忆？</h2><p>此操作会同步移除索引和派生投影。</p></div></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header>
        <div className="confirmation-memory"><strong>{memory.title}</strong><p>{memory.gist}</p></div>
        <label className="confirmation-check"><input type="checkbox" /><span>同时阻止系统从相同内容中重新学习</span></label>
        <footer><button className="button button--secondary" type="button" onClick={onClose}>取消</button><button className="button button--danger" type="button" onClick={onClose}>确认忘记</button></footer>
      </section>
    </div>
  );
}
