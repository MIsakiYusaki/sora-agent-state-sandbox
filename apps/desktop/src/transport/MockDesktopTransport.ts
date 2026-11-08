import type {
  DesktopDebugSession, DesktopAdminRegistry, DesktopAdminActionView,
  DesktopAdminPreviewRequest, DesktopAdminExecuteRequest,
  CreateProfileRequest,
  DesktopCancelTurnRequest,
  DesktopDebugSnapshot,
  DesktopDebugFrameRequest, DesktopDebugFrameDetail,
  DesktopFrameObservation,
  DesktopEventEnvelope,
  DesktopHistoryRequest,
  DesktopProviderCredentialPromptRequest,
  DesktopProviderRotatePromptRequest,
  DesktopProviderSetupOutcome,
  DesktopStatus,
  DesktopSubmitTurnRequest,
  GatewayEvent,
  HistoryItem,
  HistoryPage,
  ProviderProfile,
  TurnAccepted,
  TurnStatusView,
  VerifyModelRequest,
} from "../contracts/generated";
import type {
  ConversationControlUpdate,
  ConversationControlsView,
  ConversationIndexView,
  DesktopEventListener,
  DesktopTransport,
  ProviderConfigurationView,
  UserMemoryPage,
  UserMemoryView,
  Unsubscribe,
} from "./DesktopTransport";

const now = () => Date.now();

