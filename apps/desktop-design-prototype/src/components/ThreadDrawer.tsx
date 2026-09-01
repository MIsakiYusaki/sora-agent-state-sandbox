import { ChevronDown, GitBranch, MoreHorizontal, Plus, Search } from "lucide-react";
import { conversations } from "../data";
import { IconButton } from "./ui";

export function ThreadDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <aside className={`thread-drawer ${open ? "is-open" : ""}`} aria-label="对话列表">
      <div className="thread-drawer__header">
        <button className="profile-switcher" type="button">
          <span className="avatar avatar--small"><img src="/sora-reference.png" alt="" /></span>
          <span><strong>Sora</strong><small>这台电脑</small></span>
          <ChevronDown size={15} />
        </button>
        <IconButton label="新建对话"><Plus size={18} /></IconButton>
      </div>
      <label className="search-field">
        <Search size={16} />
        <input placeholder="搜索对话" aria-label="搜索对话" />
        <kbd>⌘ K</kbd>
      </label>
      <div className="thread-section-label"><span>最近</span><button type="button" onClick={onClose}>收起</button></div>
      <div className="thread-list">
        {conversations.map((conversation) => (
          <button key={conversation.id} className={`thread-row ${conversation.active ? "is-active" : ""}`} type="button">
            <span className="thread-row__content">
              <strong>{conversation.title}</strong>
              <small>{conversation.preview}</small>
            </span>
            <span className="thread-row__meta">
              <time>{conversation.time}</time>
              {conversation.branchCount && <span><GitBranch size={11} />{conversation.branchCount}</span>}
              {conversation.unread && <i />}
            </span>
            <MoreHorizontal className="thread-row__more" size={15} />
          </button>
        ))}
      </div>
      <div className="thread-drawer__footer">
        <span>本机保存</span>
        <span className="health-chip"><i /> 对话服务正常</span>
      </div>
    </aside>
  );
}
