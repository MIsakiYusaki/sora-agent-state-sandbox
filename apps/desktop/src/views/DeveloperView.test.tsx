import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import { DeveloperView } from "./DeveloperView";
import { TurnEvidencePanel } from "./TurnEvidence";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it.each([
  [{ index_dead_letter: 1 }, "存在死信", "red"],
  [{ projector_dead_letter: 1, index_failed: 2, index_pending: 3 }, "存在死信", "red"],
  [{ index_failed: 1 }, "存在失败", "red"],
  [{ projector_failed: 1 }, "存在失败", "red"],
  [{ index_pending: 1 }, "存在积压", "amber"],
  [{ projector_pending: 1 }, "存在积压", "amber"],
  [{ pending_extraction_jobs: 4 }, "水位为空", "mint"],
  [{ index_failed: null }, "无法判定", "neutral"],
  [{ projector_dead_letter: undefined }, "无法判定", "neutral"],
  [{ index_pending: 1, projector_dead_letter: null }, "存在积压 · 部分未观测", "amber"],
  [{ index_dead_letter: 1, projector_dead_letter: null }, "存在死信", "red"],
  [{ index_pending: -1 }, "无法判定", "neutral"],
] as const)("reports derived queue states from all counters: %j", async (changes, label, tone) => {
  const transport = new MockDesktopTransport();
  const snapshot = await transport.getDebugSnapshot("queue-status");
  vi.spyOn(transport, "getDebugSnapshot").mockResolvedValue({ ...snapshot, memory: {
    ...snapshot.memory, status: "error", pending_extraction_jobs: 0,
    index_pending: 0, projector_pending: 0, index_failed: 0, projector_failed: 0,
    index_dead_letter: 0, projector_dead_letter: 0, ...changes,
  } });
  const view = render(<DeveloperView conversationId="queue-status" events={[]} transport={transport} onExit={() => {}} />);
  await screen.findByText("真实运行快照已连接");
  fireEvent.click(screen.getByRole("button", { name: "记忆流水线" }));
  const queue = screen.getByRole("region", { name: "索引与投影队列状态" });
  expect(within(queue).getByText(label)).toHaveClass(`tag--${tone}`);
  const stages = [...view.container.querySelectorAll(".pipeline-stage")];
  expect(stages.find(stage => stage.textContent?.includes("索引与投影"))).toHaveTextContent(label);
  const store = stages.find(stage => stage.textContent?.includes("主存储"))!;
  expect(store).toHaveTextContent("已读取记录数量");
  expect(store).not.toHaveTextContent("异常");
  expect(screen.getByText("异常")).toBeInTheDocument();
  if (tone === "red") expect(stages.at(-1)?.querySelector(".pipeline-stage__status")).toHaveClass("is-error");
});

it("shows real memory retry state and attempt spans without treating dispatch as completion", () => {
  render(<TurnEvidencePanel execution={{}} legacy={{}} asyncExecutions={[]} postTurn={[]} modelCalls={[]} memoryJobs={{
    status: "available", jobs: [{ job_id: "xjob-test", status: "pending", attempt_count: 1,
      wait_reason: "retry_backoff", available_at_ms: 60000, error_code: "extractor_error:RuntimeError",
      checkpoint_available: false, attempts: [{ attempt: 1, outcome: "retry", error_code: "extractor_error:RuntimeError",
        execution: { snapshot_state: "final", spans: [{ span_id: "provider", parent_id: null,
          name: "memory.extractor.provider", status: "failed", start_offset_ms: 1, duration_ms: 12 }] } }],
    }],
  }} />);
  const panel = screen.getByRole("region", { name: "本轮记忆抽取作业" });
  expect(within(panel).getByText(/记忆抽取 1：待处理/)).toBeInTheDocument();
  expect(within(panel).getByText("失败后退避")).toBeInTheDocument();
  expect(within(panel).getByText("尝试 1：等待重试")).toBeInTheDocument();
  expect(within(panel).getByText("抽取模型调用（含内部修复）")).toBeInTheDocument();
  expect(within(panel).getByText("12 ms")).toBeInTheDocument();
  expect(within(panel).getAllByText("未观测").length).toBeGreaterThan(0);
});