export class MockDesktopTransport implements DesktopTransport {
  async getNarrative(): Promise<import("./narrative").NarrativeView> { throw new Error("布局预览不提供真实故事与时间线。"); }
  async getMemoryMaintenance(): Promise<import("./maintenance").MemoryMaintenanceView> { throw new Error("布局预览不提供真实维护状态。"); }
  async setMemoryMaintenance(_request: import("./maintenance").MemoryMaintenanceRequest): Promise<import("./maintenance").MemoryMaintenanceView> { throw new Error("布局预览不修改维护状态。"); }
  async narrativeAction(_request: import("./narrative").NarrativeActionRequest): Promise<import("./narrative").NarrativeActionResult> { throw new Error("布局预览不记录故事选择。"); }
  async getCompanion(): Promise<import("./companion").CompanionView> { throw new Error("布局预览不读取真实陪伴状态。"); }
  async companionAction(_request: import("./companion").CompanionActionRequest): Promise<import("./companion").CompanionView> { throw new Error("布局预览不修改真实陪伴状态。"); }
  async getCompanionGovernance(_domain: "persona" | "relationship"): Promise<import("./companion").CompanionGovernanceView> { throw new Error("布局预览不提供领域治理记录。"); }
  async openCompanionSession(): Promise<DesktopDebugSession> { throw new Error("布局预览不授予领域管理权限。"); }
  async resolveCompanionGovernance(_request: import("./companion").CompanionGovernanceDecision): Promise<import("./companion").CompanionGovernanceReceipt> { throw new Error("布局预览不执行领域治理。"); }
  async getPrivacyState(): Promise<import("./DesktopTransport").PrivacyState> { throw new Error("布局预览不读取真实删除状态。"); }
  async eraseSpace(_request: import("./DesktopTransport").PrivacyEraseRequest): Promise<void> { throw new Error("布局预览不能删除真实空间。"); }
  async exportPrivacy(_request: import("./DesktopTransport").PrivacyExportRequest): Promise<import("./DesktopTransport").PrivacyExportReceipt> {
    throw new Error("布局预览无法保存真实隐私导出文件。");
  }
  async listSpaces(): Promise<import("./DesktopTransport").SpacePage> { throw new Error("布局预览不读取真实空间目录。"); }
  async createSpace(_name: string, _revision: string): Promise<import("./DesktopTransport").SpacePage> { throw new Error("布局预览不创建真实空间。"); }
  async switchSpace(_request: import("./DesktopTransport").SpaceSwitchRequest): Promise<void> { throw new Error("布局预览不能切换真实空间。"); }
  async openSpaceLocation(): Promise<void> { throw new Error("布局预览不打开真实数据目录。"); }
  async getNotificationSettings(): Promise<import("./DesktopTransport").NotificationSettingsView> { throw new Error("布局预览不读取真实通知偏好。"); }
  async saveNotificationSettings(_preferences: import("./DesktopTransport").NotificationPreferences, _revision: string): Promise<import("./DesktopTransport").NotificationSettingsView> { throw new Error("布局预览不保存真实通知偏好。"); }
  async testNotification(): Promise<{ readonly status: "disabled" | "quiet" | "submitted_to_windows" }> { throw new Error("布局预览不发送 Windows 通知。"); }
  async openQualitySession(): Promise<DesktopDebugSession> { throw new Error("模拟传输不能开启真实质量治理会话。"); }
  async inspectQuality(_request: import("./DesktopTransport").QualityRequest): Promise<import("./DesktopTransport").QualityPage> { throw new Error("模拟传输没有真实质量运行记录。"); }
  async resolveQualityEvent(_request: import("./DesktopTransport").QualityResolutionRequest): Promise<import("./DesktopTransport").QualityReceipt> { throw new Error("模拟传输不结案真实质量事件。"); }
  async getMemoryQueue(_kind: "index" | "projection", _status: "all" | "pending" | "failed" | "dead_letter"): Promise<import("./DesktopTransport").MemoryQueuePage> { throw new Error("模拟传输不提供真实队列事件。"); }
  async getMemoryDetail(_id: string): Promise<import("./DesktopTransport").UserMemoryDetail> { throw new Error("模拟传输不提供真实记忆来源。"); }
  async correctMemory(_id: string, _request: import("./DesktopTransport").MemoryCorrectionRequest): Promise<{ readonly memory: UserMemoryView }> { throw new Error("模拟传输不修改真实记忆。"); }
  async getResponseBasis(_conversation: string, _turn: string): Promise<import("./DesktopTransport").UserResponseBasis> { throw new Error("模拟传输不提供实际回应依据。"); }
  async getFrameObservation(): Promise<import("../contracts/generated").DesktopFrameObservation> { throw new Error("模拟传输不读取真实采集状态。"); }
  async saveConversationDefaults(_id: string, _request: import("./DesktopTransport").DefaultControlsRequest): Promise<ConversationControlsView> { throw new Error("模拟传输不保存真实默认偏好。"); }
  async openReviewSession(): Promise<import("../contracts/generated").DesktopDebugSession> { throw new Error("模拟传输不能开启真实审核会话。"); }
  async getMemoryGovernance(_kind: import("./DesktopTransport").GovernanceKind): Promise<import("./DesktopTransport").GovernancePage> { throw new Error("模拟传输不提供真实治理记录。"); }
  async resolveMemoryReview(_request: import("./DesktopTransport").ReviewResolutionRequest): Promise<import("./DesktopTransport").ReviewReceipt> { throw new Error("模拟传输不处理真实审核。"); }
  async dismissQuarantine(_request: import("./DesktopTransport").QuarantineDismissalRequest): Promise<import("./DesktopTransport").QuarantineReceipt> { throw new Error("模拟传输不处置真实隔离记录。"); }
  async bindModelWorkload(_request: import("../contracts/generated").BindWorkloadRequest): Promise<import("../contracts/generated").WorkloadBinding> {
    throw new Error("模拟传输不修改真实模型路由。");
  }
  async deleteProvider(_profileId: string): Promise<void> { throw new Error("模拟传输不删除真实模型配置。"); }
  async openDebugSession(): Promise<DesktopDebugSession> { throw new Error("维护命令需要真实 Tauri Host；布局夹具不会执行命令。"); }
  async closeDebugSession(_sessionId: string): Promise<void> { /* No native session is created by this fixture. */ }
  async getAdminCommands(): Promise<DesktopAdminRegistry> { throw new Error("命令注册表未接入该布局夹具。"); }
  async previewAdminCommand(_request: DesktopAdminPreviewRequest): Promise<DesktopAdminActionView> { throw new Error("布局夹具禁止预览维护命令。"); }
  async executeAdminCommand(_request: DesktopAdminExecuteRequest): Promise<DesktopAdminActionView> { throw new Error("布局夹具禁止执行维护命令。"); }
  async getAdminAction(_actionId: string): Promise<DesktopAdminActionView> { throw new Error("布局夹具没有真实审计回执。"); }
  private readonly listeners = new Set<DesktopEventListener>();
  private readonly cancelled = new Set<string>();
  private profiles: ProviderProfile[] = [];
  private sequence = 0;
  private history: HistoryItem[] = [
    {
      role: "sora",
      content: "早上好。我在这里，今天想从哪里开始？",
      turn_id: "turn_welcome",
      sequence: 1,
      created_at_ms: now() - 60_000,
    },
  ];
  private controls: ConversationControlsView["controls"] = {
    quiet: { value: false, scope: "default" },
    response_mute: { value: false, scope: "default" },
    reply_depth: { value: "balanced", scope: "default" },
    follow_up: { value: "none", scope: "default" },
    topic_shift: { value: "same_topic", scope: "default" },
    state_surface: { value: "implicit", scope: "default" },
  };
  private memories: UserMemoryView[] = [];

