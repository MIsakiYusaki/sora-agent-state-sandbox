import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { DesktopAdminActionView, DesktopAdminPreviewRequest, DesktopAdminExecuteRequest, DesktopAdminRegistry } from "../contracts/generated";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import { AdminCommandPanel } from "./AdminCommandPanel";

afterEach(() => { cleanup(); vi.useRealTimers(); });

// Explicit UI unit fixtures. The production Mock transport cannot execute admin actions.
function fixture() {
  const transport = new MockDesktopTransport();
  const session = { schema_version: "DesktopDebugSession.v1" as const, session_id: "debug_fixture", window_label: "main" as const, expires_at_ms: Date.now() + 60_000, scopes: ["commands.read", "commands.maintenance"] };
  const registry: DesktopAdminRegistry = { schema_version: "DesktopAdminRegistry.v1", commands: [
    { command_id: "trace.index.sync", action_name: "sync_trace_index", description: "同步诊断索引", risk_level: "low", level: "L1", available: true, parameter_schema: { type: "object" }, default_params: { limit: 1 }, supports_dry_run: true },
    { command_id: "forget_memory", action_name: "forget_memory", description: "忘记记忆", risk_level: "high", level: "L3", available: false, unavailable_reason: "缺少目标修订契约，禁止执行。", parameter_schema: {}, default_params: {}, supports_dry_run: false },
  ], history: [] };
  const action: DesktopAdminActionView = { schema_version: "DesktopAdminActionView.v1", action_id: "qadmin_fixture", command_id: "trace.index.sync", params: { limit: 1 }, idempotency_key: "request_fixture", expected_revision: "a".repeat(64), execution_status: "previewed", risk_level: "low", active_turns: 0, impact_summary: { mutates: ["memory_trace_index"], limit: 1 }, rollback_type: null, result_summary: {}, audit_event_id: null, created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60_000).toISOString(), executed_at: null, dry_run: false };
  const open = vi.spyOn(transport, "openDebugSession").mockResolvedValue(session);
  const close = vi.spyOn(transport, "closeDebugSession").mockResolvedValue();
  const commands = vi.spyOn(transport, "getAdminCommands").mockResolvedValue(registry);
  const preview = vi.spyOn(transport, "previewAdminCommand").mockImplementation(async (request: DesktopAdminPreviewRequest) => ({ ...action, owned_by_current_client: true, params: request.params ?? {}, idempotency_key: request.idempotency_key }));
  const execute = vi.spyOn(transport, "executeAdminCommand").mockImplementation(async (request: DesktopAdminExecuteRequest) => ({ ...action, idempotency_key: request.idempotency_key, execution_status: "executed", audit_event_id: "qaudit_fixture", result_summary: { processed: 1 } }));
  const receipt = vi.spyOn(transport, "getAdminAction").mockResolvedValue({ ...action, execution_status: "execution_unknown", audit_event_id: "qaudit_fixture" });
  return { transport, session, registry, action, open, close, commands, preview, execute, receipt };
}

async function start(transport: MockDesktopTransport) {
  render(<AdminCommandPanel transport={transport} />);
  await userEvent.click(screen.getByRole("button", { name: "开启维护会话" }));
  await screen.findByRole("button", { name: "结束维护会话" });
}

it("requires a real preview and explicit impact confirmation, then displays the actual audit receipt", async () => {
  const f = fixture();
  await start(f.transport);
  const run = screen.getByRole("button", { name: "确认执行" });
  expect(run).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  expect(run).toBeDisabled();
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.dblClick(run);
  await waitFor(() => expect(f.execute).toHaveBeenCalledTimes(1));
  expect(f.execute.mock.calls[0][0]).toMatchObject({ action_id: f.action.action_id, expected_revision: f.action.expected_revision, confirmed: true });
  expect(await screen.findByText("审计 qaudit_fixture")).toBeInTheDocument();
  expect(run).toBeDisabled();
});

it("changing parameters invalidates the previous confirmation and request identity", async () => {
  const f = fixture();
  await start(f.transport);
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  await userEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByRole("textbox", { name: "命令参数" }), { target: { value: '{"limit":2}' } });
  expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await waitFor(() => expect(f.preview).toHaveBeenCalledTimes(2));
  expect(f.preview.mock.calls[1][0].idempotency_key).not.toEqual(f.preview.mock.calls[0][0].idempotency_key);
  expect(f.execute).not.toHaveBeenCalled();
});

it("uncertain execution offers receipt reads without resending the command", async () => {
  const f = fixture();
  f.execute.mockRejectedValue(new Error("private-error-sentinel"));
  await start(f.transport);
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认执行" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("勿重复执行或自动重发");
  expect(screen.queryByText(/private-error-sentinel/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "读取此回执" }));
  await waitFor(() => expect(f.receipt).toHaveBeenCalledWith(f.action.action_id));
  expect(f.execute).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
});

it("keeps unavailable data actions visible and disabled", async () => {
  const f = fixture();
  await start(f.transport);
  await userEvent.click(screen.getByRole("button", { name: /forget_memory/ }));
  expect(screen.getByText("缺少目标修订契约，禁止执行。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "预览影响" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
});

it("a known rejected request invalidates its preview without pretending it executed", async () => {
  const f = fixture();
  f.execute.mockRejectedValue({ code: "INVALID_REQUEST", safe_details: {} });
  await start(f.transport);
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认执行" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("执行未被接受");
  expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
  expect(f.execute).toHaveBeenCalledTimes(1);
});

it("an expired native session requires an explicit reopen", async () => {
  const f = fixture();
  f.execute.mockRejectedValue({ code: "INVALID_REQUEST", safe_details: { reason: "debug_session_expired" } });
  await start(f.transport);
  await userEvent.click(screen.getByRole("button", { name: "预览影响" }));
  await screen.findByText("真实影响预览");
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认执行" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("调试会话已失效");
  expect(screen.getByRole("button", { name: "开启维护会话" })).toBeEnabled();
  expect(f.open).toHaveBeenCalledTimes(1);
  expect(f.execute).toHaveBeenCalledTimes(1);
});

it("closes the exact late-opened session when the panel was already unmounted", async () => {
  const f = fixture();
  let resolve: (value: typeof f.session) => void = () => undefined;
  f.open.mockImplementation(() => new Promise((done) => { resolve = done; }));
  const view = render(<AdminCommandPanel transport={f.transport} />);
  fireEvent.click(screen.getByRole("button", { name: "开启维护会话" }));
  view.unmount();
  await act(async () => { resolve(f.session); });
  expect(f.close).toHaveBeenCalledWith(f.session.session_id);
  expect(f.commands).not.toHaveBeenCalled();
  expect(f.execute).not.toHaveBeenCalled();
});

it("expires a session without automatically reopening or executing", async () => {
  vi.useFakeTimers();
  const f = fixture();
  render(<AdminCommandPanel transport={f.transport} />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "开启维护会话" })); });
  await act(async () => { vi.advanceTimersByTime(60_001); });
  expect(screen.getByRole("alert")).toHaveTextContent("调试会话已过期");
  expect(f.open).toHaveBeenCalledTimes(1);
  expect(f.close).toHaveBeenCalledWith(f.session.session_id);
  expect(f.execute).not.toHaveBeenCalled();
});

it("the ordinary layout Mock cannot invent a successful maintenance session", async () => {
  render(<AdminCommandPanel transport={new MockDesktopTransport()} />);
  await userEvent.click(screen.getByRole("button", { name: "开启维护会话" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("维护会话未开启");
  expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
});
