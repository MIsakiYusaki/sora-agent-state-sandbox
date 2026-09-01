import type {
  CreateProfileRequest,
  DesktopCancelTurnRequest,
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
    follow_up: { value: "one", scope: "default" },
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
      follow_up: { value: "one", scope: "default" },
      topic_shift: { value: "same_topic", scope: "default" },
      state_surface: { value: "implicit", scope: "default" },
    };
    return this.controlsView(conversationId);
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
