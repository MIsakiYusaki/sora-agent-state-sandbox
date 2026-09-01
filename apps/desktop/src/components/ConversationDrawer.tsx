import { ChevronRight, PanelLeftClose, Search } from "lucide-react";
import type { RefObject } from "react";
import type { ConversationMessage } from "../conversation/state";
import { useOverlayFocus } from "../hooks/useOverlayFocus";

export function ConversationDrawer({
  open,
  modal,
  messages,
  onClose,
  triggerRef,
}: {
  readonly open: boolean;
  readonly modal: boolean;
  readonly messages: ReadonlyArray<ConversationMessage>;
  readonly onClose: () => void;
  readonly triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const drawerRef = useOverlayFocus<HTMLElement>({
    open,
    modal,
    onClose,
    triggerRef,
  });
  if (!open) return null;

  const lastUserMessage = [...messages].reverse().find((message) => message.role === "user");
  const title = lastUserMessage?.content.trim() || "当前对话";
  const soraMessage = [...messages].reverse().find((message) => message.role === "sora");

  return (
    <>
      {modal && <button className="drawer-scrim" type="button" aria-label="关闭历史" onClick={onClose} />}
      <aside
        className="thread-drawer"
        ref={drawerRef}
        role={modal ? "dialog" : "complementary"}
        aria-modal={modal || undefined}
        aria-label="会话历史"
        tabIndex={-1}
      >
        <header className="thread-drawer__header">
          <span className="avatar avatar--small" aria-hidden="true">
            <img src="/sora-reference.png" alt="" />
          </span>
          <span>
            <strong>Sora</strong>
            <small>这台电脑</small>
          </span>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭历史"
            data-autofocus
            onClick={onClose}
          >
            <PanelLeftClose size={18} />
          </button>
        </header>
        <label className="drawer-search">
          <Search size={16} aria-hidden="true" />
          <span className="sr-only">搜索会话</span>
          <input value="" placeholder="完整历史开放后可以搜索" disabled />
        </label>
        <div className="thread-section-label">
          <span>最近</span>
          <small>{messages.length} 条消息</small>
        </div>
        <nav className="thread-list" aria-label="最近会话">
          <button className="thread-row is-active" type="button" onClick={onClose}>
            <span>
              <strong>{title}</strong>
              <small>{soraMessage?.content || "还没有回复"}</small>
            </span>
            <ChevronRight size={16} aria-hidden="true" />
          </button>
        </nav>
        <footer className="thread-drawer__footer">
          当前只显示这台电脑上的当前对话
        </footer>
      </aside>
    </>
  );
}
