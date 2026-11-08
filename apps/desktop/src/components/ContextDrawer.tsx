import { Check, Info, PanelRightClose, ShieldCheck } from "lucide-react";
import { useEffect, useState, type RefObject } from "react";
import { useOverlayFocus } from "../hooks/useOverlayFocus";
import type { ConversationControlsView, DesktopTransport } from "../transport";
import type { UserResponseBasis } from "../transport/DesktopTransport";
import { NarrativePanel } from "../views/NarrativePanel";

export function ContextDrawer({
  open,
  modal,
  onClose,
  triggerRef,
  controls = null,
  loading = false,
  error = null,
  onEdit,
  transport,
  conversationId,
  turnId,
  onOpenMemory,
}: {
  readonly open: boolean;
  readonly modal: boolean;
  readonly onClose: () => void;
  readonly triggerRef: RefObject<HTMLButtonElement | null>;
  readonly controls?: ConversationControlsView | null;
  readonly loading?: boolean;
  readonly error?: string | null;
  readonly onEdit?: () => void;
  readonly transport?: DesktopTransport;
  readonly conversationId?: string;
  readonly turnId?: string | null;
  readonly onOpenMemory?: (id: string) => void;
}) {
  const [basis, setBasis] = useState<UserResponseBasis | null>(null);
  const [basisLoading, setBasisLoading] = useState(false);
  const [basisError, setBasisError] = useState(false);
  const [basisEpoch, setBasisEpoch] = useState(0);
  useEffect(() => {
    let current = true;
    setBasis(null); setBasisError(false); setBasisLoading(false);
    if (!open || !transport || !conversationId || !turnId) return;
    setBasisLoading(true);
    void transport.getResponseBasis(conversationId, turnId).then(value => {
      if (value.conversation_id !== conversationId || value.turn_id !== turnId) throw new Error("scope mismatch");
      if (current) setBasis(value);
    }).catch(() => { if (current) setBasisError(true); })
      .finally(() => { if (current) setBasisLoading(false); });
    return () => { current = false; };
  }, [open, transport, conversationId, turnId, basisEpoch]);
  const values = controls?.controls;
  const depth = values?.reply_depth?.value;
  const follow = values?.follow_up?.value;
  const surface = values?.state_surface?.value;
  const memorySurface = typeof surface === "object" && surface !== null ? (surface as Record<string, unknown>).memory : surface;
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
          {loading ? <p role="status">正在读取这次聊天的设置…</p> : error || !values ? <p role="status">回应方式暂时无法确认，请打开聊天设置重新读取。</p> : <ul className="context-summary">
            <li><Check size={16} /> {values.response_mute?.value === true ? "已暂停回应" : depth === "brief" ? "短一点" : depth === "deep" ? "认真聊聊" : "自然回应"}</li>
            <li><Check size={16} /> {follow === "none" ? "不主动追问" : "可以问一句"}</li>
            <li><Check size={16} /> {memorySurface === "none" || memorySurface === "silent" ? "这次不使用过去的事" : memorySurface === "explicit" || memorySurface === "explicit_consent" ? "可以明确提起过去" : "过去的内容自然接上"}</li>
          </ul>}
          {onEdit && <button className="text-link" type="button" onClick={onEdit}>调整这次聊天</button>}
        </section>
        {transport && <NarrativePanel transport={transport} />}
        <section className="context-section">
          <h2>这条回应参考了什么</h2>
          <div className="context-empty">
            <Info size={17} />
            <div>{basisLoading ? <p role="status">正在核对这条回应的记录…</p> : basisError ? <p role="status">依据读取失败，不能据此判断没有使用过去的内容。</p> : !turnId ? <p>有已完成的回应后，可在消息旁查看它允许参考的内容。</p> : basis?.source === "generation_sealed" ? <><p>生成前实际提供了 {basis.recent_message_count ?? "数量未报告的"} 条近期消息。以下是当时提供、当前仍可披露的记忆摘要；这不代表每条都决定了回应措辞。</p>{basis.memories.map(item => <p key={item.memory_id}>{item.summary}{onOpenMemory && <button className="text-link" type="button" onClick={() => onOpenMemory(item.memory_id)}>查看这条记忆</button>}</p>)}{basis.memories.length === 0 && <p>没有当前可披露的记忆摘要。</p>}{!!basis.unavailable_memory_count && <p>另有 {basis.unavailable_memory_count} 条内容未披露或无法对应，不能解读为未曾提供。</p>}</> : <p>这条历史回应没有生成前依据记录，不能用当前召回或事后快照代替。</p>}{turnId && <button className="text-link" type="button" disabled={basisLoading} onClick={() => setBasisEpoch(value => value + 1)}>重新读取回应依据</button>}</div>
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
