import { ChevronDown, MoreHorizontal, Plus, Search } from "lucide-react";
import type { ConversationSummaryView } from "../transport";
import type { RefObject } from "react";
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
}) {
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
        <button className="profile-switcher" type="button">
          <span className="avatar avatar--small"><img src="/sora-reference.png" alt="" /></span>
          <span><strong>Sora</strong><small>这台电脑</small></span>
          <ChevronDown size={15} />
        </button>
        <IconButton label="新建对话" onClick={onNew}><Plus size={18} /></IconButton>
      </div>
      <label className="search-field">
        <Search size={16} />
        <input placeholder="搜索对话" aria-label="搜索对话" />
        <kbd>Ctrl K</kbd>
      </label>
      <div className="thread-section-label"><span>最近</span><button type="button" onClick={onClose}>收起</button></div>
      <div className="thread-list">
        {loading && <div className="prototype-loading">正在读取本机对话…</div>}
        {!loading && conversations.length === 0 && (
          <div className="prototype-empty">还没有保存的对话。发出第一条消息后会出现在这里。</div>
        )}
        {conversations.map((conversation) => (
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
            <MoreHorizontal className="thread-row__more" size={15} />
          </button>
        ))}
      </div>
      <div className="thread-drawer__footer">
        <span>本机保存</span>
        <span className="health-chip"><i /> 已接入真实会话历史</span>
      </div>
    </aside>
    </>
  );
}
