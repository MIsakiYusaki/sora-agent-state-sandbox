import { ChevronRight, MessageSquareText, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { HistoryItem } from "../contracts/generated";
import type { ConversationSummaryView, DesktopTransport, ConversationIndexView } from "../transport";
import { PageHeader } from "../components/ui";
import { useHistoryScroll } from "../components/HistoryScrollViewport";

function dateParts(timestamp: number): { day: string; month: string; time: string; full: string } {
  const date = new Date(timestamp);
  return {
    day: String(date.getDate()).padStart(2, "0"),
    month: `${date.getMonth() + 1}月`,
    time: new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date),
    full: new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(date),
  };
}

export function HistoryView({
  conversations,
  selectedId,
  preview,
  loading,
  onSelect,
  onContinue,
  onNew,
  previewLoading = false,
  previewError = null,
  error = null,
  onLoadEarlier,
  hasEarlier = false,
  transport,
  hasMoreConversations = false,
  onLoadConversations,
}: {
  conversations: ReadonlyArray<ConversationSummaryView>;
  selectedId: string | null;
  preview: ReadonlyArray<HistoryItem>;
  loading: boolean;
  onSelect: (conversationId: string) => void;
  onContinue: (conversationId: string) => void;
  onNew: () => void;
  previewLoading?: boolean;
  previewError?: string | null;
  error?: string | null;
  onLoadEarlier?: () => void;
  hasEarlier?: boolean;
  transport?: DesktopTransport;
  hasMoreConversations?: boolean;
  onLoadConversations?: () => void;
}) {
  const [query, setQuery] = useState("");
  const [searchPage, setSearchPage] = useState<ConversationIndexView | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchAttempt, setSearchAttempt] = useState(0);
  const [resultQuery, setResultQuery] = useState("");
  const searchRequest = useRef(0);
  const searchPageInFlight = useRef(false);
  const remote = !!transport && !!query.trim();
  const messageKey = (item: HistoryItem) => `${item.turn_id}:${item.sequence}:${item.role}`;
  const last = preview[preview.length - 1];
  const { viewportRef, rememberPrepend, onScroll } = useHistoryScroll({ scope: selectedId ?? "none", firstKey: preview[0] ? messageKey(preview[0]) : undefined,
    revision: last ? `${messageKey(last)}:${last.content}` : "empty", loadingEarlier: previewLoading });
  useEffect(() => {
    const epoch = ++searchRequest.current;
    searchPageInFlight.current = false;
    setSearchPage(null); setSearchError(null); setSearchLoading(remote);
    if (!transport || !query.trim()) return;
    const timer = window.setTimeout(() => {
      void transport.listConversations({ query: query.trim(), offset: 0 }).then(page => {
        if (page.next_offset != null && page.next_offset <= 0) throw new Error("search cursor did not advance");
        if (epoch === searchRequest.current) { setResultQuery(query.trim()); setSearchPage(page); }
      }).catch(() => { if (epoch === searchRequest.current) setSearchError("正文搜索失败，不能据此认定没有相关对话。"); })
        .finally(() => { if (epoch === searchRequest.current) setSearchLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); ++searchRequest.current; };
  }, [transport, query, remote, searchAttempt]);
  const loadMore = async () => {
    if (!remote) { onLoadConversations?.(); return; }
    if (!transport || searchPage?.next_offset == null || searchLoading || searchPageInFlight.current || resultQuery !== query.trim()) return;
    const epoch = ++searchRequest.current;
    searchPageInFlight.current = true;
    setSearchLoading(true); setSearchError(null);
    try {
      const next = await transport.listConversations({ query: query.trim(), offset: searchPage.next_offset });
      if (next.next_offset != null && next.next_offset <= searchPage.next_offset) throw new Error("search cursor did not advance");
      if (epoch === searchRequest.current) setSearchPage(current => ({ ...next, items: [...(current?.items ?? []), ...next.items.filter(item => !current?.items.some(old => old.conversation_id === item.conversation_id))] }));
    } catch { if (epoch === searchRequest.current) setSearchError("更早对话搜索失败，已读取结果并非完整搜索结果。"); }
    finally { if (epoch === searchRequest.current) { searchPageInFlight.current = false; setSearchLoading(false); } }
  };
  const more = remote ? searchPage?.next_offset != null : hasMoreConversations;
  const filtered = useMemo(() => {
    if (remote) return resultQuery === query.trim() ? searchPage?.items ?? [] : [];
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return conversations;
    return conversations.filter((item) => `${item.title} ${item.preview}`.toLocaleLowerCase().includes(needle));
  }, [conversations, query, remote, searchPage, resultQuery]);
  const selected = filtered.find((item) => item.conversation_id === selectedId) ?? null;

  return (
    <main className="page page--history">
      <PageHeader
        eyebrow="只保存在这台电脑"
        title="对话历史"
        description="找回以前聊过的内容，或从一段旧对话继续。"
        actions={<button className="button" type="button" onClick={onNew}><MessageSquareText size={16} /> 新建对话</button>}
      />
      <div className="toolbar-band history-toolbar">
        <label className="search-field search-field--wide">
          <Search size={17} />
          <input value={query} maxLength={200} onChange={(event) => setQuery(event.target.value)} aria-label="搜索对话" placeholder={transport ? "搜索完整聊天正文" : "搜索标题或摘要"} />
        </label>
        <span>已读取 {filtered.length} 段对话{more ? " · 还有更早记录" : ""}</span>
      </div>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {searchError && <p className="inline-error" role="alert">{searchError}</p>}
      {searchError && remote && !searchPage && <button className="button button--secondary" type="button" disabled={searchLoading} onClick={() => setSearchAttempt(value => value + 1)}>重试正文搜索</button>}
      <section className="history-layout">
        <div className="history-list" aria-label="对话列表">
          {(loading || searchLoading) && <div className="prototype-loading">正在读取对话历史…</div>}
          {!loading && !searchLoading && !error && !searchError && filtered.length === 0 && <div className="prototype-empty">{more ? "本页没有匹配内容；继续搜索更早记录。" : query.trim() ? "没有找到相关对话。" : "还没有保存的对话。"}</div>}
          {filtered.map((conversation) => {
            const parts = dateParts(conversation.updated_at_ms);
            return (
              <button className={`history-row ${conversation.conversation_id === selected?.conversation_id ? "is-active" : ""}`} type="button" key={conversation.conversation_id} onClick={() => onSelect(conversation.conversation_id)}>
                <span className="history-date"><strong>{parts.day}</strong><small>{parts.month}</small></span>
                <span className="history-row__content">
                  <strong>{conversation.title || "未命名对话"}</strong>
                  <small>{conversation.preview || "这段对话还没有内容"}</small>
                  <span>{conversation.message_count} 条消息 · {parts.time}</span>
                </span>
                <span className="history-row__end"><ChevronRight size={17} /></span>
              </button>
            );
          })}
          {more && <button className="button button--secondary" type="button" disabled={loading || searchLoading} onClick={() => void loadMore()}>{remote ? "继续搜索更早对话" : "加载更多对话"}</button>}
        </div>
        <aside className="history-preview" aria-live="polite">
          {selected ? (
            <>
              <header>
                <div><h2>{selected.title || "未命名对话"}</h2><p>{dateParts(selected.updated_at_ms).full} · {selected.message_count} 条消息</p></div>
                <button className="button button--secondary" type="button" onClick={() => onContinue(selected.conversation_id)}>继续对话</button>
              </header>
              <div className="preview-thread" ref={viewportRef} onScroll={onScroll} aria-label="历史消息预览" tabIndex={0}>
                {hasEarlier && onLoadEarlier && <button className="button button--secondary" type="button" disabled={previewLoading} onClick={() => { rememberPrepend(); onLoadEarlier(); }}>加载更早消息</button>}
                {previewError && <p className="inline-error" role="alert">{previewError}</p>}
                {previewError && !hasEarlier && <button className="button button--secondary" type="button" disabled={previewLoading} onClick={() => onSelect(selected.conversation_id)}>重新读取这段对话</button>}
                {preview.map((message) => (
                  <article data-history-key={messageKey(message)} className={message.role === "user" ? "preview-message preview-message--user" : "preview-message preview-message--sora"} key={`${message.turn_id}-${message.sequence}-${message.role}`}>
                    <span>{message.role === "user" ? "你" : "Sora"}</span>
                    <p>{message.content}</p>
                  </article>
                ))}
                {previewLoading && <div className="prototype-loading" role="status">正在载入这段对话…</div>}
                {!previewLoading && !previewError && preview.length === 0 && <div className="prototype-empty">这段对话没有可显示的消息。</div>}
              </div>
            </>
          ) : <div className="prototype-empty">选择一段对话查看内容。</div>}
        </aside>
      </section>
    </main>
  );
}
