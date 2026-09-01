import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
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

export class TauriDesktopTransport implements DesktopTransport {
  status(): Promise<DesktopStatus> {
    return invoke("desktop_status");
  }

  listConversations(): Promise<ConversationIndexView> {
    return invoke("conversation_list");
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
}
