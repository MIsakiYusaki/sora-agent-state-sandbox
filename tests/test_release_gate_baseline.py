import pytest

from sora_sandbox.memory_quality_ops import MemoryQualityOpsService
from sora_sandbox.memory_runtime import MemoryRuntime


@pytest.fixture
def quality(tmp_path):
    memory = MemoryRuntime(tmp_path / "quality.db")
    service = MemoryQualityOpsService(memory._conn, lock=memory._lock, store_health=memory.health)
    service.register_eval_suite(
        "canonical",
        lambda: {"cases": [{"case_id": "baseline_fixture", "status": "passed"}]},
        source_module="isolated_release_gate_fixture",
    )
    yield service
    memory.close()


def test_release_requires_baseline_even_when_eval_and_slo_pass(quality):
    result = quality.run_release_gate(change_scope=["canonical"])
    assert result["status"] == "blocked"
    assert "baseline_missing" in result["blockers"]
    assert result["baseline_diff"] == {"status": "skipped", "reason": "no_active_baseline"}
    event = next(event for event in quality.list_quality_events() if event["event_type"] == "release_gate")
    assert event["severity"] == "blocker"
    assert event["status"] == "open"


def test_candidate_baseline_does_not_silently_become_active(quality):
    run = quality.run_quality_eval(suites=["canonical"])
    quality.pin_baseline(run["id"], name="fixture candidate", promote=False)
    result = quality.run_release_gate(change_scope=["canonical"])
    assert result["status"] == "blocked"
    assert "baseline_missing" in result["blockers"]


@pytest.mark.parametrize("explicit", [False, True])
def test_matching_baseline_allows_success_and_read_only_comparison_still_works(quality, explicit):
    run = quality.run_quality_eval(suites=["canonical"])
    baseline = quality.pin_baseline(run["id"], name="fixture approved baseline", promote=True)
    result = quality.run_release_gate(change_scope=["canonical"], baseline_id=baseline["id"] if explicit else None)
    assert result["status"] == "passed"
    assert result["baseline_diff"]["status"] == "passed"
    assert result["baseline_diff"]["baseline_id"] == baseline["id"]


def test_unbaselined_eval_remains_available_for_initial_baseline_creation(quality):
    run = quality.run_quality_eval(suites=["canonical"])
    assert run["status"] == "passed"
    assert quality.compare_baseline(run["id"], persist_event=False)["status"] == "skipped"
