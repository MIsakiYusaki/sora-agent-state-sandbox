"""Cancellation identity and receipt tests; no external model is invoked."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from sora_sandbox.conversation_gateway.service import ConversationGatewayService
from sora_sandbox.conversation_gateway.runtime_port import LocalConversationRuntimeAdapter
from sora_sandbox.conversation_runtime.contracts import TurnStatus
from sora_sandbox.conversation_runtime.coordinator import TurnCoordinator
from sora_sandbox.conversation_runtime.journal import ConversationJournal
from sora_sandbox.conversation_runtime.runtime import ConversationRuntime
from sora_sandbox.conversation_runtime.security import AtRestCipher, StaticKeyProvider


@pytest.fixture
def cancellation_stack(tmp_path):
    journal = ConversationJournal(tmp_path / 'cancel.db', AtRestCipher(StaticKeyProvider(b'k' * 32)))
    coordinator = TurnCoordinator(journal)
    runtime = SimpleNamespace(journal=journal, coordinator=coordinator, _evidence_lock=threading.RLock(), _active_evidence={})
    runtime.cancel = lambda conversation_id, **kwargs: ConversationRuntime.cancel(runtime, conversation_id, **kwargs)
    try:
        yield journal, coordinator, runtime
    finally:
        coordinator.close()
        journal.close()


def create_turn(journal, request_id, conversation_id='conversation'):
    context, _ = journal.create_turn(conversation_id=conversation_id, request_id=request_id, user_input='synthetic')
    return context


def test_gateway_forwards_exact_requested_turn():
    binding = SimpleNamespace(runtime_conversation_id='runtime-conversation', turn_id='old-turn')
    service = SimpleNamespace(_require_turn=Mock(return_value=binding), runtime=Mock(), _status_view=Mock())
    ConversationGatewayService.cancel(service, object(), 'old-turn', reason='user_cancelled')
    service.runtime.cancel.assert_called_once_with('runtime-conversation', turn_id='old-turn', reason='user_cancelled')


def test_late_old_turn_does_not_cancel_new_execution(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    old = create_turn(journal, 'old')
    current = create_turn(journal, 'current')
    execution = coordinator._claim('conversation', current.turn_id)
    try:
        assert not runtime.cancel('conversation', turn_id=old.turn_id, reason='synthetic-sensitive-reason')
        assert not execution.cancellation.cancelled
        receipt = journal.artifacts(old.turn_id, 'cancellation_receipt')[-1]['payload']
        assert receipt['signal_changed'] is False
        assert 'synthetic-sensitive-reason' not in str(receipt)
    finally:
        coordinator._release(execution)


def test_exact_turn_signals_once_and_records_receipts(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'current')
    journal.transition(current.turn_id, TurnStatus.PREPARING, stage='preparing')
    execution = coordinator._claim('conversation', current.turn_id)
    try:
        adapter = LocalConversationRuntimeAdapter(runtime)
        assert adapter.cancel('conversation', turn_id=current.turn_id, reason='user_cancelled')
        assert execution.cancellation.cancelled
        assert journal.get_turn(current.turn_id).status == TurnStatus.CANCEL_REQUESTED
        assert not adapter.cancel('conversation', turn_id=current.turn_id, reason='user_cancelled')
        receipts = journal.artifacts(current.turn_id, 'cancellation_receipt')
        assert len(receipts) == 2
        assert [item['payload']['signal_changed'] for item in receipts] == [True, False]
        for item in receipts:
            receipt = item['payload']
            assert receipt['received_at_ms'] <= receipt['returned_at_ms']
            assert receipt['elapsed_ms'] >= 0
            assert set(receipt) == {'received_at_ms', 'returned_at_ms', 'elapsed_ms', 'status_before', 'status_after', 'signal_changed'}
        assert journal.history_page('conversation') == []
        assert journal.client_events(current.turn_id) == []
    finally:
        coordinator._release(execution)


def test_terminal_request_does_not_signal(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'terminal')
    journal.transition(current.turn_id, TurnStatus.FAILED, stage='failed')
    assert not runtime.cancel('conversation', turn_id=current.turn_id, reason='user_cancelled')
    receipt = journal.artifacts(current.turn_id, 'cancellation_receipt')[-1]['payload']
    assert receipt['status_before'] == receipt['status_after'] == 'failed'
    assert not receipt['signal_changed']


def test_wrong_conversation_fails_closed_without_receipt(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'wrong-owner')
    with pytest.raises(ValueError):
        runtime.cancel('other', turn_id=current.turn_id, reason='user_cancelled')
    assert journal.artifacts(current.turn_id, 'cancellation_receipt') == []


def test_legacy_internal_conversation_cancel_remains_supported(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'internal')
    execution = coordinator._claim('conversation', current.turn_id)
    try:
        assert runtime.cancel('conversation', reason='runtime_shutdown')
        assert execution.cancellation.cancelled
    finally:
        coordinator._release(execution)


def test_gateway_late_cancel_cannot_stop_another_turn(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    old = create_turn(journal, 'old-gateway')
    current = create_turn(journal, 'current-gateway')
    execution = coordinator._claim('conversation', current.turn_id)
    binding = SimpleNamespace(runtime_conversation_id='conversation', turn_id=old.turn_id)
    service = SimpleNamespace(_require_turn=Mock(return_value=binding),
                              runtime=LocalConversationRuntimeAdapter(runtime), _status_view=Mock())
    try:
        ConversationGatewayService.cancel(service, object(), old.turn_id)
        assert not execution.cancellation.cancelled
        assert journal.artifacts(old.turn_id, 'cancellation_receipt')[-1]['payload']['signal_changed'] is False
    finally:
        coordinator._release(execution)


def test_receipt_does_not_claim_committing_turn_is_cancelled(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'commit-race')
    for status in (TurnStatus.PREPARING, TurnStatus.GENERATING, TurnStatus.COMMITTING):
        journal.transition(current.turn_id, status, stage=status.value)
    execution = coordinator._claim('conversation', current.turn_id)
    try:
        assert runtime.cancel('conversation', turn_id=current.turn_id)
        assert journal.get_turn(current.turn_id).status == TurnStatus.COMMITTING
        receipt = journal.artifacts(current.turn_id, 'cancellation_receipt')[-1]['payload']
        assert receipt['signal_changed'] is True
        assert receipt['status_after'] == 'committing'
    finally:
        coordinator._release(execution)


def test_diagnostic_receipt_is_content_free_and_scoped(cancellation_stack):
    journal, coordinator, runtime = cancellation_stack
    current = create_turn(journal, 'diagnostic')
    journal.transition(current.turn_id, TurnStatus.FAILED, stage='failed')
    runtime.cancel('conversation', turn_id=current.turn_id, reason='secret-custom-reason')
    diagnostic = ConversationRuntime.diagnostic_turns(runtime, 'conversation')[0]
    assert len(diagnostic['cancellation_receipts']) == 1
    assert 'secret-custom-reason' not in str(diagnostic)
    assert 'synthetic' not in str(diagnostic)
    assert ConversationRuntime.diagnostic_turns(runtime, 'other') == []
