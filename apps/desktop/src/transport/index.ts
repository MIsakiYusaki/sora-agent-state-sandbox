import type { DesktopTransport } from "./DesktopTransport";
import { MockDesktopTransport } from "./MockDesktopTransport";
import { TauriDesktopTransport } from "./TauriDesktopTransport";

export function createDesktopTransport(): DesktopTransport {
  if (window.__TAURI_INTERNALS__) return new TauriDesktopTransport();
  if (
    import.meta.env.DEV
    && import.meta.env.VITE_SORA_USE_MOCK_TRANSPORT === "true"
  ) {
    return new MockDesktopTransport();
  }
  throw new Error("Project Sora production client requires the trusted Tauri desktop host");
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
