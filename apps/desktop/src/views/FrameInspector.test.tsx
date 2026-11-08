import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { DesktopDebugFrameDetail } from "../contracts/generated";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import { FrameInspector } from "./FrameInspector";

afterEach(cleanup);
const makeDetail = (turnId = "turn-one", value = 0): DesktopDebugFrameDetail => ({
  schema_version: "DesktopDebugFrameDetail.v1", profile_id_hash: "0123456789abcdef", conversation_id: "conversation-one", turn_id: turnId,
  correlation_id: "correlation-one", collected_at_ms: 10,
  observation: { active: false, expires_at_ms: null },
  projection: { schema_version: "DebugFrameProjection.v1", frame_id: "persona_frame", owner: "persona", source: "generation_sealed", captured_at_ms: 8,
    frame_schema_version: "PersonaFrame.v1", field_count: 4, protected_field_count: 1, projection_revision: "a".repeat(64), fields: [
      { path: "snapshot_version", state: "observed", kind: "scalar", value },
      { path: "growth_paused", state: "observed", kind: "scalar", value: false },
      { path: "trait_bands.warmth", state: "observed", kind: "scalar", value: null },
      { path: "trait_bands.caution", state: "missing", kind: "scalar", value: null },
    ] }, budget: { token_estimate: 0, reply_budget_tokens: null },
});
const props = { conversationId: "conversation-one", profileHash: "0123456789abcdef", turnId: "turn-one", correlationId: "correlation-one", frameId: "persona_frame", previousTurnId: null };

it("renders observed zero false and null without turning missing fields into zero", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getDebugFrame").mockResolvedValue(makeDetail());
  render(<FrameInspector transport={transport} {...props} />);
  expect(await screen.findByText("生成前已封印的实际输入", { selector: "p" })).toBeInTheDocument();
  const table = screen.getByRole("table");
  expect(within(table).getByText("0")).toBeInTheDocument();
  expect(within(table).getByText("否")).toBeInTheDocument();
  expect(within(table).getByText("已报告空值 (null)")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: "显示来源未包含的字段" }));
  expect(within(table).getByText("来源未包含该字段")).toBeInTheDocument();
  expect(transport.getDebugFrame).toHaveBeenCalledWith({ conversation_id: "conversation-one", turn_id: "turn-one", frame_id: "persona_frame" });
});

it("discards late replies from the previous turn and rejects mismatched identity", async () => {
  const transport = new MockDesktopTransport();
  let resolveOld!: (value: DesktopDebugFrameDetail) => void;
  vi.spyOn(transport, "getDebugFrame").mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; })).mockResolvedValue({ ...makeDetail("turn-two", 2), profile_id_hash: "another-profile" });
  const view = render(<FrameInspector transport={transport} {...props} />);
  view.rerender(<FrameInspector transport={transport} {...props} turnId="turn-two" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("身份不匹配");
  await act(async () => resolveOld(makeDetail()));
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("turn-one")).not.toBeInTheDocument();
});

it("compares only declared values and warns about differing capture stages", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getDebugFrame").mockImplementation(async request => request.turn_id === "turn-old"
    ? { ...makeDetail("turn-old", 9), projection: { ...makeDetail("turn-old", 9).projection, source: "delivery_snapshot" } }
    : makeDetail());
  render(<FrameInspector transport={transport} {...props} previousTurnId="turn-old" />);
  await screen.findByRole("table");
  fireEvent.click(screen.getByRole("checkbox", { name: "对照上一条已保存轮次" }));
  expect(await screen.findByText(/两条记录来源阶段不同/)).toBeInTheDocument();
  expect(screen.getByText("有变化")).toBeInTheDocument();
  expect(screen.getByText("9")).toBeInTheDocument();
});

it("honestly refuses an ordinary mock transport and never prints arbitrary exception text", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getDebugFrame").mockRejectedValue(new Error("private-token-sentinel"));
  render(<FrameInspector transport={transport} {...props} />);
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.queryByText(/private-token-sentinel/)).not.toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("does not let a late observation read overwrite an acknowledged enable", async () => {
  const transport = new MockDesktopTransport();
  let oldRead!: (value: { active: boolean; expires_at_ms: number | null }) => void;
  vi.spyOn(transport, "getFrameObservation").mockImplementation(() => new Promise(resolve => { oldRead = resolve; }));
  vi.spyOn(transport, "setFrameObservation").mockResolvedValue({ active: true, expires_at_ms: Date.now() + 300_000 });
  render(<FrameInspector transport={transport} {...props} turnId={null} frameId={null} />);
  fireEvent.click(screen.getByRole("button", { name: "开启新轮次帧采集（5 分钟）" }));
  await screen.findByRole("button", { name: "停止新轮次帧采集" });
  await act(async () => oldRead({ active: false, expires_at_ms: null }));
  expect(screen.getByRole("button", { name: "停止新轮次帧采集" })).toBeInTheDocument();
});

it("uncertain mutation clears the previous claim and retains an explicit stop action", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getFrameObservation").mockResolvedValue({ active: false, expires_at_ms: null });
  const write = vi.spyOn(transport, "setFrameObservation").mockRejectedValue(new Error("private-operation-error"));
  render(<FrameInspector transport={transport} {...props} turnId={null} frameId={null} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "开启新轮次帧采集（5 分钟）" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未获确认");
  expect(screen.getByText(/不能据此认定已经停止/)).toBeInTheDocument();
  write.mockResolvedValue({ active: false, expires_at_ms: null });
  fireEvent.click(screen.getByRole("button", { name: "停止帧采集（状态未确认）" }));
  await act(async () => {});
  expect(write.mock.calls.map(call => call[0])).toEqual([true, false]);
  expect(screen.queryByText(/private-operation-error/)).not.toBeInTheDocument();
});
