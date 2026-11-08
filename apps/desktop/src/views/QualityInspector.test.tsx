import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { QualityPage, QualityRequest, QualityRow } from "../transport/DesktopTransport";
import { QualityInspector } from "./QualityInspector";

afterEach(() => vi.useRealTimers());
const row = (id: string, label: string, status = "passed"): QualityRow => ({ id, label, status, created_at: null, revision: "a".repeat(64), related_run_id: null });
const run = row("run_fixture", "eval"), baseline = { ...row("baseline_fixture", "quality_baseline", "active"), related_run_id: run.id }, event = row("event_fixture", "test_failure", "open");
const page = (request: QualityRequest): QualityPage => ({ schema_version: "DesktopQualityPage.v1", kind: request.kind,
  items: [request.kind === "runs" ? run : request.kind === "baselines" ? baseline : event], truncated: false,
  selected: request.record_id ? (request.kind === "runs" ? run : request.kind === "baselines" ? baseline : event) : null,
  cases: request.kind === "runs" && request.record_id ? [{ id: `case_${request.case_offset ?? 0}`, suite: "fixture_suite", name: `case_name_${request.case_offset ?? 0}`, status: "failed", severity: "error", failure_category: "regression", metrics: { accuracy: 0 } }] : null,
  case_count: request.record_id && request.kind === "runs" ? 2 : null, next_case_offset: request.record_id && request.kind === "runs" && !request.case_offset ? 100 : null,
  summary: request.record_id ? { passed_cases: 0 } : {}, comparison: request.baseline_id ? { status: "passed", baseline_id: baseline.id, regression_count: 0, unaccepted_regression_count: 0, baseline_pass_rate: 0, current_pass_rate: 0 } : null,
});
function fixture(expires = Date.now() + 300000) {
  const transport = new MockDesktopTransport();
  const open = vi.spyOn(transport, "openQualitySession").mockResolvedValue({ schema_version: "DesktopDebugSession.v1", session_id: "debug_quality", window_label: "main", expires_at_ms: expires, scopes: ["quality.read", "quality.resolve"] });
  const inspect = vi.spyOn(transport, "inspectQuality").mockImplementation(async request => page(request));
  const close = vi.spyOn(transport, "closeDebugSession").mockResolvedValue();
  const resolve = vi.spyOn(transport, "resolveQualityEvent").mockResolvedValue({ event_id: event.id, status: "resolved", audit_event_id: "audit_fixture" });
  return { transport, open, inspect, close, resolve };
}
async function enter() {
  fireEvent.click(screen.getByRole("button", { name: "开启质量治理会话（5 分钟）" }));
  await screen.findByRole("button", { name: /eval/ });
}
async function selectEvent() {
  fireEvent.click(screen.getByRole("button", { name: "质量事件" }));
  fireEvent.click(await screen.findByRole("button", { name: /test_failure/ }));
  await screen.findByRole("checkbox");
}

it("requires explicit session, reads real cases and zero metrics, paginates and compares without a release-success claim", async () => {
  const f = fixture(); const view = render(<QualityInspector transport={f.transport} />);
  expect(f.inspect).not.toHaveBeenCalled();
  await enter();
  fireEvent.click(screen.getByRole("button", { name: /eval/ }));
  expect(await screen.findByText("fixture_suite / case_name_0")).toBeInTheDocument();
  const detail = screen.getByRole("region", { name: "所选质量记录" });
  expect(within(detail).getAllByText("0").length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText("对比基线"), { target: { value: baseline.id } });
  fireEvent.click(screen.getByRole("button", { name: "读取基线对比" }));
  expect(await screen.findByText("基线比较：通过")).toBeInTheDocument();
  expect(screen.getByText(/不代表整体发布验收通过/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "加载更多用例" }));
  expect(await screen.findByText("fixture_suite / case_name_100")).toBeInTheDocument();
  expect(screen.getByText("fixture_suite / case_name_0")).toBeInTheDocument();
  expect(f.inspect).toHaveBeenCalledWith({ kind: "runs", record_id: run.id, baseline_id: baseline.id, case_offset: 100 });
  view.unmount(); expect(f.close).toHaveBeenCalledWith("debug_quality");
});

