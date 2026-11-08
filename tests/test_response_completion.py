"""Explicit fixtures for incomplete model output; never an installed-E2E substitute."""
import copy
import sqlite3
from types import SimpleNamespace

import pytest

from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.conversation_runtime.contracts import CancellationToken, TurnCancelled, TurnStatus
from sora_sandbox.conversation_runtime.journal import ConversationJournal
from sora_sandbox.conversation_runtime.post_turn import PostTurnDispatcher
from sora_sandbox.conversation_runtime.routing import ModelRouteCoordinator, RoutedLLMClient
from sora_sandbox.conversation_runtime.runtime import ConversationRuntime, ConversationRuntimeConfig
from sora_sandbox.conversation_runtime.security import AtRestCipher, StaticKeyProvider
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.model_provider.adapters import OpenAIResponsesAdapter
from sora_sandbox.state import seed_state


class FixtureProvider(FakeLLMClient):
    def __init__(self, reason, *, tail=None, cancel_at_eof=False):
        self.reason = reason
        self.tail = tail or []
        self.cancel_at_eof = cancel_at_eof
        self.closed = False

    def stream_chat(self, system, user, **kwargs):
        try:
            yield {"delta": "The coast looks", "model": "fixture"}
            yield {"delta": " peaceful.", "finish_reason": self.reason}
            yield from self.tail
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 1200}}
            if self.cancel_at_eof:
                kwargs["cancellation_token"].cancel("test-cancel-at-eof")
        finally:
            self.closed = True


@pytest.fixture
def journal(tmp_path):
    value = ConversationJournal(tmp_path / "turn.db", AtRestCipher(StaticKeyProvider(b"k" * 32)))
    yield value
    value.close()


def invoke(journal, provider, token=None):
    context, _ = journal.create_turn(conversation_id="c", request_id="r", user_input="synthetic")
    client = RoutedLLMClient(provider, ModelRouteCoordinator(), journal=journal)
    with client.coordinator.activate(context.turn_id, token or CancellationToken()):
        result = client.chat("system", '{"context_pack": {}}')
    return result


@pytest.mark.parametrize("reason", ["length", "max_tokens", "MAX_TOKENS", "incomplete", None,
                                     "content_filter", "SAFETY", "tool_calls", "tool_use", "private-secret-reason"])
def test_incomplete_stream_fails_without_losing_usage(journal, reason):
    provider = FixtureProvider(reason)
    with pytest.raises(RuntimeError, match="Model response did not complete") as caught:
        invoke(journal, provider)
    assert "private-secret" not in str(caught.value)
    assert provider.closed
    with sqlite3.connect(journal.path) as connection:
        row = connection.execute("SELECT status,completion_tokens,error_code FROM runtime_model_call").fetchone()
    assert row == ("failed", 1200, "IncompleteModelResponse")


@pytest.mark.parametrize("reason", ["stop", "STOP", "end_turn", "stop_sequence"])
def test_normal_terminal_and_usage_tail_succeed(journal, reason):
    result = invoke(journal, FixtureProvider(reason))
    assert result["content"] == "The coast looks peaceful."
    assert result["usage"]["completion_tokens"] == 1200  # Token count alone is not truncation.
    assert result["raw"]["completion_status"] == "complete"


@pytest.mark.parametrize("tail", [[{"delta": "late text"}], [{"finish_reason": "length"}]])
def test_conflicting_or_post_terminal_content_is_not_success(journal, tail):
    with pytest.raises(RuntimeError, match="Model response did not complete"):
        invoke(journal, FixtureProvider("stop", tail=tail))


def test_eof_cancellation_is_not_success(journal):
    with pytest.raises(TurnCancelled):
        invoke(journal, FixtureProvider("stop", cancel_at_eof=True))


def test_truncated_generation_has_no_success_history_or_outbox(tmp_path):
    journal = ConversationJournal(tmp_path / "runtime.db", AtRestCipher(StaticKeyProvider(b"k" * 32)))
    routes = ModelRouteCoordinator()
    runner = AgentRunner(RoutedLLMClient(FixtureProvider("length"), routes, journal=journal),
                         subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
                         interruptibility=InterruptibilityService())
    runtime = ConversationRuntime(runner, journal=journal, model_routes=routes,
                                  post_turn=PostTurnDispatcher(journal),
                                  config=ConversationRuntimeConfig(start_post_turn_worker=False))
    state = seed_state()
    before = copy.deepcopy(state)
    try:
        with pytest.raises(RuntimeError, match="Model response did not complete"):
            runtime.run(state, "Describe a peaceful coast", conversation_id="c", request_id="truncated")
        assert state == before
        assert journal.history_page("c") == []
        with sqlite3.connect(journal.path) as connection:
            turn_id, status = connection.execute("SELECT turn_id,status FROM runtime_turn").fetchone()
            assert connection.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == 0
        assert status == TurnStatus.FAILED.value
        events = journal.client_events(turn_id)
        assert events[-1].event_type == "turn.failed"
        assert not any(e.event_type.startswith("response.") for e in events)
    finally:
        runtime.close()


def test_responses_api_incomplete_event_is_not_dropped():
    target = SimpleNamespace(profile=None, model=SimpleNamespace(model_id="fixture"))
    broker = SimpleNamespace(stream_sse=lambda *args, **kwargs: iter([
        {"type": "response.output_text.delta", "delta": "partial"},
        {"type": "response.incomplete", "response": {"usage": {"output_tokens": 8}}},
    ]))
    events = list(OpenAIResponsesAdapter().stream_chat(broker, target, system="s", user="u", timeout_seconds=1))
    assert events[-1]["finish_reason"] == "incomplete"
    assert events[-1]["usage"]["output_tokens"] == 8