it("distinguishes missing memory job bridge from an empty scoped queue", () => {
  const view = render(<TurnEvidencePanel execution={{}} legacy={{}} asyncExecutions={[]} postTurn={[]} modelCalls={[]} />);
  expect(screen.getByText("作业记录暂不可读取，不能判定后台已完成。")).toBeInTheDocument();
  view.rerender(<TurnEvidencePanel execution={{}} legacy={{}} asyncExecutions={[]} postTurn={[]} modelCalls={[]} memoryJobs={{ status: "available", jobs: [] }} />);
  expect(screen.getByText(/没有关联到本轮的抽取作业/)).toBeInTheDocument();
  expect(screen.queryByText("作业记录暂不可读取，不能判定后台已完成。")).not.toBeInTheDocument();
});

it("renders measured nested spans, filters identifiers and keeps unknown timing unknown", () => {
  render(<TurnEvidencePanel execution={{ snapshot_state: "final", spans: [
    { span_id: "root", parent_id: null, name: "runtime.execute", status: "completed", start_offset_ms: 0, duration_ms: 24, ended_at_ms: 24 },
    { span_id: "child", parent_id: "root", name: "memory.recall", status: "end_unobserved", start_offset_ms: 2, duration_ms: null, ended_at_ms: null },
  ] }} legacy={{ decisions: { recalled_item_count: null, growth_event_created: false, question_budget: 0 } }} asyncExecutions={[]} postTurn={[]} modelCalls={[]} />);
  expect(screen.getByText("记忆召回全程")).toBeInTheDocument();
  expect(screen.getByText(/\d{2}:\d{2}:\d{2}\.024/)).toBeInTheDocument();
  expect(screen.getByText("结束未观测")).toBeInTheDocument();
  expect(screen.queryByText("执行中")).not.toBeInTheDocument();
  const decisions = screen.getByRole("region", { name: "本轮决策证据" });
  expect(within(decisions).getByText("未观测")).toBeInTheDocument();
  expect(within(decisions).getByText("否")).toBeInTheDocument();
  expect(within(decisions).getByText("0")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("筛选阶段"), { target: { value: "memory" } });
  expect(screen.queryByText("运行时执行")).not.toBeInTheDocument();
  expect(screen.getByText("记忆召回全程")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("筛选阶段"), { target: { value: "does-not-exist" } });
  expect(screen.getByText("没有匹配的已采集阶段。")).toBeInTheDocument();
});

it("does not fabricate intervals or timestamps for legacy and background evidence", () => {
  const view = render(<TurnEvidencePanel execution={{}} legacy={{ durations: [{ name: "context_frames", duration_ms: 3 }] }} asyncExecutions={[{ attempt: 1, spans: [] }]} postTurn={[{ status: "delivered", attempts: 1, event_id: "test-event", consumers: [] }]} modelCalls={[]} />);
  expect(screen.getByText(/本轮没有精确区间记录/)).toBeInTheDocument();
  expect(screen.getByText(/消费执行记录.*未观测/)).toBeInTheDocument();
  expect(screen.getByText(/消费回执不等于抽取/)).toBeInTheDocument();
  expect(view.container.querySelector(".evidence-span-bar i")).toBeNull();
  expect(screen.queryByText(/Invalid Date|1970/)).not.toBeInTheDocument();
});

it("renders rule-backed signal decisions without inventing missing reasons", () => {
  render(<TurnEvidencePanel execution={{}} legacy={{ signal_decisions: [
    { source: "worldops", decision: "suppressed", reason_code: "world_lore_not_event" },
    { source: "memory", decision: "background", reason_code: null },
  ] }} asyncExecutions={[]} postTurn={[]} modelCalls={[]} />);
  fireEvent.click(screen.getByText("逐信号裁决（2）"));
  expect(screen.getByText("允许世界设定回答，但未命中具体事件")).toBeInTheDocument();
  expect(screen.getByText("理由未投影")).toBeInTheDocument();
});

