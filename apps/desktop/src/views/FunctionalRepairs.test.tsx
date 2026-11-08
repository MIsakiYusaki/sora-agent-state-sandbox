import { createRef } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ThreadDrawer } from "../components/ThreadDrawer";
import { ContextDrawer } from "../components/ContextDrawer";
import { MessageList } from "../components/MessageList";
import { ControlsView } from "./ControlsView";
import { MemoryView } from "./MemoryView";
import { HistoryView } from "./HistoryView";
import { FrameInspector } from "./FrameInspector";
import { MemoryGovernancePanel } from "./MemoryGovernancePanel";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { GovernancePage } from "../transport/DesktopTransport";
import { UserMemoryDetails } from "../components/UserMemoryDetails";
import { MemoryQueuePanel } from "./MemoryQueuePanel";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const conversations = ["甲", "乙"].map((name, index) => ({ conversation_id: `repair-${index}`, title: `对话${name}`, preview: `摘要${name}`, created_at_ms: 1000, updated_at_ms: 2000, message_count: 2, conversation_version: 1 }));
const controls = { schema_version: "ConversationControls.v1" as const, conversation_id: "repair-0", controls: { reply_depth: { value: "deep", scope: "session" }, follow_up: { value: "none", scope: "session" }, state_surface: { value: { memory: "none" }, scope: "session" } }, initiative_lease: null };
const memory = { id: "repair-memory", type: "preference", summary: "测试偏好摘要", tags: [], confidence: 0.8, sensitivity: "low", evidence_count: 1, surface_permission: "implicit", visible: true, lifecycle: {}, created_at: null, updated_at: null };
const memoryPage = { schema_version: "UserMemoryPage.v1" as const, items: [memory], summary: { available: 1, visible: 1, hidden: 0, archived: 0 } };

it("F01-F03 filters sidebar rows, focuses search and opens real menu destinations", () => {
  const settings = vi.fn(), history = vi.fn();
  render(<ThreadDrawer open modal={false} conversations={conversations} activeConversationId="repair-0" loading={false} triggerRef={createRef()} onClose={() => {}} onNew={() => {}} onSelect={() => {}} onOpenSettings={settings} onOpenHistory={history} />);
  const search = screen.getByLabelText("搜索对话");
  fireEvent.change(search, { target: { value: "对话乙" } });
  expect(screen.queryByText("对话甲")).not.toBeInTheDocument();
  fireEvent.keyDown(document, { key: "k", ctrlKey: true });
  expect(search).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "管理当前数据空间" }));
  expect(settings).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByLabelText("更多操作：对话乙"));
  fireEvent.click(screen.getByRole("button", { name: "查看历史" }));
  expect(history).toHaveBeenCalledWith("repair-1");
});

it("F04 shows real controls and does not claim a failed read is the default", () => {
  const view = render(<ContextDrawer open modal={false} triggerRef={createRef()} onClose={() => {}} controls={controls} />);
  expect(screen.getByText("认真聊聊")).toBeInTheDocument();
  expect(screen.getByText("这次不使用过去的事")).toBeInTheDocument();
  view.rerender(<ContextDrawer open modal={false} triggerRef={createRef()} onClose={() => {}} controls={null} error="读取失败" />);
  expect(screen.getByRole("status")).toHaveTextContent("无法确认");
  expect(screen.queryByText("自然回应")).not.toBeInTheDocument();
});

it("F06 uses actual saved message dates", () => {
  render(<MessageList activeTurn={null} messages={[{ id: "old", turnId: "old", role: "user", content: "旧消息", sequence: 1, createdAtMs: Date.UTC(2000, 0, 1), delivery: "committed" }]} />);
  expect(screen.queryByText("今天")).not.toBeInTheDocument();
  expect(screen.getByText("2000年1月1日")).toBeInTheDocument();
});

