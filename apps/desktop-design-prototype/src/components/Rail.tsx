import {
  BookOpen,
  Brain,
  Bug,
  History,
  MessageCircle,
  Moon,
  Settings,
  Sun,
} from "lucide-react";
import type { SpaceId } from "../data";
import { IconButton } from "./ui";

const spaces: ReadonlyArray<{ id: SpaceId; label: string; icon: typeof MessageCircle }> = [
  { id: "conversation", label: "对话", icon: MessageCircle },
  { id: "history", label: "历史", icon: History },
  { id: "memory", label: "记忆与隐私", icon: Brain },
];

export function Rail({
  active,
  theme,
  developerEnabled,
  onNavigate,
  onTheme,
}: {
  active: SpaceId;
  theme: "light" | "dark";
  developerEnabled: boolean;
  onNavigate: (space: SpaceId) => void;
  onTheme: () => void;
}) {
  return (
    <nav className="rail" aria-label="主导航">
      <button className="sora-mark" type="button" aria-label="回到对话" onClick={() => onNavigate("conversation")}>
        <span className="sora-mark__star">✦</span>
      </button>
      <div className="rail__group">
        {spaces.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={`rail-button ${active === id ? "is-active" : ""}`}
            type="button"
            aria-label={label}
            title={label}
            onClick={() => onNavigate(id)}
          >
            <Icon size={19} strokeWidth={1.8} />
          </button>
        ))}
      </div>
      <div className="rail__group rail__group--bottom">
        {developerEnabled && (
          <button
            className={`rail-button rail-button--developer ${active === "developer" ? "is-active" : ""}`}
            type="button"
            aria-label="调试模式"
            title="调试模式"
            onClick={() => onNavigate("developer")}
          >
            <Bug size={18} />
          </button>
        )}
        <IconButton label={theme === "light" ? "切换到深色模式" : "切换到浅色模式"} onClick={onTheme}>
          {theme === "light" ? <Moon size={18} /> : <Sun size={18} />}
        </IconButton>
        <button
          className={`rail-button ${active === "settings" ? "is-active" : ""}`}
          type="button"
          aria-label="设置"
          title="设置"
          onClick={() => onNavigate("settings")}
        >
          <Settings size={19} />
        </button>
      </div>
      <span className="rail__brand" aria-hidden="true"><BookOpen size={13} /></span>
    </nav>
  );
}