it("keeps runtime sequences and clocks distinct from late Gateway events", async () => {
  const transport = new MockDesktopTransport();
  const base = await transport.getDebugSnapshot("one");
  vi.spyOn(transport, "getDebugSnapshot").mockResolvedValue({ ...base, timeline: { turns: [{
    turn_id: "turn-one", correlation_id: "corr-runtime", status: "committed", stage: "committed", terminal: true,
    created_at_ms: 1000, updated_at_ms: 1100, committed_at_ms: 1100, duration_ms: 100,
    events: [{ sequence: 1, event_type: "response.completed", created_at_ms: 1100, payload_fields: [] }],
  }] } });
  render(<DeveloperView conversationId="one" transport={transport} onExit={() => {}} events={[{
    turn_id: "turn-one", correlation_id: "corr-gateway", sequence: 1, event_type: "turn.accepted", created_at_ms: 100,
  }]} />);
  expect(await screen.findByText("原子提交完成", { selector: ".waterfall-name" })).toBeInTheDocument();
  expect(screen.queryByText("轮次接受", { selector: ".waterfall-name" })).not.toBeInTheDocument();
  expect(screen.getAllByText("100 ms").length).toBeGreaterThan(0);
});

it("refreshes real snapshots without overlapping requests and stops on exit", async () => {
  vi.useFakeTimers();
  const transport = new MockDesktopTransport();
  const snapshot = await transport.getDebugSnapshot("one");
  let resolve!: (value: typeof snapshot) => void;
  const read = vi.spyOn(transport, "getDebugSnapshot")
    .mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValue(snapshot);
  const view = render(<DeveloperView conversationId="one" events={[]} transport={transport} onExit={() => {}} />);
  await act(() => vi.advanceTimersByTimeAsync(6000));
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => resolve(snapshot));
  await act(() => vi.advanceTimersByTimeAsync(2000));
  expect(read).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/自动刷新，每次完成后间隔 2 秒/)).toBeInTheDocument();
  view.unmount();
  await act(() => vi.advanceTimersByTimeAsync(6000));
  expect(read).toHaveBeenCalledTimes(2);
});

it("labels retained diagnostics as stale when a refresh fails", async () => {
  vi.useFakeTimers();
  const transport = new MockDesktopTransport();
  const snapshot = await transport.getDebugSnapshot("one");
  vi.spyOn(transport, "getDebugSnapshot").mockResolvedValueOnce(snapshot)
    .mockRejectedValue(new Error("Gateway unavailable"));
  render(<DeveloperView conversationId="one" events={[]} transport={transport} onExit={() => {}} />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  await act(() => vi.advanceTimersByTimeAsync(2000));
  expect(screen.getByText(/不代表当前状态/)).toBeInTheDocument();
  expect(screen.getByText("快照读取失败")).toBeInTheDocument();
});

it("discards late responses from the previous conversation", async () => {
  const transport = new MockDesktopTransport();
  const first = { ...await transport.getDebugSnapshot("one"), profile_id_hash: "old-profile" };
  const second = { ...first, conversation_id: "two", profile_id_hash: "new-profile" };
  let resolve!: (value: typeof first) => void;
  vi.spyOn(transport, "getDebugSnapshot")
    .mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValue(second);
  const view = render(<DeveloperView conversationId="one" events={[]} transport={transport} onExit={() => {}} />);
  view.rerender(<DeveloperView conversationId="two" events={[]} transport={transport} onExit={() => {}} />);
  expect(await screen.findByText("new-profile")).toBeInTheDocument();
  await act(async () => resolve(first));
  expect(screen.queryByText("old-profile")).not.toBeInTheDocument();
});
