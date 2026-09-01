import type { DesktopTransport } from "./DesktopTransport";
import { MockDesktopTransport } from "./MockDesktopTransport";
import { TauriDesktopTransport } from "./TauriDesktopTransport";

export function createDesktopTransport(): DesktopTransport {
  return window.__TAURI_INTERNALS__ ? new TauriDesktopTransport() : new MockDesktopTransport();
}

export type {
  ConversationControlUpdate,
  ConversationControlsView,
  ConversationIndexView,
  ConversationSummaryView,
  DesktopTransport,
  ProviderConfigurationView,
  UserMemoryPage,
  UserMemoryView,
} from "./DesktopTransport";
