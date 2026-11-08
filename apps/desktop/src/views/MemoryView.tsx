import { Brain, ChevronRight, Eye, EyeOff, Search, ShieldCheck, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { DesktopTransport, UserMemoryPage, UserMemoryView } from "../transport";
import { UserMemoryDetails } from "../components/UserMemoryDetails";
import { IconButton, PageHeader } from "../components/ui";
import { MemoryMaintenance } from "./MemoryMaintenance";

function memoryKind(type: string): string {
  const labels: Record<string, string> = {
    preference: "你的偏好",
    personal_fact: "关于你的事",
    profile: "关于你的事",
    shared_experience: "一起经历的事",
    episode: "一起经历的事",
    gradual_preference: "逐渐了解的偏好",
  };
  return labels[type] ?? "聊天中记下的事";
}

function updatedLabel(value: string | null): string {
  if (!value) return "更新时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "short", day: "numeric" }).format(new Date(value));
}

export function MemoryView({
  page,
  loading,
  error,
  onVisibility,
  onForget,
  onRetry,
  transport,
  onCorrected,
  onOpenConversation,
  initialMemoryId,
}: {
  page: UserMemoryPage | null;
  loading: boolean;
  error: string | null;
  onVisibility: (memoryId: string, visible: boolean) => Promise<void>;
  onForget: (memoryId: string) => Promise<void>;
  onRetry?: () => void;
  transport?: DesktopTransport;
  onCorrected?: (memory: UserMemoryView) => Promise<void>;
  onOpenConversation?: (id: string) => void;
  initialMemoryId?: string | null;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(initialMemoryId ?? null);
  const [query, setQuery] = useState("");
  const [forgetTarget, setForgetTarget] = useState<UserMemoryView | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [detailClosed, setDetailClosed] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const forgetDialogRef = useRef<HTMLElement | null>(null);
  const forgetTriggerRef = useRef<HTMLButtonElement | null>(null);
  const items = page?.items ?? [];
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return items;
    return items.filter((item) => `${item.summary} ${item.tags.join(" ")}`.toLocaleLowerCase().includes(needle));
  }, [items, query]);
  const selected = detailClosed ? null : selectedId ? filtered.find((item) => item.id === selectedId) ?? null : filtered[0] ?? null;

  useEffect(() => {
    if (!forgetTarget) return undefined;
    const dialog = forgetDialogRef.current;
    const focusable = dialog?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    focusable?.[0]?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setForgetTarget(null);
        return;
      }
      if (event.key !== "Tab" || !focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      forgetTriggerRef.current?.focus();
    };
  }, [forgetTarget]);

  const changeVisibility = async (memory: UserMemoryView) => {
    setBusy(memory.id);
    setActionError(null);
    try {
      await onVisibility(memory.id, !memory.visible);
    } catch {
      setActionError("记忆使用方式未获得完整确认，请重新读取列表核对后再操作。");
    } finally {
      setBusy(null);
    }
  };

  const confirmForget = async () => {
    if (!forgetTarget) return;
    setBusy(forgetTarget.id);
    setActionError(null);
    try {
      await onForget(forgetTarget.id);
      setSelectedId(null);
      setForgetTarget(null);
    } catch {
      setActionError("忘记操作未获得完整确认，请刷新记忆列表核对后再操作。");
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="page page--memory">
      <PageHeader
        eyebrow="由你决定保留什么"
        title="记忆与隐私"
        description="这里是 Sora 在长期相处中实际保留的内容。你可以随时隐藏或让她忘记。"
      />
      {transport && <MemoryMaintenance transport={transport} />}
      <div className="memory-summary-strip">
        <ShieldCheck size={18} />
        <span><strong>{page ? `${page.summary.available} 条记忆` : loading ? "正在读取记忆" : "记忆数量未确认"}</strong><small>{page ? `${page.summary.visible} 条可以自然用于对话 · ${page.summary.hidden} 条已隐藏${error ? "（上次读取结果）" : ""}` : "读取成功后才显示实际数量"}</small></span>
        <p>这里只展示易读摘要，不展开私密原文或内部评分。</p>
      </div>
      <div className="toolbar-band memory-toolbar">
        <label className="search-field search-field--wide">
          <Search size={17} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索记忆" placeholder="搜索记下的内容" />
        </label>
        <span>{filtered.length} 条</span>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {actionError && <div className="inline-error" role="alert">{actionError}</div>}
      {(error || actionError || !page) && onRetry && <button className="button button--secondary" type="button" disabled={loading || busy !== null} onClick={() => { setActionError(null); onRetry(); }}>重新读取记忆</button>}
      <section className={`memory-layout ${selected ? "has-detail" : ""}`}>
        <div className="memory-list" aria-label="记忆列表">
          {loading && <div className="prototype-loading">正在读取本机记忆…</div>}
          {!loading && !error && page && filtered.length === 0 && <div className="prototype-empty">{query.trim() ? "没有找到相关内容。" : "Sora 还没有保存可显示的长期记忆。"}</div>}
          {filtered.map((memory) => (
            <button className={`memory-row ${selected?.id === memory.id ? "is-active" : ""}`} type="button" key={memory.id} onClick={() => { setSelectedId(memory.id); setDetailClosed(false); }}>
              <span className="memory-kind"><Brain size={16} /></span>
              <span className="memory-row__content">
                <span className="memory-row__eyebrow">{memoryKind(memory.type)}</span>
                <strong>{memory.summary}</strong>
                <small>{updatedLabel(memory.updated_at)} · {memory.visible ? "可以用于对话" : "已隐藏"}</small>
              </span>
              <span className="memory-row__end">{memory.visible ? <Eye size={15} /> : <EyeOff size={15} />}<ChevronRight size={17} /></span>
            </button>
          ))}
        </div>
        {selected ? (
          <aside className="memory-detail">
            <header>
              <div><span>{memoryKind(selected.type)}</span><h2>{selected.summary}</h2></div>
              <IconButton label="关闭详情" onClick={() => { setSelectedId(null); setDetailClosed(true); }}><X size={17} /></IconButton>
            </header>
            <section>
              <h3>Sora 记下的是</h3>
              <p>{selected.summary}</p>
            </section>
            <section>
              <h3>它会怎么使用</h3>
              <p>{selected.visible ? "可以在合适的时候自然接上这件事。是否明确提起，仍会遵循你在当前对话里的选择和隐私边界。" : "这条内容已经隐藏，不会进入普通对话上下文。"}</p>
            </section>
            {transport && onCorrected ? <UserMemoryDetails key={selected.id} memory={selected} transport={transport} onCorrected={async memory => { await onCorrected(memory); setSelectedId(memory.id); }} onOpenConversation={onOpenConversation} /> : <section>
              <h3>来源说明</h3>
              <p>这条摘要保存在当前数据空间，并关联 {selected.evidence_count} 条证据。普通界面不展开原始证据；来源是否为某段对话，需要按实际来源记录核对。</p>
            </section>}
            <footer>
              <button className="button button--secondary" type="button" disabled={busy !== null || loading || !!error || !!actionError} onClick={() => void changeVisibility(selected)}>
                {selected.visible ? <EyeOff size={16} /> : <Eye size={16} />}{selected.visible ? "不在对话中使用" : "恢复用于对话"}
              </button>
              <button ref={forgetTriggerRef} className="button button--danger-quiet" type="button" disabled={busy !== null || loading || !!error || !!actionError} onClick={() => setForgetTarget(selected)}><Trash2 size={16} /> 忘记这件事</button>
            </footer>
          </aside>
        ) : <aside className="memory-detail memory-detail--empty"><Brain size={24} /><p>选择一条记忆查看它会怎样影响对话。</p></aside>}
      </section>
      {forgetTarget && (
        <div className="modal-backdrop" onMouseDown={() => setForgetTarget(null)}>
          <section ref={forgetDialogRef} className="modal modal--danger" role="dialog" aria-modal="true" aria-labelledby="forget-memory-title" aria-describedby="forget-memory-description" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span className="modal-icon modal-icon--danger"><Trash2 size={18} /></span><div><h2 id="forget-memory-title">让 Sora 忘记这件事？</h2><p id="forget-memory-description">删除请求会沿记忆系统传播到相关索引与投影。</p></div></div><IconButton label="关闭" onClick={() => setForgetTarget(null)}><X size={17} /></IconButton></header>
            <div className="confirmation-memory"><p>{forgetTarget.summary}</p></div>
            <footer><button className="button button--secondary" type="button" onClick={() => setForgetTarget(null)}>先保留</button><button className="button button--danger" type="button" disabled={busy === forgetTarget.id} onClick={() => void confirmForget()}>确认忘记</button></footer>
          </section>
        </div>
      )}
    </main>
  );
}
