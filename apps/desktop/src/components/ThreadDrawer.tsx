import { MoreHorizontal, Plus, Search, Settings } from "lucide-react";
import type { ConversationSummaryView } from "../transport";
import { useEffect, useRef, useState, type RefObject } from "react";
import { useOverlayFocus } from "../hooks/useOverlayFocus";
import { IconButton } from "./ui";

function relativeTime(timestamp: number): string {
  const date = new Date(timestamp);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

export function ThreadDrawer({
  open,
  conversations,
  activeConversationId,
  loading,
  modal,
  triggerRef,
  onClose,
  onNew,
  onSelect,
  onOpenSettings,
  onOpenHistory,
  hasMore = false,
  onLoadMore,
}: {
  open: boolean;
  conversations: ReadonlyArray<ConversationSummaryView>;
  activeConversationId: string;
  loading: boolean;
  modal: boolean;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onNew: () => void;
  onSelect: (conversationId: string) => void;
  onOpenSettings?: () => void;
  onOpenHistory?: (conversationId: string) => void;
  hasMore?: boolean;
  onLoadMore?: () => void;
}) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const filtered = conversations.filter(item => `${item.title} ${item.preview}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  useEffect(() => {
    if (!open) return;
    const handle = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  }, [open]);
  const drawerRef = useOverlayFocus<HTMLElement>({
    open,
    modal,
    onClose,
    triggerRef,
  });
  if (!open) return null;
  return (
    <>
    {modal && <button className="drawer-scrim" type="button" aria-label="关闭会话历史" onClick={onClose} />}
    <aside ref={drawerRef} className="thread-drawer is-open" role={modal ? "dialog" : "complementary"} aria-modal={modal || undefined} aria-label="会话历史" tabIndex={-1}>
      <div className="thread-drawer__header">
        <button className="profile-switcher" type="button" aria-label="管理当前数据空间" disabled={!onOpenSettings} onClick={onOpenSettings}>
          <span className="avatar avatar--small"><img src="/sora-reference.png" alt="" /></span>
          <span><strong>Sora</strong><small>这台电脑</small></span>
          <Settings size={15} />
        </button>
        <IconButton label="新建对话" onClick={onNew}><Plus size={18} /></IconButton>
      </div>
      <label className="search-field">
        <Search size={16} />
        <input ref={searchRef} data-conversation-search value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题或摘要" aria-label="搜索对话" />
        <kbd>Ctrl K</kbd>
      </label>
      <div className="thread-section-label"><span>最近</span><button type="button" onClick={onClose}>收起</button></div>
      <div className="thread-list">
        {loading && <div className="prototype-loading">正在读取本机对话…</div>}
        {!loading && conversations.length === 0 && (
          <div className="prototype-empty">还没有保存的对话。发出第一条消息后会出现在这里。</div>
        )}
        {!loading && query.trim() && filtered.length === 0 && <div className="prototype-empty">没有匹配的对话。</div>}
        {filtered.map((conversation) => (
          <div className="thread-entry" key={conversation.conversation_id}>
          <button
            key={conversation.conversation_id}
            className={`thread-row ${conversation.conversation_id === activeConversationId ? "is-active" : ""}`}
            type="button"
            onClick={() => onSelect(conversation.conversation_id)}
          >
            <span className="thread-row__content">
              <strong>{conversation.title}</strong>
              <small>{conversation.preview}</small>
            </span>
            <span className="thread-row__meta">
              <time>{relativeTime(conversation.updated_at_ms)}</time>
              <span>{conversation.message_count} 条</span>
            </span>
          </button>
          <details className="thread-actions"><summary aria-label={`更多操作：${conversation.title}`}><MoreHorizontal size={15} /></summary><div className="thread-menu">
            <button type="button" onClick={() => onSelect(conversation.conversation_id)}>继续对话</button>
            {onOpenHistory && <button type="button" onClick={() => onOpenHistory(conversation.conversation_id)}>查看历史</button>}
          </div></details>
          </div>
        ))}
        {hasMore && onLoadMore && <button className="button button--secondary" type="button" disabled={loading} onClick={onLoadMore}>加载更多对话</button>}
      </div>
      <div className="thread-drawer__footer">
        <span>本机保存</span>
        <span className="health-chip"><i /> 已接入真实会话历史</span>
      </div>
    </aside>
    </>
  );
}
