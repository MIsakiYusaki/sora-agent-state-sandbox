import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import type { DesktopDebugSnapshot, DesktopStatus, HistoryPage, ShellError, TurnAccepted } from "./contracts/generated";
import { MockDesktopTransport } from "./transport/MockDesktopTransport";
import type { DesktopEventListener } from "./transport/DesktopTransport";
import type {
  ConversationControlUpdate,
  ConversationControlsView,
  UserMemoryPage,
  UserMemoryView,
} from "./transport";

const readyStatus: DesktopStatus = {
  schema_version: "DesktopStatus.v1",
  phase: "ready",
  profile_id: "local",
  client_instance_id: "desktop_test",
  protocol_version: "1.0",
  gateway_ready: true,
  provider_ready: true,
  recoverable: true,
  status_message: null,
};

class FixedStatusTransport extends MockDesktopTransport {
  constructor(private readonly fixedStatus: DesktopStatus) {
    super();
  }

  override async status(): Promise<DesktopStatus> {
    return this.fixedStatus;
  }
}

class FailOnceTransport extends MockDesktopTransport {
  private attempts = 0;

  override async status(): Promise<DesktopStatus> {
    this.attempts += 1;
    if (this.attempts === 1) throw new Error("temporary startup failure");
    return readyStatus;
  }
}

class FeatureTransport extends MockDesktopTransport {
  readonly controlUpdates: ConversationControlUpdate[] = [];
  readonly visibilityUpdates: Array<{ memoryId: string; visible: boolean }> = [];
  readonly forgottenMemoryIds: string[] = [];
  private featureMemories: UserMemoryView[] = [
    {
      id: "memory_preference_1",
      type: "preference",
      summary: "你不喜欢被连续追问，更喜欢把一件事慢慢说完。",
      tags: ["聊天节奏"],
      confidence: 0.92,
      sensitivity: "low",
      evidence_count: 3,
      surface_permission: "auto",
      visible: true,
      lifecycle: { tier: "warm" },
      created_at: "2026-08-20T08:00:00Z",
      updated_at: "2026-08-30T08:00:00Z",
    },
  ];

  override async listMemories(): Promise<UserMemoryPage> {
    return this.memoryPage();
  }

  override async setMemoryVisibility(memoryId: string, visible: boolean): Promise<UserMemoryView> {
    this.visibilityUpdates.push({ memoryId, visible });
    const index = this.featureMemories.findIndex((memory) => memory.id === memoryId);
    if (index < 0) throw new Error("memory not found");
    const updated = {
      ...this.featureMemories[index],
      visible,
      surface_permission: visible ? "auto" : "none",
    };
    this.featureMemories = this.featureMemories.map((memory) => memory.id === memoryId ? updated : memory);
    return updated;
  }

  override async forgetMemory(memoryId: string): Promise<void> {
    this.forgottenMemoryIds.push(memoryId);
    this.featureMemories = this.featureMemories.filter((memory) => memory.id !== memoryId);
  }

  override async updateConversationControl(
    conversationId: string,
    request: ConversationControlUpdate,
  ): Promise<ConversationControlsView> {
    this.controlUpdates.push(request);
    return super.updateConversationControl(conversationId, request);
  }

  private memoryPage(): UserMemoryPage {
    return {
      schema_version: "UserMemoryPage.v1",
      items: this.featureMemories,
      summary: {
        available: this.featureMemories.length,
        visible: this.featureMemories.filter((memory) => memory.visible).length,
        hidden: this.featureMemories.filter((memory) => !memory.visible).length,
        archived: 0,
      },
    };
  }
}

class EmptyFirstRunTransport extends FixedStatusTransport {
  constructor() {
    super({
      ...readyStatus,
      phase: "provider_setup_required",
      provider_ready: false,
      status_message: "请先连接一个可用模型",
    });
  }

  override async listConversations() {
    return { schema_version: "ConversationIndex.v1" as const, items: [] };
  }

  override async loadHistory(request: Parameters<MockDesktopTransport["loadHistory"]>[0]): Promise<HistoryPage> {
    return {
      schema_version: "HistoryPage.v1",
      protocol_version: "1.0",
      conversation_id: request.conversation_id,
      conversation_version: 0,
      items: [],
      next_before_sequence: null,
    };
  }
}

class VersionConflictTransport extends MockDesktopTransport {
  override async submitTurn(): Promise<TurnAccepted> {
    const error: ShellError = {
      schema_version: "ShellError.v1",
      code: "TURN_CONFLICT",
      message: "conversation changed",
      retryable: false,
      retry_after_ms: null,
      correlation_id: "0123456789abcdef0123456789abcdef",
      action: "reload_conversation",
      safe_details: { current_version: 7, expected_version: 6 },
    };
    throw error;
  }
}

