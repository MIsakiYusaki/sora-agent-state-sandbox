import { describe, expect, it } from "vitest";
import type { DesktopStatus, GatewayEvent, HistoryPage } from "../contracts/generated";
import {
  conversationReducer,
  initialConversationState,
  type ConversationState,
  type ConversationMessage,
} from "./state";

const userMessage: ConversationMessage = {
  id: "local_1",
  role: "user",
  content: "今天有点累",
  turnId: "local_1",
  sequence: Number.MAX_SAFE_INTEGER,
  createdAtMs: 1,
  delivery: "pending",
};

function event(sequence: number, eventType: string, payload: Record<string, unknown>): GatewayEvent {
  return {
    schema_version: "GatewayEvent.v1",
    protocol_version: "1.0",
    turn_id: "turn_1",
    sequence,
    event_type: eventType,
    correlation_id: "0123456789abcdef0123456789abcdef",
    payload,
    created_at_ms: sequence,
    replayed: false,
  };
}

function status(): DesktopStatus {
  return {
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
}

function history(): HistoryPage {
  return {
    schema_version: "HistoryPage.v1",
    protocol_version: "1.0",
    conversation_id: "conversation_main",
    conversation_version: 0,
    items: [],
    next_before_sequence: null,
  };
}

describe("conversationReducer", () => {
  it("preserves a draft across startup failure and retry", () => {
    const failed = conversationReducer(initialConversationState, {
      type: "boot.failed",
      error: {
        schema_version: "ShellError.v1",
        code: "INTERNAL_ERROR",
        message: "temporary startup failure",
        retryable: true,
        retry_after_ms: null,
        correlation_id: null,
        action: "retry",
        safe_details: {},
      },
    });
    const withDraft = conversationReducer(failed, {
      type: "draft.changed",
      value: "启动失败也不要丢掉这句话",
    });
    const retrying = conversationReducer(withDraft, { type: "boot.loading" });
    const restored = conversationReducer(retrying, {
      type: "boot.ready",
      status: status(),
      history: history(),
    });

    expect(restored.draft).toBe("启动失败也不要丢掉这句话");
  });

  it("keeps the visible draft until acceptance and never erases newer edits", () => {
    let state = conversationReducer(initialConversationState, {
      type: "draft.changed",
      value: userMessage.content,
    });
    state = conversationReducer(state, { type: "turn.submitting", message: userMessage });
    expect(state.draft).toBe(userMessage.content);

    state = conversationReducer(state, { type: "draft.changed", value: "下一条草稿" });
    state = conversationReducer(state, {
      type: "turn.accepted",
      localMessageId: userMessage.id,
      accepted: {
        schema_version: "TurnAccepted.v1",
        protocol_version: "1.0",
        turn_id: "turn_1",
        request_id: "request_1",
        conversation_id: "conversation_main",
        correlation_id: "0123456789abcdef0123456789abcdef",
        status: "accepted",
        conversation_version: 1,
        idempotent_replay: false,
        events_url: "/events",
      },
    });

    expect(state.draft).toBe("下一条草稿");
  });

  it("refreshes provider readiness without rebooting conversation state", () => {
    const ready = status();
    const setupRequired = {
      ...ready,
      phase: "provider_setup_required" as const,
      provider_ready: false,
      status_message: "请先连接一个可用模型",
    };
    const booted = conversationReducer(initialConversationState, {
      type: "boot.ready",
      status: setupRequired,
      history: history(),
    });

    const refreshed = conversationReducer(booted, {
      type: "status.refreshed",
      status: ready,
    });

    expect(refreshed.status?.phase).toBe("ready");
    expect(refreshed.messages).toBe(booted.messages);
    expect(refreshed.conversationVersion).toBe(booted.conversationVersion);
  });

  it("assembles streamed deltas exactly once and commits the visible message", () => {
    let state = conversationReducer(initialConversationState, {
      type: "turn.submitting",
      message: userMessage,
    });
    state = conversationReducer(state, {
      type: "turn.accepted",
      localMessageId: userMessage.id,
      accepted: {
        schema_version: "TurnAccepted.v1",
        protocol_version: "1.0",
        turn_id: "turn_1",
        request_id: "request_1",
        conversation_id: "conversation_main",
        correlation_id: "0123456789abcdef0123456789abcdef",
        status: "accepted",
        conversation_version: 1,
        idempotent_replay: false,
        events_url: "/events",
      },
    });
    state = conversationReducer(state, { type: "gateway.event", event: event(1, "response.delta", { delta: "我在" }) });
    state = conversationReducer(state, { type: "gateway.event", event: event(2, "response.delta", { delta: "这里" }) });
    state = conversationReducer(state, { type: "gateway.event", event: event(2, "response.delta", { delta: "重复" }) });
    state = conversationReducer(state, { type: "gateway.event", event: event(3, "turn.committed", { conversation_version: 2 }) });

    expect(state.messages.find((message) => message.role === "sora")).toMatchObject({
      content: "我在这里",
      delivery: "committed",
    });
    expect(state.activeTurn).toBeNull();
    expect(state.conversationVersion).toBe(2);
  });

  it("keeps orthogonal cancellation state without erasing partial output", () => {
    let state: ConversationState = {
      ...initialConversationState,
      activeTurn: { turnId: "turn_1", requestId: "request_1", stage: "response.delta", lastSequence: 1 },
      messages: [{ ...userMessage, id: "sora_1", role: "sora" as const, turnId: "turn_1", content: "先休息", delivery: "streaming" as const }],
    };
    state = conversationReducer(state, { type: "gateway.event", event: event(2, "turn.cancelled", {}) });
    expect(state.messages[0]).toMatchObject({ content: "先休息", delivery: "cancelled" });
    expect(state.activeTurn).toBeNull();
  });
});
