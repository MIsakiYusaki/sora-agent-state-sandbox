"""Regression for external Desktop IDs versus canonical Memory conversation IDs."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sora_sandbox.conversation_gateway.debug_snapshot import build_desktop_debug_snapshot
from sora_sandbox.conversation_gateway.registry import GatewayRegistry
from sora_sandbox.conversation_gateway.security import VerifiedIdentity
from sora_sandbox.memory_runtime import MemoryRuntime


@pytest.fixture
def diagnostic_stack(tmp_path):
    registry = GatewayRegistry(tmp_path / 'gateway.db')
    memory = MemoryRuntime(tmp_path / 'memory.db', owner_user_id='owner', agent_id='agent')
    identity = VerifiedIdentity('cap-test', 'profile', 'owner', 'agent', 'pair', 'desktop', frozenset({'debug.read'}))
    binding = registry.ensure_conversation(profile_id='profile', subject_id='owner', agent_id='agent',
                                           external_conversation_id='desktop-conversation')
    assert binding.runtime_conversation_id != binding.external_conversation_id
    result = memory.observe_committed_turn({
        'event_id': 'event-turn', 'turn_id': 'turn', 'event_type': 'conversation.turn_committed',
        'payload': {'runtime_turn_id': 'turn', 'conversation_id': binding.runtime_conversation_id,
                    'user_id': 'owner', 'agent_id': 'agent', 'correlation_id': 'corr-turn',
                    'user_input': 'I like walking near the sea every evening because the sound of waves helps me relax after a busy day.',
                    'reply': 'That sounds peaceful.'},
    })
    assert result['job_id']
    diagnostics = Mock(return_value=[{'turn_id': 'turn', 'correlation_id': 'corr-turn'}])
    service = SimpleNamespace(registry=registry, runtime=SimpleNamespace(diagnostic_turns=diagnostics),
                              readiness=lambda: {'status': 'ready'})
    stack = SimpleNamespace(memory=SimpleNamespace(runtime=memory), persona=None, affect=None,
                            relationship=None, worldops=None, interruptibility=None, model_provider_system=None)
    try:
        yield stack, service, identity, binding, result['job_id'], diagnostics
    finally:
        memory.close()
        registry.close()


def snapshot(parts, *, identity=None, conversation='desktop-conversation'):
    stack, service, actual_identity, *_ = parts
    return build_desktop_debug_snapshot(stack=stack, service=service,
                                       identity=identity or actual_identity, conversation_id=conversation)


def test_external_conversation_resolves_memory_jobs(diagnostic_stack):
    result = snapshot(diagnostic_stack)
    job = result.timeline['turns'][0]['memory_jobs']['jobs'][0]
    assert job['job_id'] == diagnostic_stack[4]
    assert job['turn_id'] == 'turn'
    assert result.conversation_id == 'desktop-conversation'
    diagnostic_stack[5].assert_called_once_with(diagnostic_stack[3].runtime_conversation_id, limit=20)


@pytest.mark.parametrize('field,value', [('profile_id', 'foreign'), ('subject_id', 'foreign'), ('agent_id', 'foreign')])
def test_foreign_identity_cannot_read_turns_or_jobs(diagnostic_stack, field, value):
    result = snapshot(diagnostic_stack, identity=replace(diagnostic_stack[2], **{field: value}))
    assert result.timeline['turns'] == []
    diagnostic_stack[5].assert_not_called()


def test_canonical_id_cannot_bypass_external_binding(diagnostic_stack):
    result = snapshot(diagnostic_stack, conversation=diagnostic_stack[3].runtime_conversation_id)
    assert result.timeline['turns'] == []
    diagnostic_stack[5].assert_not_called()


def test_mismatched_correlation_does_not_attach_job(diagnostic_stack):
    diagnostic_stack[5].return_value = [{'turn_id': 'turn', 'correlation_id': 'other-correlation'}]
    assert snapshot(diagnostic_stack).timeline['turns'][0]['memory_jobs']['jobs'] == []
