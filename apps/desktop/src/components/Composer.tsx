import { LoaderCircle, PlugZap, RefreshCw, Send, ShieldAlert, Square } from "lucide-react";
import { useRef, type KeyboardEvent } from "react";

export type ComposerMode =
  | "ready"
  | "submitting"
  | "streaming"
  | "offline"
  | "provider_missing"
  | "repair_required";

interface ComposerProps {
  readonly value: string;
  readonly mode: ComposerMode;
  readonly onChange: (value: string) => void;
  readonly onSubmit: () => void;
  readonly onCancel: () => void;
  readonly onReconnect: () => void;
  readonly onConnectProvider: () => void;
}

const placeholders: Record<ComposerMode, string> = {
  ready: "和 Sora 说点什么…",
  submitting: "消息正在确认，你可以准备下一条",
  streaming: "可以继续写下一条，当前回复完成后再发送",
  offline: "可以继续写，连接恢复后由你确认发送",
  provider_missing: "可以先写下来，连接模型后继续",
  repair_required: "可以保留草稿，修复完成前不会发送",
};

export function Composer({
  value,
  mode,
  onChange,
  onSubmit,
  onCancel,
  onReconnect,
  onConnectProvider,
}: ComposerProps) {
  const composingRef = useRef(false);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !composingRef.current &&
      !event.nativeEvent.isComposing &&
      mode === "ready"
    ) {
      event.preventDefault();
      if (value.trim()) onSubmit();
    }
  };

  return (
    <div className={"composer composer--" + mode}>
      <textarea
        aria-label="给 Sora 发消息"
        aria-describedby="composer-status"
        value={value}
        rows={1}
        placeholder={placeholders[mode]}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={() => {
          composingRef.current = false;
        }}
      />
      <ComposerAction
        mode={mode}
        canSend={Boolean(value.trim())}
        onSubmit={onSubmit}
        onCancel={onCancel}
        onReconnect={onReconnect}
        onConnectProvider={onConnectProvider}
      />
      <span className="sr-only" id="composer-status" aria-live="polite">
        {mode === "offline"
          ? "当前离线，草稿会保留"
          : mode === "provider_missing"
            ? "尚未连接模型，草稿会保留"
            : mode === "repair_required"
              ? "当前需要修复，草稿会保留且不会发送"
            : mode === "submitting"
              ? "正在等待消息确认"
              : mode === "streaming"
                ? "Sora 正在回应"
                : "可以发送消息"}
      </span>
    </div>
  );
}

function ComposerAction({
  mode,
  canSend,
  onSubmit,
  onCancel,
  onReconnect,
  onConnectProvider,
}: {
  readonly mode: ComposerMode;
  readonly canSend: boolean;
  readonly onSubmit: () => void;
  readonly onCancel: () => void;
  readonly onReconnect: () => void;
  readonly onConnectProvider: () => void;
}) {
  if (mode === "submitting") {
    return (
      <button
        type="button"
        className="composer__action composer__action--text"
        aria-label="正在等待消息确认"
        disabled
      >
        <LoaderCircle size={16} className="spin" />
        等待确认
      </button>
    );
  }

  if (mode === "streaming") {
    return (
      <button
        type="button"
        className="composer__action composer__action--stop"
        aria-label="停止生成"
        title="停止生成"
        onClick={onCancel}
      >
        <Square size={15} fill="currentColor" />
      </button>
    );
  }

  if (mode === "offline") {
    return (
      <button
        type="button"
        className="composer__action composer__action--text"
        onClick={onReconnect}
      >
        <RefreshCw size={16} />
        重新连接
      </button>
    );
  }

  if (mode === "provider_missing") {
    return (
      <button
        type="button"
        className="composer__action composer__action--text"
        onClick={onConnectProvider}
      >
        <PlugZap size={16} />
        连接模型
      </button>
    );
  }

  if (mode === "repair_required") {
    return (
      <button
        type="button"
        className="composer__action composer__action--text"
        aria-label="需要修复后才能发送"
        disabled
      >
        <ShieldAlert size={16} />
        需要修复
      </button>
    );
  }

  return (
    <button
      type="button"
      className="composer__action composer__action--send"
      aria-label="发送"
      title="发送"
      disabled={!canSend}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onSubmit}
    >
      <Send size={18} />
    </button>
  );
}
