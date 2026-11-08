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
  ModelDescriptor,
  TurnAccepted,
  TurnStatusView,
  VerifyModelRequest,
  BindWorkloadRequest, WorkloadBinding,
} from "../contracts/generated";

export interface ProviderConfigurationView {
  readonly revision: number;
  readonly profiles: ReadonlyArray<ProviderProfile>;
  readonly models: Readonly<Record<string, ReadonlyArray<ModelDescriptor>>>;
  readonly bindings: ReadonlyArray<Readonly<Record<string, unknown>>>;
  readonly health: {
    readonly status: string;
    readonly conversation_ready: boolean;
    readonly missing_conversation_workloads: ReadonlyArray<string>;
  };
}

export interface ConversationSummaryView {
  readonly conversation_id: string;
  readonly title: string;
  readonly preview: string;
  readonly created_at_ms: number;
  readonly updated_at_ms: number;
  readonly message_count: number;
  readonly conversation_version: number;
}

export interface ConversationIndexView {
  readonly schema_version: "ConversationIndex.v1";
  readonly items: ReadonlyArray<ConversationSummaryView>;
  readonly next_offset?: number | null;
}

export interface UserMemoryView {
  readonly id: string;
  readonly type: string;
  readonly summary: string;
  readonly tags: ReadonlyArray<string>;
  readonly confidence: number;
  readonly sensitivity: string;
  readonly evidence_count: number;
  readonly surface_permission: string;
  readonly visible: boolean;
  readonly lifecycle: Readonly<Record<string, unknown>>;
  readonly created_at: string | null;
  readonly updated_at: string | null;
}

export interface UserMemoryPage {
  readonly schema_version: "UserMemoryPage.v1";
  readonly items: ReadonlyArray<UserMemoryView>;
  readonly summary: {
    readonly available: number;
    readonly visible: number;
    readonly hidden: number;
    readonly archived: number;
  };
}

export interface ConversationControlsView {
  readonly schema_version: "ConversationControls.v1";
  readonly conversation_id: string;
  readonly controls: Readonly<Record<string, { readonly value: unknown; readonly scope: string; readonly expires_at?: number | null }>>;
  readonly initiative_lease: Readonly<Record<string, unknown>> | null;
}

export interface ConversationControlUpdate {
  readonly control: string;
  readonly value: unknown;
  readonly scope?: "turn" | "session" | "duration" | "persistent";
  readonly duration_seconds?: number | null;
}

export type DesktopEventListener = (event: DesktopEventEnvelope) => void;
export type Unsubscribe = () => void;

