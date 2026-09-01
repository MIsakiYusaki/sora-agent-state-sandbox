import { Check, Info, PanelRightClose, ShieldCheck } from "lucide-react";
import type { RefObject } from "react";
import { useOverlayFocus } from "../hooks/useOverlayFocus";

export function ContextDrawer({
  open,
  modal,
  onClose,
  triggerRef,
}: {
  readonly open: boolean;
  readonly modal: boolean;
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

  return (
    <>
      {modal && <button className="drawer-scrim" type="button" aria-label="关闭聊天信息" onClick={onClose} />}
      <aside
        className="context-drawer"
        ref={drawerRef}
        role={modal ? "dialog" : "complementary"}
        aria-modal={modal || undefined}
        aria-label="这次聊天"
        tabIndex={-1}
      >
        <header>
          <span>
            <strong>这次聊天</strong>
            <small>只显示你可以理解和控制的信息</small>
          </span>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭聊天信息"
            data-autofocus
            onClick={onClose}
          >
            <PanelRightClose size={18} />
          </button>
        </header>
        <section className="context-section">
          <h2>当前回应方式</h2>
          <ul className="context-summary">
            <li><Check size={16} /> 自然回应</li>
            <li><Check size={16} /> 不主动追问</li>
            <li><Check size={16} /> 过去的内容自然接上</li>
          </ul>
          <p>相关服务接入后，才会在这里提供可以保存和撤销的操作。</p>
        </section>
        <section className="context-section">
          <h2>这条回应参考了什么</h2>
          <div className="context-empty">
            <Info size={17} />
            <p>回应来源功能开放后会在这里显示，不会展示内部评分或推理过程。</p>
          </div>
        </section>
        <footer>
          <ShieldCheck size={16} />
          <span>隐私和长期控制需要明确确认，并提供撤销入口。</span>
        </footer>
      </aside>
    </>
  );
}
