import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
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
  HistoryPage,
  ProviderProfile,
  TurnAccepted,
  TurnStatusView,
  VerifyModelRequest,
  BindWorkloadRequest, WorkloadBinding,
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
  GovernanceKind, GovernancePage, ReviewResolutionRequest, ReviewReceipt, DefaultControlsRequest, UserMemoryDetail, MemoryCorrectionRequest, UserResponseBasis, MemoryQueuePage,
  QualityRequest, QualityPage, QualityResolutionRequest, QualityReceipt,
  QuarantineDismissalRequest, QuarantineReceipt,
} from "./DesktopTransport";

export class TauriDesktopTransport implements DesktopTransport {
  getNarrative(): Promise<import("./narrative").NarrativeView> { return invoke("narrative_read"); }
  getMemoryMaintenance(): Promise<import("./maintenance").MemoryMaintenanceView> { return invoke("memory_maintenance_read"); }
  setMemoryMaintenance(request: import("./maintenance").MemoryMaintenanceRequest): Promise<import("./maintenance").MemoryMaintenanceView> { return invoke("memory_maintenance_control", { request }); }
  narrativeAction(request: import("./narrative").NarrativeActionRequest): Promise<import("./narrative").NarrativeActionResult> { return invoke("narrative_action", { request }); }
  getCompanion(): Promise<import("./companion").CompanionView> { return invoke("companion_read"); }
  companionAction(request: import("./companion").CompanionActionRequest): Promise<import("./companion").CompanionView> { return invoke("companion_action", { request }); }
  getCompanionGovernance(domain: "persona" | "relationship"): Promise<import("./companion").CompanionGovernanceView> { return invoke("companion_governance", { domain }); }
  openCompanionSession(): Promise<DesktopDebugSession> { return invoke("companion_session_open"); }
  resolveCompanionGovernance(request: import("./companion").CompanionGovernanceDecision): Promise<import("./companion").CompanionGovernanceReceipt> { return invoke("companion_governance_resolve", { request }); }
  getPrivacyState(): Promise<import("./DesktopTransport").PrivacyState> { return invoke("privacy_state"); }
  async eraseSpace(request: import("./DesktopTransport").PrivacyEraseRequest): Promise<void> { await invoke("privacy_erase", { request }); }
  exportPrivacy(request: import("./DesktopTransport").PrivacyExportRequest): Promise<import("./DesktopTransport").PrivacyExportReceipt> {
    return invoke("privacy_export", { request });
  }
  listSpaces(): Promise<import("./DesktopTransport").SpacePage> { return invoke("spaces_list"); }
  createSpace(name: string, expectedRevision: string): Promise<import("./DesktopTransport").SpacePage> { return invoke("spaces_create", { name, expectedRevision }); }
  async switchSpace(request: import("./DesktopTransport").SpaceSwitchRequest): Promise<void> { await invoke("spaces_switch", { request }); }
  async openSpaceLocation(): Promise<void> { await invoke("spaces_open_location"); }
  getNotificationSettings(): Promise<import("./DesktopTransport").NotificationSettingsView> { return invoke("notifications_settings"); }
  saveNotificationSettings(preferences: import("./DesktopTransport").NotificationPreferences, expectedRevision: string): Promise<import("./DesktopTransport").NotificationSettingsView> { return invoke("notifications_save", { request: { preferences, expected_revision: expectedRevision } }); }
  testNotification(): Promise<{ readonly status: "disabled" | "quiet" | "submitted_to_windows" }> { return invoke("notifications_test"); }
  openQualitySession(): Promise<DesktopDebugSession> { return invoke("debug_quality_session_open"); }
  inspectQuality(request: QualityRequest): Promise<QualityPage> { return invoke("debug_quality_inspect", { request }); }
  resolveQualityEvent(request: QualityResolutionRequest): Promise<QualityReceipt> { return invoke("debug_quality_resolve", { request }); }
  getMemoryQueue(kind: "index" | "projection", status: "all" | "pending" | "failed" | "dead_letter"): Promise<MemoryQueuePage> { return invoke("debug_memory_queue", { kind, status }); }
  getMemoryDetail(memoryId: string): Promise<UserMemoryDetail> { return invoke("memory_detail", { memoryId }); }
  correctMemory(memoryId: string, request: MemoryCorrectionRequest): Promise<{ readonly memory: UserMemoryView }> { return invoke("memory_correct", { memoryId, request }); }
  getResponseBasis(conversationId: string, turnId: string): Promise<UserResponseBasis> { return invoke("conversation_response_basis", { conversationId, turnId }); }
  openReviewSession(): Promise<DesktopDebugSession> { return invoke("debug_review_session_open"); }
  getMemoryGovernance(kind: GovernanceKind, offset = 0): Promise<GovernancePage> { return invoke("debug_memory_governance", { kind, offset }); }
  resolveMemoryReview(request: ReviewResolutionRequest): Promise<ReviewReceipt> { return invoke("debug_review_resolve", { request }); }
  dismissQuarantine(request: QuarantineDismissalRequest): Promise<QuarantineReceipt> { return invoke("debug_quarantine_dismiss", { request }); }
  bindModelWorkload(request: BindWorkloadRequest): Promise<WorkloadBinding> {
    return invoke("provider_bind_workload", { request });
  }
  async deleteProvider(profileId: string): Promise<void> {
    await invoke("provider_delete", { profileId, confirmation: `delete:${profileId}` });
  }
  status(): Promise<DesktopStatus> {
    return invoke("desktop_status");
  }

