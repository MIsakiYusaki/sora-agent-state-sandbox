import type {
  DesktopStatus,
  GatewayEvent,
  HistoryItem,
  HistoryPage,
  ShellError,
  TurnAccepted,
} from "../contracts/generated";

export type DeliveryState = "pending" | "streaming" | "committed" | "cancelled" | "failed";

export interface ConversationMessage {
  readonly id: string;
  readonly role: "user" | "sora";
  readonly content: string;
  readonly turnId: string;
  readonly sequence: number;
  readonly createdAtMs: number;
  readonly delivery: DeliveryState;
}

export interface ActiveTurn {
  readonly turnId: string;
  readonly requestId: string;
  readonly stage: string;
  readonly lastSequence: number;
}

export interface PendingSubmission {
  readonly localMessageId: string;
  readonly text: string;
}

export interface ConversationState {
  readonly boot: "loading" | "ready" | "failed";
  readonly status: DesktopStatus | null;
  readonly conversationId: string;
  readonly conversationVersion: number;
  readonly messages: ReadonlyArray<ConversationMessage>;
  readonly draft: string;
  readonly pendingSubmission: PendingSubmission | null;
  readonly activeTurn: ActiveTurn | null;
  readonly error: ShellError | null;
  readonly settingsOpen: boolean;
}

export type ConversationAction =
  | { readonly type: "conversation.selected"; readonly conversationId: string }
  | { readonly type: "boot.ready"; readonly status: DesktopStatus; readonly history: HistoryPage }
  | { readonly type: "boot.loading" }
  | { readonly type: "boot.failed"; readonly error: ShellError }
  | { readonly type: "status.refreshed"; readonly status: DesktopStatus }
  | { readonly type: "draft.changed"; readonly value: string }
  | { readonly type: "turn.submitting"; readonly message: ConversationMessage }
  | { readonly type: "turn.accepted"; readonly accepted: TurnAccepted; readonly localMessageId: string }
  | { readonly type: "gateway.event"; readonly event: GatewayEvent }
  | { readonly type: "turn.failed"; readonly error: ShellError; readonly turnId?: string }
  | { readonly type: "error.dismissed" }
  | { readonly type: "settings.changed"; readonly open: boolean };

export const initialConversationState: ConversationState = {
  boot: "loading",
  status: null,
  conversationId: "conversation_main",
  conversationVersion: 0,
  messages: [],
  draft: "",
  pendingSubmission: null,
  activeTurn: null,
  error: null,
  settingsOpen: false,
};

function historyMessage(item: HistoryItem): ConversationMessage {
  return {
    id: `${item.turn_id}:${item.sequence}:${item.role}`,
    role: item.role,
    content: item.content,
    turnId: item.turn_id,
    sequence: item.sequence,
    createdAtMs: item.created_at_ms,
    delivery: "committed",
  };
}

function eventText(event: GatewayEvent): string {
  const payload = event.payload ?? {};
  const delta = payload["delta"];
  if (typeof delta === "string") return delta;
  const legacyText = payload["text"];
  return typeof legacyText === "string" ? legacyText : "";
}

function patchTurn(
  messages: ReadonlyArray<ConversationMessage>,
  turnId: string,
  update: (message: ConversationMessage) => ConversationMessage,
): ReadonlyArray<ConversationMessage> {
  return messages.map((message) => (message.turnId === turnId ? update(message) : message));
}

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case "conversation.selected":
      return {
        ...state,
        boot: "loading",
        conversationId: action.conversationId,
        conversationVersion: 0,
        messages: [],
        draft: "",
        pendingSubmission: null,
        activeTurn: null,
        error: null,
      };
    case "boot.loading":
      return { ...state, boot: "loading", error: null };
    case "boot.ready":
      return {
        ...state,
        boot: "ready",
        status: action.status,
        conversationVersion: action.history.conversation_version,
        messages: action.history.items.map(historyMessage),
        error: null,
      };
    case "boot.failed":
      return { ...state, boot: "failed", error: action.error };
    case "status.refreshed":
      return { ...state, status: action.status };
    case "draft.changed":
      return { ...state, draft: action.value };
    case "turn.submitting":
      return {
        ...state,
        messages: [...state.messages, action.message],
        pendingSubmission: {
          localMessageId: action.message.id,
          text: action.message.content,
        },
        error: null,
      };
    case "turn.accepted":
      return {
        ...state,
        draft:
          state.pendingSubmission?.localMessageId === action.localMessageId &&
          state.draft === state.pendingSubmission.text
            ? ""
            : state.draft,
        messages: state.messages.map((message) =>
          message.id === action.localMessageId
            ? { ...message, turnId: action.accepted.turn_id, delivery: "committed" }
            : message,
        ),
        activeTurn: {
          turnId: action.accepted.turn_id,
          requestId: action.accepted.request_id,
          stage: "accepted",
          lastSequence: 0,
        },
        pendingSubmission: null,
        conversationVersion: action.accepted.conversation_version,
      };
    case "gateway.event": {
      const active = state.activeTurn;
      if (active?.turnId === action.event.turn_id && action.event.sequence <= active.lastSequence) {
        return state;
      }
      const nextActive =
        active?.turnId === action.event.turn_id
          ? { ...active, lastSequence: action.event.sequence, stage: action.event.event_type }
          : active;
      if (action.event.event_type === "response.delta") {
        const delta = eventText(action.event);
        const existing = state.messages.some(
          (message) => message.turnId === action.event.turn_id && message.role === "sora",
        );
        const messages: ReadonlyArray<ConversationMessage> = existing
          ? state.messages.map((message) =>
              message.turnId === action.event.turn_id && message.role === "sora"
                ? { ...message, content: message.content + delta, delivery: "streaming" as const }
                : message,
            )
          : [
              ...state.messages,
              {
                id: `${action.event.turn_id}:sora`,
                role: "sora" as const,
                content: delta,
                turnId: action.event.turn_id,
                sequence: action.event.sequence,
                createdAtMs: action.event.created_at_ms,
                delivery: "streaming" as const,
              },
            ];
        return { ...state, messages, activeTurn: nextActive };
      }
      if (action.event.event_type === "turn.committed") {
        const version = (action.event.payload ?? {})["conversation_version"];
        return {
          ...state,
          messages: patchTurn(state.messages, action.event.turn_id, (message) => ({
            ...message,
            delivery: "committed",
          })),
          activeTurn: null,
          pendingSubmission: null,
          conversationVersion:
            typeof version === "number" ? version : state.conversationVersion,
        };
      }
      if (action.event.event_type === "turn.cancelled") {
        return {
          ...state,
          messages: patchTurn(state.messages, action.event.turn_id, (message) => ({
            ...message,
            delivery: message.role === "sora" ? "cancelled" : message.delivery,
          })),
          activeTurn: null,
          pendingSubmission: null,
        };
      }
      if (action.event.event_type === "turn.failed") {
        return {
          ...state,
          messages: patchTurn(state.messages, action.event.turn_id, (message) => ({
            ...message,
            delivery: message.role === "sora" ? "failed" : message.delivery,
          })),
          activeTurn: null,
          pendingSubmission: null,
        };
      }
      return { ...state, activeTurn: nextActive };
    }
    case "turn.failed":
      return {
        ...state,
        messages: action.turnId
          ? patchTurn(state.messages, action.turnId, (message) => ({ ...message, delivery: "failed" }))
          : state.messages,
        activeTurn: null,
        pendingSubmission: null,
        error: action.error,
      };
    case "error.dismissed":
      return { ...state, error: null };
    case "settings.changed":
      return { ...state, settingsOpen: action.open };
  }
}
