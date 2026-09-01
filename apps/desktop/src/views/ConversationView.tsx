import {
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Ellipsis,
  History,
  Info,
  Menu,
  PanelRight,
  RotateCcw,
  Send,
  SlidersHorizontal,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { PrototypeState } from "../data";
import { IconButton, StatusDot } from "../components/ui";

interface Message {
  id: number;
  role: "user" | "sora";
  text: string;
  time: string;
  status?: "streaming" | "stopped";
}

const seedMessages: Message[] = [
  {
    id: 1,
    role: "user",
    text: "今天脑子有点乱，不太想解决问题，只想慢慢说一会儿。",
    time: "22:14",
  },
  {
    id: 2,
    role: "sora",
    text: "好。那我们今晚不急着把任何事变成答案。你想到哪里就说到哪里，我在这儿跟着你。",
    time: "22:14",
  },
  {
    id: 3,
    role: "user",
    text: "我刚才路过窗边，团子还是缩在那条旧毛毯上。",
    time: "22:17",
  },
  {
    id: 4,
    role: "sora",
    text: "它最近很认那块位置。窗边、旧毛毯，再加上今晚这种安静的光，听起来像是它给自己选好的小岛。",
    time: "22:18",
  },
];

export function ConversationView({
  prototypeState,
  threadDrawerOpen,
  contextOpen,
  onThreadDrawer,
  onContext,
  onOpenControls,
  onOpenHistory,
  onReconnect,
}: {
  prototypeState: PrototypeState;
  threadDrawerOpen: boolean;
  contextOpen: boolean;
  onThreadDrawer: () => void;
  onContext: () => void;
  onOpenControls: () => void;
  onOpenHistory: () => void;
  onReconnect: () => void;
}) {
  const [messages, setMessages] = useState<Message[]>(seedMessages);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [explainOpen, setExplainOpen] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const isOffline = prototypeState === "offline" || prototypeState === "recovering";
  const isEmpty = prototypeState === "empty";
  const noProvider = prototypeState === "no-provider";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages, streaming]);

  const send = () => {
    const text = draft.trim();
    if (!text || streaming || isOffline || noProvider) return;
    const id = Date.now();
    setMessages((current) => [...current, { id, role: "user", text, time: "现在" }]);
    setDraft("");
    setStreaming(true);
    window.setTimeout(() => {
      setMessages((current) => [
        ...current,
        {
          id: id + 1,
          role: "sora",
          text: "我听见了。我们可以先把这句话放在这里，不急着往下解释。",
          time: "现在",
        },
      ]);
      setStreaming(false);
    }, 1300);
  };

  return (
    <section className={`conversation-workspace ${contextOpen ? "has-context" : ""}`}>
      <div className="conversation-main">
        <header className="conversation-topbar">
          <div className="conversation-topbar__start">
            <IconButton label={threadDrawerOpen ? "收起对话列表" : "展开对话列表"} onClick={onThreadDrawer}>
              <Menu size={18} />
            </IconButton>
            <span className="avatar"><img src="/sora-reference.png" alt="Sora" /></span>
            <div className="conversation-identity">
              <button type="button"><strong>Sora</strong><ChevronDown size={14} /></button>
              <span><StatusDot tone={isOffline ? "danger" : "positive"} />{isOffline ? "离线" : "在你身边"}</span>
            </div>
          </div>
          <div className="conversation-topbar__actions">
            <IconButton label="历史与分支" onClick={onOpenHistory}><History size={18} /></IconButton>
            <IconButton label="本轮对话控制" onClick={onOpenControls}><SlidersHorizontal size={18} /></IconButton>
            <IconButton label={contextOpen ? "关闭聊天摘要" : "查看这次聊天"} className={contextOpen ? "is-active" : ""} onClick={onContext}>
              <PanelRight size={18} />
            </IconButton>
          </div>
        </header>

        {isOffline && (
          <div className="connection-band connection-band--danger" role="status">
            <WifiOff size={16} />
            <span>{prototypeState === "recovering" ? "正在恢复对话服务，草稿已经保留。" : "对话服务暂时离线。你仍可查看历史，恢复后可以继续发送。"}</span>
            <button type="button" onClick={onReconnect}>{prototypeState === "recovering" ? "查看进度" : "重新连接"}</button>
          </div>
        )}
        {noProvider && (
          <div className="connection-band connection-band--warning" role="status">
            <CircleAlert size={16} />
            <span>尚未配置可用于文字对话的模型。</span>
            <button type="button">打开模型设置</button>
          </div>
        )}

        <div className="message-scroll" role="log" aria-live="polite">
          {isEmpty ? (
            <EmptyConversation />
          ) : (
            <div className="message-canvas">
              <div className="day-divider"><span>今天</span></div>
              {messages.map((message) => (
                <article className={`message message--${message.role}`} key={message.id}>
                  {message.role === "sora" && <span className="avatar avatar--message"><img src="/sora-reference.png" alt="" /></span>}
                  <div className="message__column">
                    <div className="message__body"><p>{message.text}</p></div>
                    <div className="message__meta">
                      <time>{message.time}</time>
                      {message.status === "stopped" && <span>已停止</span>}
                      {message.role === "sora" && (
                        <div className="message__tools">
                          <IconButton label="复制"><Copy size={13} /></IconButton>
                          <IconButton label="有帮助"><ThumbsUp size={13} /></IconButton>
                          <IconButton label="没有帮助"><ThumbsDown size={13} /></IconButton>
                          <IconButton label="解释这条回应使用了什么" onClick={() => setExplainOpen(message.id)}><Info size={13} /></IconButton>
                          <IconButton label="更多操作"><Ellipsis size={14} /></IconButton>
                        </div>
                      )}
                    </div>
                  </div>
                </article>
              ))}
              {streaming && (
                <article className="message message--sora">
                  <span className="avatar avatar--message"><img src="/sora-reference.png" alt="" /></span>
                  <div className="message__column">
                    <div className="message__body message__body--streaming">
                      <p>我在听<span className="stream-caret" /></p>
                    </div>
                  </div>
                </article>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        <footer className="composer-zone">
          <div className={`composer ${streaming ? "is-busy" : ""}`}>
            <textarea
              rows={1}
              value={draft}
              aria-label="给 Sora 发消息"
              placeholder={isOffline ? "连接恢复后可以继续" : noProvider ? "配置模型后可以开始对话" : "和 Sora 说点什么…"}
              disabled={isOffline || noProvider}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  send();
                }
              }}
            />
            <div className="composer__footer">
              <div className="composer__mode">
                <button type="button"><Sparkles size={14} /> 自然回应 <ChevronDown size={13} /></button>
                <span>本轮不追问</span>
              </div>
              {streaming ? (
                <button className="composer__action composer__action--stop" type="button" aria-label="停止生成" onClick={() => setStreaming(false)}><Square size={15} fill="currentColor" /></button>
              ) : (
                <button className="composer__action" type="button" aria-label="发送" disabled={!draft.trim() || isOffline || noProvider} onClick={send}><Send size={17} /></button>
              )}
            </div>
          </div>
          <span className="composer-note">Sora 可能会犯错。重要信息请核实。</span>
        </footer>
      </div>

      {contextOpen && <ContextDrawer onClose={onContext} onOpenControls={onOpenControls} />}
      {explainOpen !== null && <ResponseExplanation onClose={() => setExplainOpen(null)} />}
    </section>
  );
}