it("F07 blocks writes without state and keeps the confirmed selection after failed save", async () => {
  const update = vi.fn().mockRejectedValue(new Error("private-failure-sentinel"));
  const view = render(<ControlsView controls={null} loading={false} error="读取失败" onBack={() => {}} onUpdate={update} />);
  expect(screen.queryByText("已保存")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "短一点" })).toBeDisabled();
  view.rerender(<ControlsView controls={controls} loading={false} error={null} onBack={() => {}} onUpdate={update} />);
  fireEvent.click(screen.getByRole("button", { name: "短一点" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("尚未确认保存");
  expect(screen.getByRole("button", { name: "认真聊聊" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByText(/private-failure-sentinel/)).not.toBeInTheDocument();
});

it("F08/F12/F13 closes and reopens detail, and reports mutation failure visibly", async () => {
  render(<MemoryView page={memoryPage} loading={false} error={null} onVisibility={async () => { throw new Error("private"); }} onForget={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
  expect(screen.queryByText("Sora 记下的是")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /测试偏好摘要/ }));
  fireEvent.click(screen.getByRole("button", { name: "不在对话中使用" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未获得完整确认");
});

it("F12 does not replace unread memory counts with an empty store", () => {
  render(<MemoryView page={null} loading={false} error="读取失败" onVisibility={async () => {}} onForget={async () => {}} />);
  expect(screen.getByText("记忆数量未确认")).toBeInTheDocument();
  expect(screen.queryByText("0 条记忆")).not.toBeInTheDocument();
  expect(screen.queryByText("Sora 还没有保存可显示的长期记忆。")).not.toBeInTheDocument();
});

it("F09/F11 never pairs filtered conversation headings with old content and keeps all loaded preview messages", () => {
  render(<HistoryView conversations={conversations} selectedId="repair-0" loading={false} preview={Array.from({ length: 12 }, (_, sequence) => ({ role: "user" as const, content: `甲正文${sequence}`, turn_id: `turn-${sequence}`, sequence, created_at_ms: 1000 }))} onSelect={() => {}} onContinue={() => {}} onNew={() => {}} />);
  expect(screen.getByText("甲正文0")).toBeInTheDocument();
  expect(screen.getByText("甲正文11")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索对话"), { target: { value: "对话乙" } });
  expect(screen.queryByRole("heading", { name: "对话乙" })).not.toBeInTheDocument();
  expect(screen.queryByText("甲正文0")).not.toBeInTheDocument();
});

it("F14 can stop observation without loaded frame details and expires the visible state", async () => {
  const transport = new MockDesktopTransport();
  const observe = vi.spyOn(transport, "setFrameObservation").mockImplementation(async active => ({ active, expires_at_ms: active ? Date.now() + 300_000 : null }));
  render(<FrameInspector transport={transport} conversationId="test" profileHash="test" turnId={null} correlationId={null} frameId={null} previousTurnId={null} />);
  fireEvent.click(screen.getByRole("button", { name: "开启新轮次帧采集（5 分钟）" }));
  fireEvent.click(await screen.findByRole("button", { name: "停止新轮次帧采集" }));
  await waitFor(() => expect(observe.mock.calls.map(call => call[0])).toEqual([true, false]));
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole("button", { name: "开启新轮次帧采集（5 分钟）" }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTime(300_001));
  expect(screen.getByRole("button", { name: "开启新轮次帧采集（5 分钟）" })).toBeInTheDocument();
});

function reviewFixture() {
  const transport = new MockDesktopTransport();
  const session = { schema_version: "DesktopDebugSession.v1" as const, session_id: "debug_review_fixture", window_label: "main" as const, expires_at_ms: Date.now() + 300_000, scopes: ["memory.review.read", "memory.review.resolve"] };
  const page: GovernancePage = { schema_version: "DesktopGovernancePage.v1", truncated: false, items: [{ id: "review-one", kind: "reviews", status: "open", memory_type: "preference", summary: "合成低敏偏好", summary_state: "available", reason_codes: ["insufficient_confidence"], created_at: null, evidence_count: 1, revision: "a".repeat(64), can_approve: true }] };
  const open = vi.spyOn(transport, "openReviewSession").mockResolvedValue(session);
  const close = vi.spyOn(transport, "closeDebugSession").mockResolvedValue();
  const read = vi.spyOn(transport, "getMemoryGovernance").mockResolvedValue(page);
  const resolve = vi.spyOn(transport, "resolveMemoryReview").mockResolvedValue({ schema_version: "DesktopReviewReceipt.v1", review_id: "review-one", status: "approved", resolution: "approved", resolved_at: "2026-10-15T12:00:00Z" });
  return { transport, session, page, open, close, read, resolve };
}

it("F15 requires explicit session and per-record confirmation, then reads a real receipt and revokes on exit", async () => {
  const f = reviewFixture();
  const view = render(<MemoryGovernancePanel transport={f.transport} />);
  expect(f.read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" }));
  fireEvent.click(await screen.findByRole("button", { name: /合成低敏偏好/ }));
  const submit = screen.getByRole("button", { name: "确认批准" });
  expect(submit).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(submit);
  expect(await screen.findByText(/审核记录 review-one/)).toBeInTheDocument();
  expect(f.resolve).toHaveBeenCalledWith({ review_id: "review-one", action: "approve", expected_revision: "a".repeat(64), confirmed: true });
  expect(f.read).toHaveBeenCalledTimes(2);
  view.unmount();
  expect(f.close).toHaveBeenCalledWith(f.session.session_id);
});

it("F15 read failure cannot masquerade as no pending reviews or leak exception content", async () => {
  const f = reviewFixture();
  f.read.mockRejectedValue(new Error("private-review-sentinel"));
  render(<MemoryGovernancePanel transport={f.transport} />);
  fireEvent.click(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("治理记录读取失败");
  expect(screen.queryByText("当前查询确实没有记录。")).not.toBeInTheDocument();
  expect(screen.queryByText(/private-review-sentinel/)).not.toBeInTheDocument();
  expect(f.resolve).not.toHaveBeenCalled();
});

it("F16 protected records cannot be approved from their metadata", async () => {
  const f = reviewFixture();
  f.read.mockResolvedValue({ ...f.page, items: [{ ...f.page.items[0], summary: null, summary_state: "protected", can_approve: false }] });
  render(<MemoryGovernancePanel transport={f.transport} />);
  fireEvent.click(screen.getByRole("button", { name: "开启记忆审核会话（5 分钟）" }));
  fireEvent.click(await screen.findByRole("button", { name: /受保护的内容/ }));
  expect(screen.getByRole("option", { name: "批准" })).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "审核决定" })).toHaveValue("reject");
  expect(f.resolve).not.toHaveBeenCalled();
});

it("F26 saves defaults only after explicit consent and excludes pause from persistent preferences", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ControlsView controls={controls} loading={false} error={null} onBack={() => {}} onUpdate={async () => {}} onDefaults={save} />);
  fireEvent.click(screen.getByRole("button", { name: "设为我的默认方式" }));
  const panel = screen.getByRole("region", { name: "确认默认聊天方式" });
  expect(within(panel).getByRole("button", { name: "确认保存默认方式" })).toBeDisabled();
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(within(panel).getByRole("checkbox"));
  fireEvent.click(within(panel).getByRole("button", { name: "确认保存默认方式" }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save).toHaveBeenCalledWith({ action: "save", confirmed: true, reply_depth: "deep", follow_up: "none", topic_shift: "same_topic", memory_mode: "off" });
  expect(await screen.findByText(/已保存为这个数据空间的默认方式/)).toBeInTheDocument();
});

it("F27 tonight quiet writes a real duration control rather than muting reactive replies", async () => {
  const update = vi.fn().mockResolvedValue(undefined);
  render(<ControlsView controls={controls} loading={false} error={null} onBack={() => {}} onUpdate={update} />);
  fireEvent.click(screen.getByRole("button", { name: /今晚不主动提醒/ }));
  await waitFor(() => expect(update).toHaveBeenCalledOnce());
  expect(update.mock.calls[0][0]).toMatchObject({ control: "quiet", value: true, scope: "duration" });
  expect(update.mock.calls[0][0].duration_seconds).toBeGreaterThan(0);
  expect(update.mock.calls[0][0].duration_seconds).toBeLessThanOrEqual(86400);
});

it("F14 reentry reads active observation even without any saved frame", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getFrameObservation").mockResolvedValue({ active: true, expires_at_ms: Date.now() + 300000 });
  render(<FrameInspector transport={transport} conversationId="test" profileHash="test" turnId={null} correlationId={null} frameId={null} previousTurnId={null} />);
  expect(await screen.findByRole("button", { name: "停止新轮次帧采集" })).toBeInTheDocument();
});

it("F05 renders only the selected response basis and discards a late previous-turn reply", async () => {
  const transport = new MockDesktopTransport();
  let oldReply!: (value: Awaited<ReturnType<typeof transport.getResponseBasis>>) => void;
  const read = vi.spyOn(transport, "getResponseBasis").mockImplementationOnce(() => new Promise(resolve => { oldReply = resolve; })).mockResolvedValue({ schema_version: "UserResponseBasis.v1", conversation_id: "one", turn_id: "two", source: "unobserved", recent_message_count: null, memories: [], unavailable_memory_count: null });
  const view = render(<ContextDrawer open modal={false} triggerRef={createRef()} onClose={() => {}} controls={controls} transport={transport} conversationId="one" turnId="one" />);
  view.rerender(<ContextDrawer open modal={false} triggerRef={createRef()} onClose={() => {}} controls={controls} transport={transport} conversationId="one" turnId="two" />);
  expect(await screen.findByText(/这条历史回应没有生成前依据记录/)).toBeInTheDocument();
  await act(async () => oldReply({ schema_version: "UserResponseBasis.v1", conversation_id: "one", turn_id: "one", source: "generation_sealed", recent_message_count: 2, memories: [{ memory_id: "old", summary: "旧回应的来源摘要" }], unavailable_memory_count: 0 }));
  expect(screen.queryByText("旧回应的来源摘要")).not.toBeInTheDocument();
  expect(read).toHaveBeenCalledTimes(2);
});

it("F29 uses the read revision and explicit confirmation for a real correction request", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getMemoryDetail").mockResolvedValue({ schema_version: "UserMemoryDetail.v1", memory, revision: "c".repeat(64), can_correct: true, sources: [{ kind: "manual", created_at: null, conversation_id: null }] });
  const corrected = { ...memory, id: "new-memory", summary: "纠正后的摘要" };
  const correct = vi.spyOn(transport, "correctMemory").mockResolvedValue({ memory: corrected });
  const refresh = vi.fn().mockResolvedValue(undefined);
  render(<UserMemoryDetails memory={memory} transport={transport} onCorrected={refresh} />);
  fireEvent.click(await screen.findByRole("button", { name: "纠正记下的内容" }));
  fireEvent.change(screen.getByLabelText("正确的记忆摘要"), { target: { value: corrected.summary } });
  expect(screen.getByRole("button", { name: "确认纠错" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "确认纠错" }));
  await waitFor(() => expect(correct).toHaveBeenCalledWith(memory.id, { summary: corrected.summary, expected_revision: "c".repeat(64), confirmed: true }));
  expect(refresh).toHaveBeenCalledWith(corrected);
});

it("F17 queue read failures do not report empty and loading rows never executes maintenance", async () => {
  const transport = new MockDesktopTransport();
  const read = vi.spyOn(transport, "getMemoryQueue").mockResolvedValue({ schema_version: "DesktopMemoryQueuePage.v1", kind: "index", items: [{ id: "index-event", status: "pending", attempts: 2, backend: "vector", target: null, action: "upsert", created_at: null, next_attempt_at: null, error_code: "timeout", error_fingerprint: "abc123" }], truncated: false });
  const execute = vi.spyOn(transport, "executeAdminCommand");
  render(<MemoryQueuePanel transport={transport} />);
  fireEvent.click(await screen.findByRole("button", { name: /vector · pending/ }));
  expect(screen.getByText("abc123")).toBeInTheDocument();
  expect(execute).not.toHaveBeenCalled();
  read.mockRejectedValue(new Error("private-event-sentinel"));
  fireEvent.click(screen.getByRole("button", { name: "重新读取事件" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("不能据此认为队列为空");
  expect(screen.queryByText(/private-event-sentinel/)).not.toBeInTheDocument();
  expect(execute).not.toHaveBeenCalled();
});