class UnobservedDebugTransport extends MockDesktopTransport {
  override async getDebugSnapshot(conversationId: string): Promise<DesktopDebugSnapshot> {
    const snapshot = await super.getDebugSnapshot(conversationId);
    return {
      ...snapshot,
      routes: { status: "degraded", registry_revision: null, binding_count: null, bindings: null },
      systems: snapshot.systems.map((item) => item.subsystem_id === "model_provider" ? {
        ...item,
        status: "degraded",
        details: { conversation_ready: null, missing_conversation_workloads: null },
      } : item),
      memory: {
        status: "degraded",
        active_memories: null,
        pending_candidates: null,
        open_reviews: null,
        active_quarantines: null,
        pending_extraction_jobs: null,
        index_pending: null,
        index_failed: null,
        index_dead_letter: null,
        projector_pending: null,
        projector_failed: null,
        projector_dead_letter: null,
      },
      async_work: {
        pending: null,
        failed: null,
        dead_letter: null,
        memory: { status: "degraded", index_pending: null, projector_pending: null },
        affect: { status: "degraded", outbox_pending: null },
        relationship: { status: "degraded", pending: null },
        worldops: { status: "degraded", outbox_pending: null },
      },
      quality: {
        status: "degraded",
        eval_suite_count: null,
        quality_run_count: null,
        baseline_count: null,
        open_event_count: null,
        admin_action_count: null,
        eval_suites: null,
        latest_runs: null,
        baselines: null,
        open_events: null,
        admin_actions: null,
      },
    };
  }
}

afterEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
  window.localStorage.clear();
});