it("requires per-event confirmation, resets on decision changes and prevents duplicate submission", async () => {
  const f = fixture(); let complete!: (value: Awaited<ReturnType<typeof f.transport.resolveQualityEvent>>) => void;
  f.resolve.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  render(<QualityInspector transport={f.transport} />); await enter(); await selectEvent();
  expect(screen.getByRole("button", { name: "确认结案" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("质量事件结案决定"), { target: { value: "false_positive" } });
  expect(screen.getByRole("button", { name: "确认结案" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  const confirm = screen.getByRole("button", { name: "确认结案" }); fireEvent.click(confirm); fireEvent.click(confirm);
  expect(f.resolve).toHaveBeenCalledExactlyOnceWith({ event_id: event.id, expected_revision: event.revision, resolution: "false_positive", confirmed: true });
  await act(async () => complete({ event_id: event.id, status: "resolved", audit_event_id: "audit_fixture" }));
  expect(await screen.findByText(/审计回执 audit_fixture/)).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});

it("reports unknown mutation outcome, clears actionable stale detail and never retries automatically", async () => {
  const f = fixture(); f.resolve.mockRejectedValue(new Error("private-error-sentinel"));
  render(<QualityInspector transport={f.transport} />); await enter(); await selectEvent();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "确认结案" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未获得完整确认");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByText(/private-error-sentinel/)).not.toBeInTheDocument();
  expect(f.resolve).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "重新读取质量记录" }));
  expect(await screen.findByRole("button", { name: /test_failure/ })).toBeInTheDocument();
  expect(f.resolve).toHaveBeenCalledOnce();
});

it("does not turn a failed read into empty records or keep stale actions", async () => {
  const f = fixture(); render(<QualityInspector transport={f.transport} />); await enter();
  f.inspect.mockRejectedValue(new Error("private-read-sentinel"));
  fireEvent.click(screen.getByRole("button", { name: /eval/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("当前结果未确认");
  expect(screen.queryByText("当前查询确认没有记录。")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "所选质量记录" })).not.toBeInTheDocument();
});

it("expires the session and discards a late detail response", async () => {
  vi.useFakeTimers();
  const f = fixture(Date.now() + 10000); render(<QualityInspector transport={f.transport} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "开启质量治理会话（5 分钟）" })));
  let complete!: (value: QualityPage) => void;
  f.inspect.mockImplementation(request => request.kind === "runs" ? new Promise(resolve => { complete = resolve; }) : Promise.resolve(page(request)));
  fireEvent.click(screen.getByRole("button", { name: /eval/ }));
  await act(async () => { await vi.advanceTimersByTimeAsync(10001); });
  await act(async () => complete(page({ kind: "runs", record_id: run.id })));
  expect(screen.getByRole("alert")).toHaveTextContent("已到期");
  expect(screen.queryByRole("region", { name: "所选质量记录" })).not.toBeInTheDocument();
  expect(f.close).toHaveBeenCalledWith("debug_quality");
});

it("revokes a session that opens after its panel unmounts", async () => {
  const f = fixture(); let complete!: (value: Awaited<ReturnType<typeof f.transport.openQualitySession>>) => void;
  f.open.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  const view = render(<QualityInspector transport={f.transport} />);
  fireEvent.click(screen.getByRole("button", { name: "开启质量治理会话（5 分钟）" })); view.unmount();
  await act(async () => complete({ schema_version: "DesktopDebugSession.v1", session_id: "debug_late", expires_at_ms: Date.now() + 300000, window_label: "main", scopes: [] }));
  expect(f.close).toHaveBeenCalledWith("debug_late"); expect(f.inspect).not.toHaveBeenCalled();
});

it("rejects mismatched record details", async () => {
  const f = fixture(); render(<QualityInspector transport={f.transport} />); await enter();
  f.inspect.mockImplementation(async request => ({ ...page(request), selected: { ...run, id: "foreign_record" } }));
  fireEvent.click(screen.getByRole("button", { name: /eval/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未确认");
  expect(screen.queryByText("foreign_record")).not.toBeInTheDocument();
});

it("drops the previous profile state when the transport changes", async () => {
  const f = fixture(), other = fixture(); const view = render(<QualityInspector transport={f.transport} />); await enter();
  view.rerender(<QualityInspector transport={other.transport} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "开启质量治理会话（5 分钟）" })).toBeEnabled());
  expect(screen.queryByRole("button", { name: /eval/ })).not.toBeInTheDocument();
  expect(f.close).toHaveBeenCalledWith("debug_quality"); expect(other.inspect).not.toHaveBeenCalled();
});
