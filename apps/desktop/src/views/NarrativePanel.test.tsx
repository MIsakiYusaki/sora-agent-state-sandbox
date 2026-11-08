import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesktopTransport } from "../transport";
import type { NarrativeActionRequest, NarrativeActionResult, NarrativeView } from "../transport/narrative";
import { NarrativePanel } from "./NarrativePanel";

afterEach(cleanup);
const baseline: NarrativeView = { schema_version: "DesktopNarrative.v1", source: "local_preview", available: true, world_name: "澄空域", bundle_id: "qa.bundle", canon_version: "1.1.0", truncated: false, unavailable_reason: null,
  events: [{ id: "qa-instance", event_id: "qa-event", revision: 2, title: "星灯节前夜", summary: "与 Sora 一起选择一句星灯留言。", state: "invited", updated_at: "2026-11-08T00:00:00Z", selected_choice: null, choices: [{ id: "safe_return", label: "愿每个赶路的人，都有一盏回家的灯。" }, { id: "defer", label: "改天再选" }], can_invite: false, tasks: [{ id: "message", title: "选定一句留言", completed: false }], memory_state: "not_requested", history: [] }] };
function api(methods: Record<string, unknown> = {}) { return { getNarrative: vi.fn().mockResolvedValue(baseline), narrativeAction: vi.fn(), ...methods } as unknown as DesktopTransport; }
function result(request: NarrativeActionRequest): NarrativeActionResult {
  return { receipt: { schema_version: "WorldOpsUserActionReceipt.v1", request_id: request.request_id, instance_id: request.instance_id, event_id: "qa-event", revision: 4, state: "completed", timeline_revision: 4, correlation_id: "qa-correlation", llm_called: false }, duration_ms: 1,
    view: { ...baseline, events: [{ ...baseline.events[0], state: "completed", choices: [], tasks: [{ id: "message", title: "选定一句留言", completed: true }], selected_choice: "愿每个赶路的人，都有一盏回家的灯。", memory_state: "needs_review" }] } };
}

describe("真实故事选择", () => {
  it("仅在用户展开时读取，并明确本机开发内容来源", async () => {
    const user = userEvent.setup(); const transport = api();
    render(<NarrativePanel transport={transport} />);
    expect(transport.getNarrative).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "打开故事与经历" }));
    expect(await screen.findByText(/当前为本机开发预览故事/)).toBeInTheDocument();
    expect(transport.narrativeAction).not.toHaveBeenCalled();
  });

  it("选择携带实际修订与签名包版本，回执前不显示任务完成，连点不重复", async () => {
    const user = userEvent.setup(); let finish!: (value: NarrativeActionResult) => void; let sent!: NarrativeActionRequest;
    const transport = api({ narrativeAction: vi.fn((request: NarrativeActionRequest) => { sent = request; return new Promise<NarrativeActionResult>(resolve => { finish = resolve; }); }) });
    render(<NarrativePanel transport={transport} />);
    await user.click(screen.getByRole("button", { name: "打开故事与经历" }));
    await user.click(await screen.findByText("星灯节前夜"));
    await user.dblClick(screen.getByRole("button", { name: "愿每个赶路的人，都有一盏回家的灯。" }));
    expect(transport.narrativeAction).toHaveBeenCalledTimes(1);
    expect(sent).toMatchObject({ instance_id: "qa-instance", expected_revision: 2, bundle_id: "qa.bundle", canon_version: "1.1.0", action: "choice", choice_id: "safe_return" });
    expect(screen.getByText("尚未完成：选定一句留言")).toBeInTheDocument();
    await act(async () => finish(result(sent)));
    expect(screen.getByText("已完成：选定一句留言")).toBeInTheDocument();
    expect(screen.getByText("共同记忆等待审核。")).toBeInTheDocument();
    expect(screen.queryByText("共同记忆已保存。")).not.toBeInTheDocument();
  });

  it("未知结果清除可执行目标，重新读取而不自动重发", async () => {
    const user = userEvent.setup(); const transport = api({ narrativeAction: vi.fn().mockRejectedValue(new Error("unknown")) });
    render(<NarrativePanel transport={transport} />);
    await user.click(screen.getByRole("button", { name: "打开故事与经历" }));
    await user.click(await screen.findByText("星灯节前夜"));
    await user.click(screen.getByRole("button", { name: "改天再选" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不会自动重发");
    expect(screen.queryByRole("button", { name: "改天再选" })).not.toBeInTheDocument();
    expect(transport.narrativeAction).toHaveBeenCalledTimes(1);
  });

  it("读取失败与签名失败均不能冒充空故事或成功选择", async () => {
    const user = userEvent.setup(); const transport = api({ getNarrative: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ ...baseline, available: false, events: [], unavailable_reason: "content_verification_failed" }) });
    render(<NarrativePanel transport={transport} />);
    await user.click(screen.getByRole("button", { name: "打开故事与经历" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败");
    await user.click(screen.getByRole("button", { name: "重新读取故事" }));
    expect(await screen.findByText("故事内容未通过当前可信校验，暂时不能参与。")).toBeInTheDocument();
    expect(transport.narrativeAction).not.toHaveBeenCalled();
  });

  it("回执目标不属于当前请求时拒绝采用结果", async () => {
    const user = userEvent.setup(); const transport = api({ narrativeAction: vi.fn((request: NarrativeActionRequest) => Promise.resolve({ ...result(request), receipt: { ...result(request).receipt, instance_id: "foreign-instance" } })) });
    render(<NarrativePanel transport={transport} />);
    await user.click(screen.getByRole("button", { name: "打开故事与经历" }));
    await user.click(await screen.findByText("星灯节前夜"));
    await user.click(screen.getByRole("button", { name: "愿每个赶路的人，都有一盏回家的灯。" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未能确认");
    expect(screen.queryByText("已完成：选定一句留言")).not.toBeInTheDocument();
  });
});