describe("Desktop App", () => {
  it("keeps identity and privacy recovery reachable when a pending erasure blocks history", async () => {
    class ErasureRecoveryTransport extends FixedStatusTransport {
      override async loadHistory(): Promise<HistoryPage> { throw new Error("profile erasure pending"); }
      override async getPrivacyState() {
        return { schema_version: "DesktopPrivacyState.v1" as const, profile_id: "fixture-erasure", space_name: "隔离空间", revision: "a".repeat(64), pending_operation: "erase_" + "1".repeat(32), last_completed_operation: null };
      }
    }
    const transport = new ErasureRecoveryTransport({ ...readyStatus, phase: "degraded", profile_id: "fixture-erasure", gateway_ready: false, provider_ready: false, status_message: "空间删除尚未完成" });
    const user = userEvent.setup();
    render(<App transport={transport} />);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "给 Sora 发消息" })).toHaveAttribute("placeholder", "可以继续写，连接恢复后由你确认发送"));
    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(screen.getByRole("button", { name: "数据与隐私" }));
    expect(await screen.findByRole("button", { name: "继续恢复删除" })).toBeDisabled();
    expect(screen.getByText(/此空间已暂停打开/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出记忆摘要" })).toBeDisabled();
  });

  it("shows a safe recovery path when the Gateway disappears during a turn", async () => {
    class InterruptedTransport extends MockDesktopTransport {
      callback: DesktopEventListener | undefined;
      submits = 0;
      override async subscribe(listener: DesktopEventListener) {
        this.callback = listener;
        return () => { this.callback = undefined; };
      }
      override async submitTurn(): Promise<TurnAccepted> {
        this.submits += 1;
        return {
          schema_version: "TurnAccepted.v1", protocol_version: "1.0",
          turn_id: "turn_crashed", request_id: "request_crashed",
          conversation_id: "conversation_main", correlation_id: "0123456789abcdef0123456789abcdef",
          status: "accepted", conversation_version: 0, events_url: "/v1/turns/turn_crashed/events",
        };
      }
    }
    const transport = new InterruptedTransport();
    const user = userEvent.setup();
    render(<App transport={transport} />);
    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "和 Sora 说点什么…"));
    await user.type(composer, "故障前的合成问题{enter}");
    await waitFor(() => expect(composer).toHaveValue(""));
    act(() => transport.callback?.({
      kind: "shell_error", conversation_id: "conversation_main", turn_id: "turn_crashed",
      payload: {
        schema_version: "ShellError.v1", code: "GATEWAY_UNAVAILABLE",
        message: "Project Sora Gateway is unavailable", retryable: true,
        retry_after_ms: null, correlation_id: null, action: "restart_gateway", safe_details: {},
      },
    }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("对话连接已中断");
    expect(alert).not.toHaveTextContent("Project Sora Gateway");
    expect(screen.getByText("本轮未完成")).toBeInTheDocument();
    expect(composer).toHaveValue("故障前的合成问题");
    expect(transport.submits).toBe(1);
    await user.click(screen.getByRole("button", { name: "重新载入" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(composer).toHaveValue("故障前的合成问题");
    expect(transport.submits).toBe(1);
  });

  it("announces a failure before the first delta, marks the turn and restores its draft", async () => {
    class FailedResponseTransport extends MockDesktopTransport {
      callback: DesktopEventListener | undefined;
      submits = 0;
      override async subscribe(listener: DesktopEventListener) {
        this.callback = listener;
        return () => { this.callback = undefined; };
      }
      override async submitTurn(): Promise<TurnAccepted> {
        this.submits += 1;
        return {
          schema_version: "TurnAccepted.v1", protocol_version: "1.0",
          turn_id: "turn_incomplete", request_id: "request_incomplete",
          conversation_id: "conversation_main", correlation_id: "0123456789abcdef0123456789abcdef",
          status: "accepted", conversation_version: 0, events_url: "/v1/turns/turn_incomplete/events",
        };
      }
    }
    const transport = new FailedResponseTransport();
    const user = userEvent.setup();
    render(<App transport={transport} />);
    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "和 Sora 说点什么…"));
    await user.type(composer, "请把路线说完整{enter}");
    await waitFor(() => expect(composer).toHaveValue(""));
    act(() => transport.callback?.({
      kind: "gateway_event", conversation_id: "conversation_main", turn_id: "turn_incomplete",
      payload: {
        schema_version: "GatewayEvent.v1", protocol_version: "1.0", turn_id: "turn_incomplete",
        sequence: 3, event_type: "turn.failed", created_at_ms: Date.now(), replayed: false,
        correlation_id: "0123456789abcdef0123456789abcdef",
        payload: { error_code: "IncompleteModelResponse" },
      },
    }));
    expect(await screen.findByRole("alert")).toHaveTextContent("这次回复没有完整生成");
    expect(composer).toHaveValue("请把路线说完整");
    expect(screen.getByText("本轮未完成")).toBeInTheDocument();
    expect(screen.queryByText("Sora 正在回应")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^发送$/ })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "关闭错误提示" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(composer).toHaveValue("请把路线说完整");
    expect(transport.submits).toBe(1);
  });

  it("renders the real conversation surface and sends text with Enter", async () => {
    const user = userEvent.setup();
    render(<App transport={new MockDesktopTransport()} />);
    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "和 Sora 说点什么…"));
    expect(screen.getByRole("button", { name: "对话历史" })).toBeEnabled();
    await user.type(composer, "今天有点累{enter}");
    const feed = screen.getByRole("feed", { name: "当前对话" });
    const sentMessage = within(feed).getByText("今天有点累");
    expect(sentMessage).toBeInTheDocument();
    expect(sentMessage.closest("article")).toHaveClass("message--user");
    expect(await within(feed).findByText(/今天消耗了不少/)).toBeInTheDocument();
  });

  it("opens the real model settings page without rendering an API key input in WebView", async () => {
    const user = userEvent.setup();
    render(<App transport={new MockDesktopTransport()} />);
    await user.click(screen.getByRole("button", { name: "设置" }));
    expect(await screen.findByRole("heading", { name: "模型与连接" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Windows 原生安全窗口录入/)).toBeInTheDocument();
    const modelSelect = screen.getByRole("combobox", { name: "模型 ID" });
    expect(within(modelSelect).getByRole("option", { name: "DeepSeek V4 Flash" })).toBeInTheDocument();
    expect(within(modelSelect).getByRole("option", { name: "DeepSeek V4 Pro" })).toBeInTheDocument();
    expect(within(modelSelect).queryByRole("option", { name: "DeepSeek Chat" })).not.toBeInTheDocument();
  });

  it("keeps the composer editable when a provider is missing and opens settings", async () => {
    const user = userEvent.setup();
    const transport = new FixedStatusTransport({
      ...readyStatus,
      phase: "provider_setup_required",
      provider_ready: false,
      status_message: "请先连接一个可用模型",
    });
    render(<App transport={transport} />);

    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    const connect = await screen.findByRole("button", { name: "连接模型" });
    expect(composer).toBeEnabled();
    await user.type(composer, "先把这句话记在草稿里");
    expect(composer).toHaveValue("先把这句话记在草稿里");
    await user.click(connect);
    expect(await screen.findByRole("heading", { name: "模型与连接" })).toBeInTheDocument();
  });

  it("keeps the composer editable while the gateway is restarting", async () => {
    const transport = new FixedStatusTransport({
      ...readyStatus,
      phase: "restarting",
      gateway_ready: false,
      status_message: "正在恢复对话服务",
    });
    render(<App transport={transport} />);

    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    expect(await screen.findByRole("button", { name: "重新连接" })).toBeInTheDocument();
    expect(composer).toBeEnabled();
  });

  it("preserves the draft when startup is retried", async () => {
    const user = userEvent.setup();
    render(<App transport={new FailOnceTransport()} />);

    await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    expect(await screen.findByText(/暂时无法载入对话/)).toBeInTheDocument();
    const offlineComposer = screen.getByRole("textbox", { name: "给 Sora 发消息" });
    await user.type(offlineComposer, "启动失败也不要丢掉这句话");
    expect(offlineComposer).toHaveValue("启动失败也不要丢掉这句话");
    await user.click(screen.getByRole("button", { name: "重新连接" }));
    await waitFor(() => {
      const restoredComposer = screen.getByRole("textbox", { name: "给 Sora 发消息" });
      expect(restoredComposer).toHaveAttribute("placeholder", "和 Sora 说点什么…");
      expect(restoredComposer).toHaveValue("启动失败也不要丢掉这句话");
    });
  });

  it("closes a compact history drawer with Escape and restores focus", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 600 });
    const user = userEvent.setup();
    render(<App transport={new MockDesktopTransport()} />);

    const trigger = screen.getByRole("button", { name: "展开对话列表" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "会话历史" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "会话历史" })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("loads the real conversation index without exposing fake branches or internal versions", async () => {
    const user = userEvent.setup();
    render(<App transport={new MockDesktopTransport()} />);

    await user.click(screen.getByRole("button", { name: "历史" }));
    expect(await screen.findByRole("heading", { name: "对话历史" })).toBeInTheDocument();
    expect(screen.getAllByText("当前对话")).toHaveLength(2);
    expect(screen.queryByText(/分支接口/)).not.toBeInTheDocument();
    expect(screen.queryByText(/版本 \d/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续对话" })).toBeEnabled();
  });

  it("hides, restores, and forgets a real user memory through the transport", async () => {
    const user = userEvent.setup();
    const transport = new FeatureTransport();
    render(<App transport={transport} />);

    await user.click(screen.getByRole("button", { name: "记忆与隐私" }));
    expect(await screen.findByRole("heading", { name: "记忆与隐私" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /你不喜欢被连续追问/ }));
    expect(screen.queryByText(/可信度|敏感级别|生命周期/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "不在对话中使用" }));
    await waitFor(() => expect(transport.visibilityUpdates).toEqual([
      { memoryId: "memory_preference_1", visible: false },
    ]));
    expect(await screen.findByRole("button", { name: "恢复用于对话" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "忘记这件事" }));
    const forgetDialog = await screen.findByRole("dialog", { name: "让 Sora 忘记这件事？" });
    expect(forgetDialog).toHaveAccessibleDescription("删除请求会沿记忆系统传播到相关索引与投影。");
    expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "让 Sora 忘记这件事？" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "忘记这件事" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "忘记这件事" }));
    await user.click(screen.getByRole("button", { name: "确认忘记" }));
    await waitFor(() => expect(transport.forgottenMemoryIds).toEqual(["memory_preference_1"]));
    expect(await screen.findByText("Sora 还没有保存可显示的长期记忆。")).toBeInTheDocument();
  });

  it("maps natural conversation controls to session-scoped runtime contracts", async () => {
    const user = userEvent.setup();
    const transport = new FeatureTransport();
    render(<App transport={transport} />);

    await user.click(screen.getByRole("button", { name: "本轮对话控制" }));
    expect(await screen.findByRole("heading", { name: "这次想怎么聊" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "别追问" }));
    await user.click(screen.getByRole("button", { name: "这次不用" }));

    await waitFor(() => expect(transport.controlUpdates).toEqual([
      { control: "follow_up", value: "none", scope: "session" },
      { control: "state_surface", value: { memory: "none" }, scope: "session" },
    ]));
    expect(screen.getByText(/不追问/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回对话" })).toBeEnabled();
  });

  it("keeps all six settings sections and exposes Developer Mode only after opt-in", async () => {
    const user = userEvent.setup();
    render(<App transport={new MockDesktopTransport()} />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
    for (const label of ["模型与供应商", "Sora 与我", "外观", "通知", "数据与隐私", "高级"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(screen.getByRole("region", { name: "Windows 通知偏好" })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("通知偏好或系统状态读取失败，当前状态未确认");
    expect(screen.queryByRole("checkbox", { name: "允许在后台完成回应时提示我" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "高级" }));
    expect(screen.getByText(/查看真实运行快照和当前会话事件/)).toBeInTheDocument();
    const developerSwitch = screen.getByRole("switch", { name: "调试模式" });
    expect(developerSwitch).toHaveAttribute("aria-checked", "false");
    await user.click(developerSwitch);
    expect(developerSwitch).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "打开" }));

    expect(await screen.findByText("真实运行快照已连接")).toBeInTheDocument();
    expect(screen.getByText("mock-local-profile")).toBeInTheDocument();
    expect(screen.queryByText(/^local$/)).not.toBeInTheDocument();
    for (const label of ["单轮时间线", "上下文帧", "模型路由", "记忆流水线", "异步任务", "状态系统", "评测与发布", "受控控制台"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "单轮时间线" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "单轮时间线" })).toBeInTheDocument();

    const expectedPanels = [
      ["上下文帧", "上下文帧"],
      ["模型路由", "模型路由"],
      ["记忆流水线", "记忆流水线"],
      ["异步任务", "异步任务"],
      ["状态系统", "状态系统"],
      ["评测与发布", "评测与发布门禁"],
      ["受控控制台", "受控控制台"],
    ] as const;
    for (const [button, heading] of expectedPanels) {
      await user.click(screen.getByRole("button", { name: button }));
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(screen.getByText("尚未读取维护历史，不显示模拟输出。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开启维护会话" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认执行" })).toBeDisabled();
    expect(screen.queryByText(/\$0\.|1\.82 s|发布通过/)).not.toBeInTheDocument();
  });

  it("renders missing production diagnostics as unobserved instead of zero or success", async () => {
    const user = userEvent.setup();
    render(<App transport={new UnobservedDebugTransport()} />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(screen.getByRole("button", { name: "高级" }));
    await user.click(screen.getByRole("switch", { name: "调试模式" }));
    await user.click(screen.getByRole("button", { name: "打开" }));

    await user.click(await screen.findByRole("button", { name: "模型路由" }));
    expect(screen.getByText("路由状态未观测")).toBeInTheDocument();
    expect(screen.getByText("当前无法判定路由完整性")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "记忆流水线" }));
    expect(screen.getByText("水位未观测")).toBeInTheDocument();
    expect(screen.getByText("无法判定")).toBeInTheDocument();
    expect(screen.queryByText("水位为空")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "异步任务" }));
    expect(screen.getByText("失败水位未观测")).toBeInTheDocument();
    expect(screen.queryByText("无失败积压")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "评测与发布" }));
    expect(screen.getByText("运行记录未观测")).toBeInTheDocument();
    expect(screen.getByText(/质量运行读取失败或未报告/)).toBeInTheDocument();
  });

  it("maps an empty provider setup state to an honest first-run surface", async () => {
    render(<App transport={new EmptyFirstRunTransport()} />);

    expect(await screen.findByRole("heading", { name: "先连接一个模型服务" })).toBeInTheDocument();
    expect(screen.getByText(/Windows 原生安全窗口录入/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "连接模型" })).toBeEnabled();
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
  });

  it("preserves the draft and reloads authoritative history after a version conflict", async () => {
    const user = userEvent.setup();
    render(<App transport={new VersionConflictTransport()} />);

    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "和 Sora 说点什么…"));
    await user.type(composer, "这句话不能被另一个窗口覆盖{enter}");
    expect(await screen.findByText("对话版本有更新。")).toBeInTheDocument();
    expect(screen.getByText(/当前窗口版本 6，本机最新版本 7/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "需要修复后才能发送" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "给 Sora 发消息" })).toHaveValue("这句话不能被另一个窗口覆盖");

    await user.click(screen.getByRole("button", { name: "重新载入最新内容" }));
    await waitFor(() => {
      const restoredComposer = screen.getByRole("textbox", { name: "给 Sora 发消息" });
      expect(restoredComposer).toHaveAttribute("placeholder", "和 Sora 说点什么…");
      expect(restoredComposer).toHaveValue("这句话不能被另一个窗口覆盖");
    });
  });

  it("enters a write-blocked safe degradation state when recovery is not allowed", async () => {
    const transport = new FixedStatusTransport({
      ...readyStatus,
      phase: "degraded",
      gateway_ready: false,
      recoverable: false,
      status_message: null,
    });
    render(<App transport={transport} />);

    expect(await screen.findByText(/只读安全降级状态/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "需要修复后才能发送" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "给 Sora 发消息" })).toBeEnabled();
  });
});
