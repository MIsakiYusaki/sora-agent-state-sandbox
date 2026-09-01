import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import type { DesktopStatus, HistoryPage, ShellError, TurnAccepted } from "./contracts/generated";
import { MockDesktopTransport } from "./transport/MockDesktopTransport";
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

afterEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
  window.localStorage.clear();
});

describe("Desktop App", () => {
  it("renders the real conversation surface and sends text with Enter", async () => {
    const user = userEvent.setup();
    render(<App />);
    const composer = await screen.findByRole("textbox", { name: "给 Sora 发消息" });
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "和 Sora 说点什么…"));
    await user.type(composer, "今天有点累{enter}");
    const feed = screen.getByRole("feed", { name: "当前对话" });
    expect(within(feed).getByText("今天有点累")).toBeInTheDocument();
    expect(await within(feed).findByText(/今天消耗了不少/)).toBeInTheDocument();
  });

  it("opens the real model settings page without rendering an API key input in WebView", async () => {
    const user = userEvent.setup();
    render(<App />);
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
    render(<App />);

    const trigger = screen.getByRole("button", { name: "展开对话列表" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "会话历史" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "会话历史" })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("loads the real conversation index and labels unavailable branching honestly", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "历史" }));
    expect(await screen.findByRole("heading", { name: "对话与分支" })).toBeInTheDocument();
    expect(screen.getAllByText("当前对话")).toHaveLength(2);
    expect(screen.getByText("真实分支接口尚未提供")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续对话" })).toBeEnabled();
  });

  it("hides, restores, and forgets a real user memory through the transport", async () => {
    const user = userEvent.setup();
    const transport = new FeatureTransport();
    render(<App transport={transport} />);

    await user.click(screen.getByRole("button", { name: "记忆与隐私" }));
    expect(await screen.findByRole("heading", { name: "记忆与隐私" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /你不喜欢被连续追问/ }));
    await user.click(screen.getByRole("button", { name: "隐藏" }));
    await waitFor(() => expect(transport.visibilityUpdates).toEqual([
      { memoryId: "memory_preference_1", visible: false },
    ]));
    expect(await screen.findByRole("button", { name: "恢复使用" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "忘记" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("忘记这条记忆");
    await user.click(screen.getByRole("button", { name: "确认忘记" }));
    await waitFor(() => expect(transport.forgottenMemoryIds).toEqual(["memory_preference_1"]));
    expect(await screen.findByText("目前没有可显示的记忆。")).toBeInTheDocument();
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
    render(<App />);

    await user.click(screen.getByRole("button", { name: "设置" }));
    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
    for (const label of ["模型与供应商", "Sora 与我", "外观", "通知", "数据与隐私", "高级"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "通知" }));
    expect(screen.getByText(/尚未接入 Windows 通知服务/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "高级" }));
    const developerSwitch = screen.getByRole("switch", { name: "调试模式" });
    expect(developerSwitch).toHaveAttribute("aria-checked", "false");
    await user.click(developerSwitch);
    expect(developerSwitch).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "打开" }));

    expect(await screen.findByText("这里没有模拟数据")).toBeInTheDocument();
    for (const label of ["单轮时间线", "上下文帧", "模型路由", "记忆流水线", "异步任务", "状态系统", "评测与发布", "受控控制台"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText(/真实调试通道尚未接入/)).toBeInTheDocument();
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
