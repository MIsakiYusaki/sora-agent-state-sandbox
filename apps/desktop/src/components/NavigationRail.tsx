import {
  BrainCircuit,
  History,
  MessageCircle,
  Settings,
  Sparkles,
} from "lucide-react";

export type SpaceId = "conversation" | "memory" | "settings";

export function NavigationRail({
  activeSpace,
  historyOpen,
  onNavigate,
  onHistory,
}: {
  readonly activeSpace: SpaceId;
  readonly historyOpen: boolean;
  readonly onNavigate: (space: SpaceId) => void;
  readonly onHistory: () => void;
}) {
  return (
    <nav className="rail" aria-label="主要导航">
      <button
        className="sora-mark"
        type="button"
        aria-label="回到对话"
        onClick={() => onNavigate("conversation")}
      >
        <Sparkles size={20} />
      </button>
      <div className="rail__group">
        <RailButton
          label="对话"
          active={activeSpace === "conversation" && !historyOpen}
          onClick={() => onNavigate("conversation")}
        >
          <MessageCircle size={19} />
        </RailButton>
        <RailButton label="历史" active={historyOpen} onClick={onHistory}>
          <History size={19} />
        </RailButton>
        <RailButton
          label="记忆与隐私"
          active={activeSpace === "memory"}
          onClick={() => onNavigate("memory")}
        >
          <BrainCircuit size={19} />
        </RailButton>
      </div>
      <div className="rail__group rail__group--bottom">
        <RailButton
          label="设置"
          active={activeSpace === "settings"}
          onClick={() => onNavigate("settings")}
        >
          <Settings size={19} />
        </RailButton>
      </div>
    </nav>
  );
}

function RailButton({
  label,
  active,
  onClick,
  children,
}: {
  readonly label: string;
  readonly active: boolean;
  readonly onClick: () => void;
  readonly children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={active ? "rail-button is-active" : "rail-button"}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      title={label}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
