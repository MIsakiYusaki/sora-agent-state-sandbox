import { CalendarDays, ChevronRight, GitBranch, MessageSquareText, Search, SlidersHorizontal } from "lucide-react";
import { conversations } from "../data";
import { PageHeader, Tag } from "../components/ui";

export function HistoryView() {
  return (
    <main className="page page--history">
      <PageHeader
        eyebrow="本机历史"
        title="对话与分支"
        description="搜索、恢复或比较曾经的对话路径。删除和导出操作会单独确认。"
        actions={<button className="button" type="button"><MessageSquareText size={16} /> 新建对话</button>}
      />
      <div className="toolbar-band">
        <label className="search-field search-field--wide"><Search size={17} /><input aria-label="搜索对话内容" placeholder="搜索标题或消息内容" /><kbd>⌘ K</kbd></label>
        <button className="button button--secondary" type="button"><CalendarDays size={16} /> 时间</button>
        <button className="button button--secondary" type="button"><SlidersHorizontal size={16} /> 筛选</button>
      </div>
      <section className="history-layout">
        <div className="history-list">
          <div className="list-label"><span>最近 30 天</span><small>{conversations.length} 个对话</small></div>
          {conversations.map((conversation, index) => (
            <button className={`history-row ${index === 0 ? "is-active" : ""}`} type="button" key={conversation.id}>
              <span className="history-date"><strong>{index === 0 ? "29" : 28 - index}</strong><small>8月</small></span>
              <span className="history-row__content"><strong>{conversation.title}</strong><small>{conversation.preview}</small><span><Tag tone="neutral">文字</Tag>{conversation.branchCount && <Tag tone="violet"><GitBranch size={11} /> {conversation.branchCount} 条分支</Tag>}</span></span>
              <span className="history-row__end"><time>{conversation.time}</time><ChevronRight size={17} /></span>
            </button>
          ))}
        </div>
        <aside className="history-preview">
          <header><div><span>今晚想慢一点</span><small>今天 22:14–22:18 · 4 条消息</small></div><button className="button button--secondary" type="button">继续对话</button></header>
          <div className="preview-thread">
            <p className="preview-thread__user">今天脑子有点乱，不太想解决问题，只想慢慢说一会儿。</p>
            <p className="preview-thread__sora">好。那我们今晚不急着把任何事变成答案。</p>
            <p className="preview-thread__user">我刚才路过窗边，团子还是缩在那条旧毛毯上。</p>
          </div>
          <div className="branch-map">
            <div className="branch-map__heading"><strong>分支</strong><span>从第 2 条消息开始</span></div>
            <button className="branch-node is-current" type="button"><i /><span><strong>主线</strong><small>当前对话</small></span></button>
            <button className="branch-node" type="button"><i /><span><strong>聊团子的习惯</strong><small>昨天创建</small></span></button>
          </div>
        </aside>
      </section>
    </main>
  );
}
