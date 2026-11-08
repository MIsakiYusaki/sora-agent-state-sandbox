import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer, type ComposerMode } from "./Composer";

function renderComposer(mode: ComposerMode) {
  const onCancel = vi.fn();
  const onSubmit = vi.fn();
  render(<Composer value="保留这份草稿" mode={mode} onChange={vi.fn()} onSubmit={onSubmit}
    onCancel={onCancel} onReconnect={vi.fn()} onConnectProvider={vi.fn()}
    controlLabel="自然回应" followUpLabel="可以问一句" onOpenControls={vi.fn()} />);
  return { onCancel, onSubmit, input: screen.getByRole("textbox", { name: "给 Sora 发消息" }) };
}

describe("Composer keyboard stop", () => {
  it("routes Escape to the existing cancel action and preserves the draft", () => {
    const { input, onCancel, onSubmit } = renderComposer("streaming");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input).toHaveValue("保留这份草稿");
    expect(screen.getByRole("button", { name: "停止生成" })).toHaveAttribute("aria-keyshortcuts", "Escape");
  });

  it.each<ComposerMode>(["ready", "submitting", "offline", "provider_missing", "repair_required"])(
    "does not submit or cancel from Escape when %s", (mode) => {
      const { input, onCancel, onSubmit } = renderComposer(mode);
      fireEvent.keyDown(input, { key: "Escape" });
      expect(onCancel).not.toHaveBeenCalled();
      expect(onSubmit).not.toHaveBeenCalled();
    },
  );

  it("does not treat IME dismissal as cancellation", () => {
    const { input, onCancel } = renderComposer("streaming");
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Escape", isComposing: true });
    expect(onCancel).not.toHaveBeenCalled();
  });
});