  async status(): Promise<DesktopStatus> {
    return {
      schema_version: "DesktopStatus.v1",
      phase: "ready",
      profile_id: "local",
      client_instance_id: "desktop_mock",
      protocol_version: "1.0",
      gateway_ready: true,
      provider_ready: true,
      recoverable: true,
      status_message: "本地预览模式",
    };
  }

  async listConversations(): Promise<ConversationIndexView> {
    const firstUser = this.history.find((item) => item.role === "user");
    const last = this.history.at(-1);
    return {
      schema_version: "ConversationIndex.v1",
      items: last ? [{
        conversation_id: "conversation_main",
        title: firstUser?.content.slice(0, 42) || "当前对话",
        preview: last.content.slice(0, 96),
        created_at_ms: this.history[0]?.created_at_ms ?? now(),
        updated_at_ms: last.created_at_ms,
        message_count: this.history.length,
        conversation_version: Math.floor(this.history.length / 2),
      }] : [],
    };
  }

  async loadHistory(request: DesktopHistoryRequest): Promise<HistoryPage> {
    return {
      schema_version: "HistoryPage.v1",
      protocol_version: "1.0",
      conversation_id: request.conversation_id,
      conversation_version: Math.floor(this.history.length / 2),
      items: this.history,
      next_before_sequence: null,
    };
  }

  async submitTurn(request: DesktopSubmitTurnRequest): Promise<TurnAccepted> {
    const turnId = `turn_${crypto.randomUUID().replaceAll("-", "")}`;
    const requestId = `request_${crypto.randomUUID().replaceAll("-", "")}`;
    const userSequence = ++this.sequence;
    this.history.push({
      role: "user",
      content: request.text,
      turn_id: turnId,
      sequence: userSequence,
      created_at_ms: now(),
    });
    const accepted: TurnAccepted = {
      schema_version: "TurnAccepted.v1",
      protocol_version: "1.0",
      turn_id: turnId,
      request_id: requestId,
      conversation_id: request.conversation_id,
      correlation_id: crypto.randomUUID().replaceAll("-", ""),
      status: "accepted",
      conversation_version: Math.floor(this.history.length / 2),
      idempotent_replay: false,
      events_url: `/v1/turns/${turnId}/events`,
    };
    void this.simulateResponse(request.conversation_id, turnId, request.text);
    return accepted;
  }

  async cancelTurn(request: DesktopCancelTurnRequest): Promise<TurnStatusView> {
    this.cancelled.add(request.turn_id);
    this.emitGateway(request.conversation_id, request.turn_id, "turn.cancelled", {});
    return {
      schema_version: "TurnStatusView.v1",
      protocol_version: "1.0",
      turn_id: request.turn_id,
      request_id: "request_cancelled",
      conversation_id: request.conversation_id,
      correlation_id: crypto.randomUUID().replaceAll("-", ""),
      runtime_status: "cancelled",
      product_status: "cancelled",
      stage: "cancelled",
      revision: 1,
      visible_output_started: true,
      created_at_ms: now(),
      updated_at_ms: now(),
      committed_at_ms: null,
    };
  }

