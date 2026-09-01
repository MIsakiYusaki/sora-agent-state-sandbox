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
  HistoryPage,
  ProviderProfile,
  ModelDescriptor,
  TurnAccepted,
  TurnStatusView,
  VerifyModelRequest,
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
  readonly controls: Readonly<Record<string, { readonly value: unknown; readonly scope: string }>>;
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
  status(): Promise<DesktopStatus>;
  listConversations(): Promise<ConversationIndexView>;
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
  listMemories(): Promise<UserMemoryPage>;
  setMemoryVisibility(memoryId: string, visible: boolean): Promise<UserMemoryView>;
  forgetMemory(memoryId: string): Promise<void>;
  getConversationControls(conversationId: string): Promise<ConversationControlsView>;
  updateConversationControl(
    conversationId: string,
    request: ConversationControlUpdate,
  ): Promise<ConversationControlsView>;
  resetConversationControls(conversationId: string): Promise<ConversationControlsView>;
}
