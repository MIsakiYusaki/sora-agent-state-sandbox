import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MemoryGovernancePanel } from "./MemoryGovernancePanel";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { GovernancePage } from "../transport/DesktopTransport";

const page: GovernancePage = { schema_version: "DesktopGovernancePage.v1", truncated: false, items: [{ id: "quarantine_fixture", kind: "quarantines", status: "active", memory_type: "preference", summary: null, summary_state: "protected", reason_codes: ["gate_quarantine"], created_at: null, evidence_count: 1, revision: "a".repeat(64), can_approve: false, can_dismiss: true, severity: "high", risk_type: "privacy" }] };
function fixture() {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "openReviewSession").mockResolvedValue({ schema_version: "DesktopDebugSession.v1", session_id: "debug_quarantine", window_label: "main", expires_at_ms: Date.now() + 300000, scopes: ["memory.review.read", "memory.quarantine.resolve"] });
  const list = vi.spyOn(transport, "getMemoryGovernance").mockResolvedValue(page);
  const dismiss = vi.spyOn(transport, "dismissQuarantine").mockResolvedValue({ status: "dismissed", quarantine_id: "quarantine_fixture", audit_id: "gate_audit_fixture", resolved_at: "2026-10-28T12:00:00Z" });
  return { transport, list, dismiss };
}
async function select() {
  fireEvent.click(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" }));
  fireEvent.click(await screen.findByRole("button", { name: /受保护的内容/ }));
  await screen.findByRole("checkbox");
}

it("requires explicit per-record confirmation, shows the disposition audit and never offers protected approval", async () => {
  const f = fixture(); render(<MemoryGovernancePanel transport={f.transport} initialKind="quarantines" />); await select();
  const button = screen.getByRole("button", { name: "确认拒绝候选并结束隔离" }); expect(button).toBeDisabled();
  expect(screen.getByText("privacy")).toBeInTheDocument(); expect(screen.queryByLabelText("审核决定")).not.toBeInTheDocument();
  f.list.mockResolvedValue({ ...page, items: [] });
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(button); fireEvent.click(button);
  expect(await screen.findByText(/审计 gate_audit_fixture/)).toBeInTheDocument();
  expect(f.dismiss).toHaveBeenCalledExactlyOnceWith({ quarantine_id: "quarantine_fixture", expected_revision: "a".repeat(64), confirmed: true });
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});

it("clears unconfirmed disposition state and does not automatically retry", async () => {
  const f = fixture(); f.dismiss.mockRejectedValue(new Error("private-error-sentinel"));
  render(<MemoryGovernancePanel transport={f.transport} initialKind="quarantines" />); await select();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "确认拒绝候选并结束隔离" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未获得完整确认");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument(); expect(f.dismiss).toHaveBeenCalledOnce();
  expect(screen.queryByText(/private-error-sentinel/)).not.toBeInTheDocument();
});

it("does not attach a late review session from a previous transport to the current profile", async () => {
  const old = fixture(), next = fixture(); let complete!: (value: Awaited<ReturnType<typeof old.transport.openReviewSession>>) => void;
  vi.spyOn(old.transport, "closeDebugSession");
  vi.mocked(old.transport.openReviewSession).mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  const view = render(<MemoryGovernancePanel transport={old.transport} initialKind="quarantines" />);
  fireEvent.click(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" }));
  view.rerender(<MemoryGovernancePanel transport={next.transport} initialKind="quarantines" />);
  await act(async () => complete({ schema_version: "DesktopDebugSession.v1", session_id: "debug_old_profile", expires_at_ms: Date.now() + 300000, window_label: "main", scopes: [] }));
  expect(old.transport.closeDebugSession).toHaveBeenCalledWith("debug_old_profile");
  expect(next.list).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" })).toBeEnabled();
});