  listConversations(request: { offset?: number; query?: string } = {}): Promise<ConversationIndexView> {
    return invoke("conversation_list", { offset: request.offset ?? 0, query: request.query ?? "" });
  }

  loadHistory(request: DesktopHistoryRequest): Promise<HistoryPage> {
    return invoke("conversation_history", { request });
  }

  submitTurn(request: DesktopSubmitTurnRequest): Promise<TurnAccepted> {
    return invoke("conversation_submit", { request });
  }

  cancelTurn(request: DesktopCancelTurnRequest): Promise<TurnStatusView> {
    return invoke("conversation_cancel", { request });
  }

  async subscribe(listener: DesktopEventListener): Promise<Unsubscribe> {
    return listen<DesktopEventEnvelope>("sora://desktop-event", (event) => listener(event.payload));
  }

  listProviders(): Promise<ProviderConfigurationView> {
    return invoke("provider_list");
  }

  createProvider(request: CreateProfileRequest): Promise<ProviderProfile> {
    return invoke("provider_create", { request });
  }

  promptAndVerifyCredential(
    request: DesktopProviderCredentialPromptRequest,
  ): Promise<DesktopProviderSetupOutcome> {
    return invoke("provider_prompt_and_verify_credential", { request });
  }

  rotateCredential(request: DesktopProviderRotatePromptRequest): Promise<DesktopProviderSetupOutcome> {
    return invoke("provider_prompt_and_rotate_credential", { request });
  }

  verifyProvider(
    profileId: string,
    request: VerifyModelRequest,
  ): Promise<Readonly<Record<string, unknown>>> {
    return invoke("provider_verify", { profileId, request });
  }

  listMemories(): Promise<UserMemoryPage> {
    return invoke("memory_list");
  }

  async setMemoryVisibility(memoryId: string, visible: boolean): Promise<UserMemoryView> {
    const result = await invoke<{ memory: UserMemoryView }>("memory_set_visibility", {
      memoryId,
      visible,
    });
    return result.memory;
  }

  async forgetMemory(memoryId: string): Promise<void> {
    await invoke("memory_forget", { memoryId });
  }

  getConversationControls(conversationId: string): Promise<ConversationControlsView> {
    return invoke("conversation_controls", { conversationId });
  }

  updateConversationControl(
    conversationId: string,
    request: ConversationControlUpdate,
  ): Promise<ConversationControlsView> {
    return invoke("conversation_control_update", { conversationId, request });
  }

  resetConversationControls(conversationId: string): Promise<ConversationControlsView> {
    return invoke("conversation_controls_reset", { conversationId });
  }
  saveConversationDefaults(conversationId: string, request: DefaultControlsRequest): Promise<ConversationControlsView> {
    return invoke("conversation_control_defaults", { conversationId, request });
  }

  getDebugSnapshot(conversationId: string): Promise<DesktopDebugSnapshot> {
    return invoke("debug_snapshot", { conversationId });
  }
  getDebugFrame(request: DesktopDebugFrameRequest): Promise<DesktopDebugFrameDetail> {
    return invoke("debug_frame", { request });
  }
  setFrameObservation(enabled: boolean): Promise<DesktopFrameObservation> {
    return invoke("debug_frame_observation", { request: { enabled } });
  }
  getFrameObservation(): Promise<DesktopFrameObservation> { return invoke("debug_frame_observation_status"); }
  openDebugSession(): Promise<DesktopDebugSession> { return invoke("debug_session_open"); }
  async closeDebugSession(sessionId: string): Promise<void> { await invoke("debug_session_close", { sessionId }); }
  getAdminCommands(): Promise<DesktopAdminRegistry> { return invoke("debug_commands"); }
  previewAdminCommand(request: DesktopAdminPreviewRequest): Promise<DesktopAdminActionView> {
    return invoke("debug_command_preview", { request });
  }
  executeAdminCommand(request: DesktopAdminExecuteRequest): Promise<DesktopAdminActionView> {
    return invoke("debug_command_execute", { request });
  }
  getAdminAction(actionId: string): Promise<DesktopAdminActionView> {
    return invoke("debug_command_action", { actionId });
  }
}