export interface DesktopTransport {
  getNarrative(): Promise<import("./narrative").NarrativeView>;
  getMemoryMaintenance(): Promise<import("./maintenance").MemoryMaintenanceView>;
  setMemoryMaintenance(request: import("./maintenance").MemoryMaintenanceRequest): Promise<import("./maintenance").MemoryMaintenanceView>;
  narrativeAction(request: import("./narrative").NarrativeActionRequest): Promise<import("./narrative").NarrativeActionResult>;
  getCompanion(): Promise<import("./companion").CompanionView>;
  companionAction(request: import("./companion").CompanionActionRequest): Promise<import("./companion").CompanionView>;
  getCompanionGovernance(domain: "persona" | "relationship"): Promise<import("./companion").CompanionGovernanceView>;
  openCompanionSession(): Promise<DesktopDebugSession>;
  resolveCompanionGovernance(request: import("./companion").CompanionGovernanceDecision): Promise<import("./companion").CompanionGovernanceReceipt>;
  getPrivacyState(): Promise<PrivacyState>;
  eraseSpace(request: PrivacyEraseRequest): Promise<void>;
  exportPrivacy(request: PrivacyExportRequest): Promise<PrivacyExportReceipt>;
  listSpaces(): Promise<SpacePage>;
  createSpace(name: string, expectedRevision: string): Promise<SpacePage>;
  switchSpace(request: SpaceSwitchRequest): Promise<void>;
  openSpaceLocation(): Promise<void>;
  getNotificationSettings(): Promise<NotificationSettingsView>;
  saveNotificationSettings(preferences: NotificationPreferences, expectedRevision: string): Promise<NotificationSettingsView>;
  testNotification(): Promise<{ readonly status: "disabled" | "quiet" | "submitted_to_windows" }>;
  openQualitySession(): Promise<DesktopDebugSession>;
  inspectQuality(request: QualityRequest): Promise<QualityPage>;
  resolveQualityEvent(request: QualityResolutionRequest): Promise<QualityReceipt>;
  openReviewSession(): Promise<DesktopDebugSession>;
  getMemoryQueue(kind: "index" | "projection", status: "all" | "pending" | "failed" | "dead_letter"): Promise<MemoryQueuePage>;
  getMemoryGovernance(kind: GovernanceKind, offset?: number): Promise<GovernancePage>;
  resolveMemoryReview(request: ReviewResolutionRequest): Promise<ReviewReceipt>;
  dismissQuarantine(request: QuarantineDismissalRequest): Promise<QuarantineReceipt>;
  status(): Promise<DesktopStatus>;
  listConversations(request?: { readonly offset?: number; readonly query?: string }): Promise<ConversationIndexView>;
  loadHistory(request: DesktopHistoryRequest): Promise<HistoryPage>;
  submitTurn(request: DesktopSubmitTurnRequest): Promise<TurnAccepted>;
  cancelTurn(request: DesktopCancelTurnRequest): Promise<TurnStatusView>;
  subscribe(listener: DesktopEventListener): Promise<Unsubscribe>;
  listProviders(): Promise<ProviderConfigurationView>;
  createProvider(request: CreateProfileRequest): Promise<ProviderProfile>;
  promptAndVerifyCredential(
    request: DesktopProviderCredentialPromptRequest,
  ): Promise<DesktopProviderSetupOutcome>;
  rotateCredential(request: DesktopProviderRotatePromptRequest): Promise<DesktopProviderSetupOutcome>;
  verifyProvider(profileId: string, request: VerifyModelRequest): Promise<Readonly<Record<string, unknown>>>;
  bindModelWorkload(request: BindWorkloadRequest): Promise<WorkloadBinding>;
  deleteProvider(profileId: string): Promise<void>;
  listMemories(): Promise<UserMemoryPage>;
  getMemoryDetail(memoryId: string): Promise<UserMemoryDetail>;
  correctMemory(memoryId: string, request: MemoryCorrectionRequest): Promise<{ readonly memory: UserMemoryView }>;
  getResponseBasis(conversationId: string, turnId: string): Promise<UserResponseBasis>;
  setMemoryVisibility(memoryId: string, visible: boolean): Promise<UserMemoryView>;
  forgetMemory(memoryId: string): Promise<void>;
  getConversationControls(conversationId: string): Promise<ConversationControlsView>;
  updateConversationControl(
    conversationId: string,
    request: ConversationControlUpdate,
  ): Promise<ConversationControlsView>;
  resetConversationControls(conversationId: string): Promise<ConversationControlsView>;
  saveConversationDefaults(conversationId: string, request: DefaultControlsRequest): Promise<ConversationControlsView>;
  getDebugSnapshot(conversationId: string): Promise<DesktopDebugSnapshot>;
  getDebugFrame(request: DesktopDebugFrameRequest): Promise<DesktopDebugFrameDetail>;
  setFrameObservation(enabled: boolean): Promise<DesktopFrameObservation>;
  getFrameObservation(): Promise<DesktopFrameObservation>;
  openDebugSession(): Promise<DesktopDebugSession>;
  closeDebugSession(sessionId: string): Promise<void>;
  getAdminCommands(): Promise<DesktopAdminRegistry>;
  previewAdminCommand(request: DesktopAdminPreviewRequest): Promise<DesktopAdminActionView>;
  executeAdminCommand(request: DesktopAdminExecuteRequest): Promise<DesktopAdminActionView>;
  getAdminAction(actionId: string): Promise<DesktopAdminActionView>;
}

