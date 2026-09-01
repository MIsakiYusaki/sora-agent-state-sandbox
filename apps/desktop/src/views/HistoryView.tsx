import { CalendarDays, ChevronRight, GitBranch, MessageSquareText, Search, SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";
import type { HistoryItem } from "../contracts/generated";
import type { ConversationSummaryView } from "../transport";
import { PageHeader, Tag } from "../components/ui";

function dateParts(timestamp: number): { day: string; month: string; time: string } {
  const date = new Date(timestamp);
  return {
    day: String(date.getDate()).padStart(2, "0"),
    month: `${date.getMonth() + 1}月`,
    time: new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date),
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
}: {
  conversations: ReadonlyArray<ConversationSummaryView>;
  selectedId: string | null;
  preview: ReadonlyArray<HistoryItem>;
  loading: boolean;
  onSelect: (conversationId: string) => void;
  onContinue: (conversationId: string) => void;
  onNew: () => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(
    () => conversations.filter((item) => `${item.title}${item.preview}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())),
    [conversations, query],
  );
  const selected = conversations.find((item) => item.conversation_id === selectedId) ?? filtered[0] ?? null;

  return (
    <main className="page page--history">
      <PageHeader
        eyebrow="本机历史"
        title="对话与分支"
        description="搜索并恢复这台电脑上的真实对话。分支创建尚未接入时不会显示模拟分支。"
        actions={<button className="button" type="button" onClick={onNew}><MessageSquareText size={16} /> 新建对话</button>}
      />
      <div className="toolbar-band">
        <label className="search-field search-field--wide"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索对话内容" placeholder="搜索标题或最近消息" /><kbd>Ctrl K</kbd></label>
        <button className="button button--secondary" type="button" disabled><CalendarDays size={16} /> 时间</button>
        <button className="button button--secondary" type="button" disabled><SlidersHorizontal size={16} /> 筛选</button>
      </div>
      <section className="history-layout">
        <div className="history-list">
          <div className="list-label"><span>本机对话</span><small>{filtered.length} 个对话</small></div>
          {loading && <div className="prototype-loading">正在读取对话历史…</div>}
          {!loading && filtered.length === 0 && <div className="prototype-empty">没有符合条件的对话。</div>}
          {filtered.map((conversation) => {
            const parts = dateParts(conversation.updated_at_ms);
            return (
              <button className={`history-row ${conversation.conversation_id === selected?.conversation_id ? "is-active" : ""}`} type="button" key={conversation.conversation_id} onClick={() => onSelect(conversation.conversation_id)}>
                <span className="history-date"><strong>{parts.day}</strong><small>{parts.month}</small></span>
                <span className="history-row__content"><strong>{conversation.title}</strong><small>{conversation.preview}</small><span><Tag tone="neutral">文字</Tag><Tag tone="blue">{conversation.message_count} 条消息</Tag></span></span>
                <span className="history-row__end"><time>{parts.time}</time><ChevronRight size={17} /></span>
              </button>
            );
          })}
        </div>
        <aside className="history-preview">
          {selected ? (
            <>
              <header><div><span>{selected.title}</span><small>{selected.message_count} 条消息 · 版本 {selected.conversation_version}</small></div><button className="button button--secondary" type="button" onClick={() => onContinue(selected.conversation_id)}>继续对话</button></header>
              <div className="preview-thread">
                {preview.slice(-6).map((message) => (
                  <p className={message.role === "user" ? "preview-thread__user" : "preview-thread__sora"} key={`${message.turn_id}-${message.sequence}-${message.role}`}>{message.content}</p>
                ))}
                {preview.length === 0 && <div className="prototype-empty">正在载入这段对话…</div>}
              </div>
              <div className="branch-map">
                <div className="branch-map__heading"><strong>分支</strong><span>真实分支接口尚未提供</span></div>
                <div className="branch-unavailable"><GitBranch size={16} /><span>当前只能恢复主线；这里不会展示原型中的模拟分支。</span></div>
              </div>
            </>
          ) : <div className="prototype-empty">选择一段对话查看预览。</div>}
        </aside>
      </section>
    </main>
  );
}
