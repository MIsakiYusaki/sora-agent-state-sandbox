import { LoaderCircle, RotateCcw } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ActiveTurn, ConversationMessage } from "../conversation/state";

interface MessageListProps {
  readonly messages: ReadonlyArray<ConversationMessage>;
  readonly activeTurn: ActiveTurn | null;
  readonly emptyMode?: "conversation" | "provider_setup";
}

function formatTime(timestamp: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

export function MessageList({ messages, activeTurn, emptyMode = "conversation" }: MessageListProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const initializedRef = useRef(false);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    if (!initializedRef.current || nearBottomRef.current) {
      viewport.scrollTop = viewport.scrollHeight;
      initializedRef.current = true;
    }
  }, [messages, activeTurn?.stage]);

  return (
    <div
      className="message-scroll"
      ref={viewportRef}
      role="feed"
      aria-label="当前对话"
      aria-busy={activeTurn !== null}
      onScroll={(event) => {
        const viewport = event.currentTarget;
        nearBottomRef.current =
          viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 72;
      }}
    >
      <div className="message-list">
        {messages.length > 0 && <div className="day-divider"><span>今天</span></div>}
        {messages.length === 0 ? (
          <EmptyConversation mode={emptyMode} />
        ) : (
          messages.map((message, index) => (
            <article
              className={"message message--" + message.role}
              key={message.id}
              data-delivery={message.delivery}
              aria-posinset={index + 1}
              aria-setsize={messages.length}
              tabIndex={0}
            >
              {message.role === "sora" && (
                <span className="avatar avatar--message" aria-hidden="true">
                  <img src="/sora-reference.png" alt="" />
                </span>
              )}
              <div className="message__content">
                <div className="message__body">
                  <p>{message.content}</p>
                  {message.delivery === "streaming" && (
                    <span className="stream-caret" aria-hidden="true" />
                  )}
                </div>
                <div className="message__meta">
                  <time dateTime={new Date(message.createdAtMs).toISOString()}>
                    {formatTime(message.createdAtMs)}
                  </time>
                  {message.delivery === "pending" && <span>等待确认</span>}
                  {message.delivery === "failed" && (
                    <span className="message__failure">
                      <RotateCcw size={12} /> 未送达
                    </span>
                  )}
                  {message.delivery === "cancelled" && <span>已停止</span>}
                </div>
              </div>
            </article>
          ))
        )}
        {activeTurn && !messages.some(
          (message) => message.turnId === activeTurn.turnId && message.role === "sora",
        ) && (
          <div className="thinking-row" role="status" aria-live="polite">
            <LoaderCircle size={16} className="spin" />
            <span>Sora 正在回应</span>
          </div>
        )}
        <div className="sr-only" aria-live="polite" aria-atomic="true">
          {activeTurn?.stage === "turn.committed" ? "Sora 的回应已完成" : ""}
        </div>
      </div>
    </div>
  );
}

function EmptyConversation({ mode }: { mode: "conversation" | "provider_setup" }) {
  return (
    <section className="empty-conversation">
      <span className="empty-presence" aria-hidden="true">
        <img src="/sora-reference.png" alt="" />
      </span>
      <h2>{mode === "provider_setup" ? "先连接一个模型服务" : "从现在想说的事开始"}</h2>
      <p>{mode === "provider_setup"
        ? "模型密钥只会通过 Windows 原生安全窗口录入。连接完成后，这份空白对话和草稿都会保留。"
        : "不需要先设置关系或偏好。你可以随时在这次聊天里调整回应方式。"}</p>
    </section>
  );
}