export type PrivacyExportKind = "memory_summary" | "diagnostic";
export interface PrivacyState {
  readonly schema_version: "DesktopPrivacyState.v1";
  readonly profile_id: string;
  readonly space_name: string;
  readonly revision: string;
  readonly pending_operation: string | null;
  readonly last_completed_operation: string | null;
}
export interface PrivacyEraseRequest {
  readonly expected_profile_id: string;
  readonly expected_revision: string;
  readonly confirmation: string;
  readonly confirmed: true;
}
export interface PrivacyExportRequest {
  readonly kind: PrivacyExportKind;
  readonly expected_profile_id: string;
  readonly confirmed: true;
}
export interface PrivacyExportReceipt {
  readonly schema_version: "DesktopPrivacyExportReceipt.v1";
  readonly kind: PrivacyExportKind;
  readonly status: "saved" | "cancelled";
  readonly bytes?: number;
}
export type GovernanceKind = "reviews" | "candidates" | "quarantines";
export interface SpacePage { readonly revision: string; readonly active_id: string; readonly spaces: readonly { readonly id: string; readonly name: string }[] }
export interface SpaceSwitchRequest { readonly space_id: string; readonly expected_revision: string; readonly confirmed: true }
export interface NotificationPreferences { readonly enabled: boolean; readonly quiet_enabled: boolean; readonly quiet_start: number; readonly quiet_end: number }
export interface NotificationSettingsView { readonly preferences: NotificationPreferences; readonly revision: string; readonly os_permission: "enabled" | "disabled" | "unavailable" }
export type QualityKind = "runs" | "baselines" | "events";
export interface QualityRequest { readonly kind: QualityKind; readonly record_id?: string; readonly baseline_id?: string; readonly case_offset?: number }
export interface QualityRow { readonly id: string; readonly label: string; readonly status: string; readonly created_at: string | null; readonly revision: string | null; readonly related_run_id: string | null; readonly severity?: string | null; readonly source_type?: string | null; readonly source_fingerprint?: string | null; readonly resolved_at?: string | null }
export interface QualityCase { readonly id: string; readonly suite: string; readonly name: string; readonly status: string; readonly severity: string; readonly failure_category: string | null; readonly metrics: Readonly<Record<string, number>> }
export interface QualityPage {
  readonly schema_version: "DesktopQualityPage.v1"; readonly kind: QualityKind; readonly items: readonly QualityRow[]; readonly truncated: boolean;
  readonly selected: QualityRow | null; readonly cases: readonly QualityCase[] | null; readonly case_count: number | null; readonly next_case_offset: number | null;
  readonly summary: Readonly<Record<string, number>>;
  readonly comparison: { readonly status: string; readonly baseline_id: string; readonly regression_count: number; readonly unaccepted_regression_count: number; readonly baseline_pass_rate: number | null; readonly current_pass_rate: number | null } | null;
}
export interface QualityResolutionRequest { readonly event_id: string; readonly expected_revision: string; readonly resolution: "investigated" | "superseded" | "false_positive"; readonly confirmed: true }
export interface QualityReceipt { readonly event_id: string; readonly status: "resolved" | "already_resolved"; readonly audit_event_id: string }
export interface MemoryQueuePage { readonly schema_version: "DesktopMemoryQueuePage.v1"; readonly kind: "index" | "projection"; readonly truncated: boolean; readonly items: readonly { readonly id: string; readonly status: string; readonly attempts: number | null; readonly backend: string | null; readonly target: string | null; readonly action: string | null; readonly created_at: string | null; readonly next_attempt_at: string | null; readonly error_fingerprint: string | null; readonly error_code: string | null; readonly revision?: string | null; readonly can_requeue?: boolean; readonly outcome?: "applied" | "skipped_duplicate" | "rejected_policy" | "unobserved" }[] }
export interface UserMemoryDetail { readonly schema_version: "UserMemoryDetail.v1"; readonly memory: UserMemoryView; readonly revision: string; readonly can_correct: boolean; readonly sources: readonly { readonly kind: "manual" | "conversation" | "other"; readonly created_at: string | null; readonly conversation_id: string | null }[] }
export interface MemoryCorrectionRequest { readonly summary: string; readonly expected_revision: string; readonly confirmed: true }
export interface UserResponseBasis { readonly schema_version: "UserResponseBasis.v1"; readonly conversation_id: string; readonly turn_id: string; readonly source: "generation_sealed" | "unobserved"; readonly recent_message_count: number | null; readonly memories: readonly { readonly memory_id: string; readonly summary: string }[]; readonly unavailable_memory_count: number | null }
export interface DefaultControlsRequest { readonly action: "save" | "reset"; readonly confirmed: true; readonly reply_depth?: "brief" | "balanced" | "deep"; readonly follow_up?: "none" | "one"; readonly topic_shift?: "same_topic" | "adjacent"; readonly memory_mode?: "natural" | "explicit" | "off" }
export interface GovernanceRow {
  readonly id: string; readonly kind: GovernanceKind; readonly status: string;
  readonly memory_type: string | null; readonly summary: string | null;
  readonly summary_state: "available" | "protected"; readonly reason_codes: readonly string[];
  readonly created_at: string | null; readonly evidence_count: number | null;
  readonly revision: string | null; readonly can_approve: boolean;
  readonly can_dismiss?: boolean; readonly severity?: string | null; readonly risk_type?: string | null;
}
export interface GovernancePage { readonly schema_version: "DesktopGovernancePage.v1"; readonly items: readonly GovernanceRow[]; readonly truncated: boolean; readonly next_offset?: number | null }
export interface ReviewResolutionRequest { readonly review_id: string; readonly action: "approve" | "reject" | "defer"; readonly expected_revision: string; readonly confirmed: true }
export interface ReviewReceipt { readonly schema_version: "DesktopReviewReceipt.v1"; readonly review_id: string; readonly status: string; readonly resolution: string | null; readonly resolved_at: string | null }
export interface QuarantineDismissalRequest { readonly quarantine_id: string; readonly expected_revision: string; readonly confirmed: true }
export interface QuarantineReceipt { readonly status: "dismissed" | "already_dismissed"; readonly quarantine_id: string; readonly audit_id: string; readonly resolved_at: string }