  async subscribe(listener: DesktopEventListener): Promise<Unsubscribe> {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async listProviders(): Promise<ProviderConfigurationView> {
    const ready = this.profiles.some((profile) => profile.status === "verified");
    return {
      revision: 1,
      profiles: this.profiles,
      models: {},
      bindings: [],
      health: {
        status: "ok",
        conversation_ready: ready,
        missing_conversation_workloads: ready ? [] : ["response_generation"],
      },
    };
  }

  async createProvider(request: CreateProfileRequest): Promise<ProviderProfile> {
    const profile: ProviderProfile = {
      profile_id: `provider_${crypto.randomUUID().replaceAll("-", "")}`,
      owner: { tenant_id: "local", user_id: "local-user", agent_id: "sora" },
      display_name: request.display_name,
      provider_kind: request.provider_kind,
      protocol: request.protocol,
      endpoint: {
        base_url: request.base_url,
        mode: request.endpoint_mode ?? "remote",
        custom: request.custom_endpoint ?? false,
      },
      credential_ref: null,
      auth_scheme: request.auth_scheme ?? "bearer",
      status: "draft",
      capability_revision: 0,
      policy_revision: 1,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    this.profiles = [...this.profiles, profile];
    return profile;
  }

  async promptAndVerifyCredential(
    request: DesktopProviderCredentialPromptRequest,
  ): Promise<DesktopProviderSetupOutcome> {
    return this.setupOutcome(this.markVerified(request.profile_id), request.verification.model_id);
  }

  async rotateCredential(request: DesktopProviderRotatePromptRequest): Promise<DesktopProviderSetupOutcome> {
    return this.setupOutcome(this.markVerified(request.profile_id), request.verification.model_id);
  }

  async verifyProvider(
    profileId: string,
    request: VerifyModelRequest,
  ): Promise<Readonly<Record<string, unknown>>> {
    return { provider_profile_id: profileId, model_id: request.model_id, verified: true };
  }

  async listMemories(): Promise<UserMemoryPage> {
    return {
      schema_version: "UserMemoryPage.v1",
      items: this.memories,
      summary: {
        available: this.memories.length,
        visible: this.memories.filter((item) => item.visible).length,
        hidden: this.memories.filter((item) => !item.visible).length,
        archived: 0,
      },
    };
  }

  async setMemoryVisibility(memoryId: string, visible: boolean): Promise<UserMemoryView> {
    let updated: UserMemoryView | undefined;
    this.memories = this.memories.map((item) => {
      if (item.id !== memoryId) return item;
      updated = {
        ...item,
        visible,
        surface_permission: visible ? "auto" : "none",
      };
      return updated;
    });
    if (!updated) throw new Error("memory not found");
    return updated;
  }

  async forgetMemory(memoryId: string): Promise<void> {
    this.memories = this.memories.filter((item) => item.id !== memoryId);
  }

  async getConversationControls(conversationId: string): Promise<ConversationControlsView> {
    return this.controlsView(conversationId);
  }

  async updateConversationControl(
    conversationId: string,
    request: ConversationControlUpdate,
  ): Promise<ConversationControlsView> {
    this.controls = {
      ...this.controls,
      [request.control]: {
        value: request.value,
        scope: request.scope ?? "session",
      },
    };
    return this.controlsView(conversationId);
  }

  async resetConversationControls(conversationId: string): Promise<ConversationControlsView> {
    this.controls = {
      quiet: { value: false, scope: "default" },
      response_mute: { value: false, scope: "default" },
      reply_depth: { value: "balanced", scope: "default" },
      follow_up: { value: "none", scope: "default" },
      topic_shift: { value: "same_topic", scope: "default" },
      state_surface: { value: "implicit", scope: "default" },
    };
    return this.controlsView(conversationId);
  }

  async getDebugFrame(_request: DesktopDebugFrameRequest): Promise<DesktopDebugFrameDetail> {
    throw new Error("模拟传输不提供真实上下文帧；仅显式测试夹具可以覆盖。");
  }
  async setFrameObservation(enabled: boolean): Promise<DesktopFrameObservation> {
    if (enabled) throw new Error("模拟传输不能开启真实帧采集。");
    return { active: false, expires_at_ms: null }; // This fixture owns no sampler.
  }

  async getDebugSnapshot(conversationId: string): Promise<DesktopDebugSnapshot> {
    const active = (subsystem_id: string, label: string, summary: string) => ({
      schema_version: "DesktopDebugSubsystem.v1" as const,
      subsystem_id,
      label,
      status: "active" as const,
      summary,
      details: {},
    });
    return {
      schema_version: "DesktopDebugSnapshot.v1",
      collected_at_ms: now(),
      profile_id_hash: "mock-local-profile",
      conversation_id: conversationId,
      gateway: { status: "ready", active_turns: 0, runtime_status: "ready", registry_status: "ready" },
      timeline: { status: "empty", turns: [], retention_limit: 20 },
      routes: { status: "available", registry_revision: 0, binding_count: 0, bindings: [] },
      systems: [
        active("conversation_runtime", "文字会话运行时", "流式输出、取消和提交链路可用"),
        active("interruptibility", "对话边界与可打断性", "自然语言控制已接入"),
        active("memory", "记忆系统 01-10", "记忆治理、召回和质量层已组装"),
        active("persona", "人格系统", "有界、可回滚的人格轨迹服务已组装"),
        active("affect", "情绪与内部状态", "短期状态与衰减服务已组装"),
        active("relationship", "关系连续性", "按用户隔离的关系服务已组装"),
        active("worldops", "叙事世界", "内容与个人时间线服务已组装"),
        active("model_provider", "模型服务与路由", "Provider 与 workload 路由已组装"),
        active("module_10", "评测、观测与管理", "评测注册表与受控管理动作已组装"),
      ],
      memory: { status: "ok", active_memories: this.memories.length, pending_candidates: 0, open_reviews: 0 },
      async_work: { pending: 0, failed: 0, dead_letter: 0 },
      quality: { status: "ok", eval_suite_count: 0, quality_run_count: 0, baseline_count: 0, open_event_count: 0, admin_action_count: 0, eval_suites: [], latest_runs: [], baselines: [], open_events: [], admin_actions: [] },
      bridges: [
        { schema_version: "DesktopDebugBridge.v1", bridge_id: "gateway_events", label: "当前会话事件", status: "available", summary: "当前 turn 的真实 Gateway 事件可用" },
        { schema_version: "DesktopDebugBridge.v1", bridge_id: "turn_diagnostics", label: "逐轮全链路诊断", status: "available", summary: "测试适配器提供事件级诊断" },
        { schema_version: "DesktopDebugBridge.v1", bridge_id: "debug_snapshot", label: "脱敏运行快照", status: "available", summary: "身份隔离的 L0 快照可用" },
        { schema_version: "DesktopDebugBridge.v1", bridge_id: "typed_frames", label: "逐轮类型化上下文帧", status: "partial", summary: "逐轮帧契约元数据已投影" },
        { schema_version: "DesktopDebugBridge.v1", bridge_id: "typed_commands", label: "受控命令执行", status: "partial", summary: "命令注册表已投影，执行桥关闭" },
      ],
    };
  }

  private markVerified(profileId: string): ProviderProfile {
    let result: ProviderProfile | undefined;
    this.profiles = this.profiles.map((profile) => {
      if (profile.profile_id !== profileId) return profile;
      result = {
        ...profile,
        credential_ref: "bound",
        status: "verified",
        capability_revision: (profile.capability_revision ?? 0) + 1,
        updated_at: new Date().toISOString(),
      };
      return result;
    });
    if (!result) throw new Error("Provider profile not found");
    return result;
  }

  private controlsView(conversationId: string): ConversationControlsView {
    return {
      schema_version: "ConversationControls.v1",
      conversation_id: conversationId,
      controls: this.controls,
      initiative_lease: null,
    };
  }

  private setupOutcome(profile: ProviderProfile, modelId: string): DesktopProviderSetupOutcome {
    return {
      schema_version: "DesktopProviderSetupOutcome.v1",
      profile,
      routing: {
        schema_version: "ProviderRoutingSetupResult.v1",
        profile_id: profile.profile_id,
        model: {
          provider_profile_id: profile.profile_id,
          model_id: modelId,
          capabilities: {
            chat: true,
            streaming: true,
            structured_output: true,
            thinking_control: true,
            cancellation: true,
            usage_reporting: true,
            embedding: false,
            rerank: false,
          },
          source: "mock",
          revision: 1,
        },
        bound_workloads: [],
        preserved_workloads: [],
        unsupported_workloads: [],
        missing_conversation_workloads: [],
        ready_for_conversation: true,
      },
    };
  }

  private async simulateResponse(conversationId: string, turnId: string, input: string): Promise<void> {
    await this.delay(180);
    this.emitGateway(conversationId, turnId, "turn.progress", { stage: "thinking" });
    const response = input.includes("累")
      ? "听起来今天消耗了不少。先在我这里停一会儿也可以，不必马上振作。"
      : "我听见了。我们可以慢一点，把这件事按你舒服的节奏理清楚。";
    let assembled = "";
    for (const chunk of response.match(/.{1,6}/gu) ?? [response]) {
      await this.delay(65);
      if (this.cancelled.has(turnId)) return;
      assembled += chunk;
      this.emitGateway(conversationId, turnId, "response.delta", { text: chunk });
    }
    this.history.push({
      role: "sora",
      content: assembled,
      turn_id: turnId,
      sequence: ++this.sequence,
      created_at_ms: now(),
    });
    this.emitGateway(conversationId, turnId, "turn.committed", {
      committed: true,
      conversation_version: Math.floor(this.history.length / 2),
    });
  }

  private emitGateway(
    conversationId: string,
    turnId: string,
    eventType: string,
    payload: Readonly<Record<string, unknown>>,
  ): void {
    const event: GatewayEvent = {
      schema_version: "GatewayEvent.v1",
      protocol_version: "1.0",
      turn_id: turnId,
      sequence: ++this.sequence,
      event_type: eventType,
      correlation_id: crypto.randomUUID().replaceAll("-", ""),
      payload,
      created_at_ms: now(),
      replayed: false,
    };
    const envelope: DesktopEventEnvelope = {
      schema_version: "DesktopEventEnvelope.v1",
      kind: "gateway_event",
      conversation_id: conversationId,
      turn_id: turnId,
      payload: event as unknown as Readonly<Record<string, unknown>>,
    };
    for (const listener of this.listeners) listener(envelope);
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }
}