function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <div className="empty-presence" aria-hidden="true">
        <img src="/sora-reference.png" alt="" />
        <span className="empty-presence__star">✦</span>
      </div>
      <h1>晚上好。</h1>
      <p>不用想好从哪里开始。你此刻想到的第一句话就可以。</p>
      <div className="prompt-suggestions">
        <button type="button">今天发生了一件小事</button>
        <button type="button">我只想安静地待一会儿</button>
        <button type="button">陪我把一个想法说清楚</button>
      </div>
    </div>
  );
}

function ContextDrawer({ onClose, onOpenControls }: { onClose: () => void; onOpenControls: () => void }) {
  return (
    <aside className="context-drawer" aria-label="这次聊天">
      <header><div><span>这次聊天</span><small>你可以随时改变聊天方式</small></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header>
      <section className="context-section">
        <div className="context-section__heading"><strong>现在的聊天方式</strong><button className="text-link" type="button" onClick={onOpenControls}>调整</button></div>
        <div className="context-mode-list">
          <span><Check size={14} /><strong>自然回应</strong></span>
          <span><Check size={14} /><strong>不在结尾追问</strong></span>
          <span><Check size={14} /><strong>可以顺着话题延伸</strong></span>
        </div>
      </section>
      <section className="context-section">
        <div className="context-section__heading"><strong>接上了过去的内容</strong><span>2 条</span></div>
        <button className="context-memory" type="button"><Brain size={16} /><span><strong>你不喜欢被连续追问</strong><small>只影响聊天节奏，没有直接提起</small></span><Check size={14} /></button>
        <button className="context-memory" type="button"><Brain size={16} /><span><strong>团子喜欢睡在窗边</strong><small>来自你明确说过的话</small></span><Check size={14} /></button>
      </section>
      <section className="context-section">
        <div className="context-section__heading"><strong>这次没有做的事</strong></div>
        <p className="context-plain-note">没有主动创建任务，也没有把不确定的推测写进记忆。</p>
      </section>
      <footer><Info size={14} /><span>这里只说明对你有意义的内容。内部模型判断和分数不会出现在普通界面。</span></footer>
    </aside>
  );
}

function ResponseExplanation({ onClose }: { onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="modal modal--explain" role="dialog" aria-modal="true" aria-labelledby="explain-title" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="modal-icon"><Info size={18} /></span><div><h2 id="explain-title">这条回应参考了什么</h2><p>只说明与你有关的部分</p></div></div><IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton></header>
        <div className="explain-list">
          <div><Check size={16} /><span><strong>跟随了你选择的“不追问”</strong><small>所以回应停在陪伴，没有在结尾追加问题。</small></span></div>
          <div><Check size={16} /><span><strong>自然接上了团子和旧毛毯</strong><small>这些内容来自你以前明确说过的话。</small></span></div>
          <div><Check size={16} /><span><strong>没有把推测当成事实</strong><small>“像一座小岛”只是这次回应里的比喻，不会写进记忆。</small></span></div>
        </div>
        <footer><button className="button button--secondary" type="button">调整聊天方式</button><button className="button" type="button" onClick={onClose}>知道了</button></footer>
      </section>
    </div>
  );
}
