import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { DesktopAdminActionView, DesktopAdminRegistry } from "../contracts/generated";
import type { MemoryQueuePage } from "../transport/DesktopTransport";
import { MemoryQueuePanel } from "./MemoryQueuePanel";

const row = { id: "idx-fixture", status: "dead_letter", attempts: 3, backend: "fts", target: null, action: "upsert", created_at: null, next_attempt_at: null, error_fingerprint: "error-fixture", error_code: "timeout", revision: "a".repeat(64), can_requeue: true, outcome: "unobserved" as const };
const page: MemoryQueuePage = { schema_version: "DesktopMemoryQueuePage.v1", kind: "index", truncated: false, items: [row] };

it("hydrates a single-event preview from the selected real revision and confirms through the maintenance session", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getMemoryQueue").mockResolvedValue(page);
  vi.spyOn(transport, "openDebugSession").mockResolvedValue({ schema_version: "DesktopDebugSession.v1", session_id: "debug-fixture", window_label: "main", expires_at_ms: Date.now() + 60_000, scopes: ["commands.maintenance"] });
  const registry: DesktopAdminRegistry = { schema_version: "DesktopAdminRegistry.v1", commands: [{ command_id: "memory.event.requeue", action_name: "requeue_memory_event", description: "重新入队单条失败或死信事件", risk_level: "medium", level: "L1", available: true, supports_dry_run: true, default_params: null, parameter_schema: { type: "object" } }], history: [] };
  vi.spyOn(transport, "getAdminCommands").mockResolvedValue(registry);
  const action: DesktopAdminActionView = { schema_version: "DesktopAdminActionView.v1", action_id: "admin-fixture", command_id: "memory.event.requeue", risk_level: "medium", idempotency_key: "request-fixture", params: { kind: "index", event_id: row.id, expected_revision: row.revision }, expected_revision: "b".repeat(64), impact_summary: { additional_attempts: 3 }, rollback_type: "inspect_queue_receipt", execution_status: "previewed", active_turns: 0, result_summary: {}, audit_event_id: null, created_at: "2026-11-06T15:00:00Z", expires_at: null, executed_at: null, dry_run: false, owned_by_current_client: true };
  const preview = vi.spyOn(transport, "previewAdminCommand").mockResolvedValue(action);
  const execute = vi.spyOn(transport, "executeAdminCommand").mockResolvedValue({ ...action, execution_status: "executed", result_summary: { status: "requeued", requeued_count: 1 }, audit_event_id: "audit-fixture" });
  render(<MemoryQueuePanel transport={transport} />);
  fireEvent.click(await screen.findByRole("button", { name: /fts · dead_letter/ }));
  fireEvent.click(screen.getByRole("button", { name: "预览此事件重新入队" }));
  fireEvent.click(screen.getByRole("button", { name: "开启维护会话" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "命令参数" })).toHaveValue(JSON.stringify(action.params, null, 2)));
  fireEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  expect(preview.mock.calls[0][0].params).toEqual(action.params);
  expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  expect(await screen.findByText(/事件已重新入队，等待消费者处理/)).toBeInTheDocument();
  expect(execute).toHaveBeenCalledOnce();
});

it("keeps legacy or unconfirmed rows read-only and reports policy rejection as a distinct outcome", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getMemoryQueue").mockResolvedValue({ ...page, items: [{ ...row, can_requeue: false, revision: null, outcome: "rejected_policy" }] });
  render(<MemoryQueuePanel transport={transport} />);
  fireEvent.click(await screen.findByRole("button", { name: /fts · dead_letter/ }));
  expect(screen.getByText("依据当前状态拒绝应用")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "预览此事件重新入队" })).not.toBeInTheDocument();
});
