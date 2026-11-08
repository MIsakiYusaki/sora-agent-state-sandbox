"""Temporary stores only. Production defaults never invent a user's memories."""
import os
from unittest.mock import patch

from sora_sandbox.conversation_gateway.state import ProfileStateRepository
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.paths import SEEDS_DIR
from sora_sandbox.product_migrations import retire_untouched_example_memories
from sora_sandbox.state import StateStore, load_jsonl, seed_state


def test_new_default_runtime_and_reset_have_no_example_memories(tmp_path):
    with patch.dict(os.environ, {"SORA_SANDBOX_STATE_DIR": str(tmp_path)}):
        runtime = MemoryRuntime.default(owner_user_id="fixture-user", agent_id="fixture-agent")
    try:
        assert runtime.list_memories() == []
        runtime.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))
        assert len(runtime.list_memories()) == 3
        runtime.reset()
        assert runtime.list_memories() == []
    finally:
        runtime.close()


def test_state_entry_points_start_empty_and_fixtures_explicitly_opt_in(tmp_path):
    assert seed_state()["memory"]["items"] == []
    assert len(seed_state(include_example_memories=True)["memory"]["items"]) == 3
    store = StateStore(tmp_path / "state.json")
    assert store.load()["memory"]["items"] == []
    assert store.reset()["memory"]["items"] == []
    repository = ProfileStateRepository(tmp_path / "profiles", profile_scope="fixture-profile")
    assert repository.load("fixture-profile")["memory"]["items"] == []


def test_legacy_retirement_is_audited_idempotent_and_preserves_user_edits(tmp_path):
    runtime = MemoryRuntime(tmp_path / "memory.db", owner_user_id="fixture-user", agent_id="fixture-agent")
    try:
        examples = load_jsonl(SEEDS_DIR / "memory.seed.jsonl")
        runtime.seed_if_empty(examples)
        edited_id = examples[0]["id"]
        corrected = runtime.revise(edited_id, "这是用户后来纠正的真实偏好")
        retired = retire_untouched_example_memories(runtime)
        assert set(retired) == {item["id"] for item in examples[1:]}
        assert runtime.get_memory(edited_id)["status"] == "superseded"
        assert runtime.get_memory(corrected["id"])["status"] == "active"
        # Matching text with real provenance must survive as well.
        real = {**examples[1], "id": "fixture-real-memory"}
        assert runtime.import_legacy_memories([real]) == 1
        assert runtime.get_memory(real["id"])["status"] == "active"
        for memory_id in retired:
            assert runtime.get_memory(memory_id)["status"] == "disabled"
            assert runtime.revisions_for_memory(memory_id)[0]["reason"] == "bundled_example_retired"
            assert runtime.evidence_for_memory(memory_id)
        assert retire_untouched_example_memories(runtime) == []
        runtime.restore(retired[0])
        assert retire_untouched_example_memories(runtime) == []
    finally:
        runtime.close()


def test_same_bundled_id_with_real_provenance_is_preserved(tmp_path):
    runtime = MemoryRuntime(tmp_path / "memory.db")
    try:
        examples = load_jsonl(SEEDS_DIR / "memory.seed.jsonl")
        assert runtime.import_legacy_memories(examples) == 3
        assert retire_untouched_example_memories(runtime) == []
        assert len(runtime.list_memories(status="active")) == 3
    finally:
        runtime.close()


def test_normal_stack_startup_retires_legacy_examples_after_registering_adapters(tmp_path):
    from sora_sandbox.llm import FakeLLMClient
    from sora_sandbox.runtime_stack import SoraRuntimeStack
    with patch.dict(os.environ, {"SORA_SANDBOX_STATE_DIR": str(tmp_path), "SORA_SANDBOX_CONFIG_DIR": str(tmp_path / "config")}):
        runtime = MemoryRuntime.default(owner_user_id="fixture-user", agent_id="fixture-agent")
        runtime.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))
        runtime.close()
        stack = SoraRuntimeStack.build(FakeLLMClient(), start_workers=False, owner_user_id="fixture-user", agent_id="fixture-agent", tenant_id="fixture-tenant")
        try:
            assert stack.memory.list_memories(status="active") == []
            assert len(stack.memory.list_memories(status="disabled")) == 3
        finally:
            stack.close()


def test_product_migration_preserves_other_owners_in_an_existing_store(tmp_path):
    runtime = MemoryRuntime(tmp_path / "memory.db", owner_user_id="fixture-first")
    runtime.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))
    runtime.close()
    runtime = MemoryRuntime(tmp_path / "memory.db", owner_user_id="fixture-other")
    try:
        assert retire_untouched_example_memories(runtime) == []
        for example in load_jsonl(SEEDS_DIR / "memory.seed.jsonl"):
            assert runtime.get_memory(example["id"])["status"] == "active"
    finally:
        runtime.close()
