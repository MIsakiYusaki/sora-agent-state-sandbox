from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from .prompt_loader import load_prompt_registry
from .render import render_trace_panel
from .state import (
    add_memory_item,
    apply_growth_event,
    commit_memory_hook,
    find_world_event,
    find_memory_hook,
    load_origin_templates,
    list_memory_hooks,
    now_ms,
    reject_growth_event,
    reject_memory_hook,
    select_origin_template,
    summarize_state,
)


JsonDict = dict[str, Any]


class CommandError(RuntimeError):
    pass


def handle_command(
    line: str,
    state: JsonDict,
    memory_admin: Any | None = None,
    interruptibility_admin: Any | None = None,
    persona_admin: Any | None = None,
    affect_admin: Any | None = None,
) -> tuple[str, bool]:
    parts = shlex.split(line, posix=False)
    if not parts:
        return "", False
    command = parts[0].lower()
    args = parts[1:]

    if command in {"/exit", "/quit", "/q"}:
        return "再见。状态已经保存在本地。", True
    if command in {"/help", "/?"}:
        return help_text(), False
    if command == "/state":
        return dump(summarize_state(state)), False
    if command == "/trace":
        result = state["observability"].get("last_run_result", {})
        if not result:
            return "暂无 Agent 轨迹。先输入一句话跑一轮。", False
        return render_trace_panel(result), False
    if command == "/trace-json":
        return dump(state["observability"].get("last_trace", [])), False
    if command == "/state-diff":
        return dump(state["observability"].get("last_state_diff", {})), False
    if command == "/response-plan":
        return dump(state["observability"].get("last_response_plan", {})), False
    if command == "/signals":
        return dump(state["observability"].get("last_context_pack", {}).get("signals", [])), False
    if command == "/subsystems":
        return dump(state["observability"].get("last_context_pack", {}).get("subsystem_health", [])), False
    if command == "/interpretation":
        return dump(state["observability"].get("last_context_pack", {}).get("turn_interpretation", {})), False
    if command == "/context-pack":
        return dump(state["observability"].get("last_context_pack", {})), False
    if command == "/interruptibility":
        if interruptibility_admin is None:
            return dump(state.get("interruptibility", {})), False
        return dump(handle_interruptibility_command(args, state, interruptibility_admin)), False
    if command == "/turn-debug":
        mode = arg_value(args[0]).lower() if args else "summary"
        return dump(build_turn_debug_snapshot(state, mode=mode)), False
    if command == "/arbitration":
        return dump(state["observability"].get("last_context_pack", {}).get("arbitration_result", {})), False
    if command == "/initiative":
        return dump({
            "state": state.get("initiative", {}),
            "last_window": state["observability"].get("last_context_pack", {}).get("initiative_window", {}),
        }), False
    if command == "/proactive":
        return dump(state.get("initiative", {})), False
    if command == "/set-idle-threshold":
        return set_idle_threshold(args, state), False
    if command == "/origin":
        return dump(state.get("origin", {})), False
    if command == "/origins":
        return dump(load_origin_templates()), False
    if command == "/set-origin":
        return set_origin_command(args, state), False
    if command == "/reroll-origin":
        return reroll_origin_command(state), False
    if command == "/prompts":
        return list_prompts(), False
    if command == "/prompt":
        return show_prompt(args), False
    if command in {"/show-history", "/history"}:
        return show_history(state), False
    if command == "/clear-history":
        state["session"]["recent_turns"] = []
        state["session"]["rolling_summary"] = ""
        return "短期对话历史已清空。", False

    if command == "/memories":
        if memory_admin is not None:
            return dump(memory_admin.list_memories()), False
        return dump(state["memory"]["items"]), False
    if command == "/add-memory":
        if memory_admin is not None:
            return dump(add_memory_runtime(args, memory_admin)), False
        return add_memory(args, state), False
    if command == "/search-memory":
        if memory_admin is not None:
            return dump(memory_admin.search(" ".join(args).strip('"'))), False
        return search_memory(args, state), False
    if command == "/disable-memory":
        if memory_admin is not None:
            return dump(memory_admin.disable(required_arg(args, "/disable-memory <id>"))), False
        return set_memory_status(args, state, "disabled"), False
    if command == "/delete-memory":
        if memory_admin is not None:
            return dump(memory_admin.delete(required_arg(args, "/delete-memory <id>"))), False
        return set_memory_status(args, state, "deleted"), False
    if command == "/restore-memory":
        require_memory_admin(memory_admin)
        return dump(memory_admin.restore(required_arg(args, "/restore-memory <id>"))), False
    if command == "/rollback-memory-candidate":
        require_memory_admin(memory_admin)
        candidate_id = required_arg(args, "/rollback-memory-candidate <candidate_id> [reason]")
        reason = arg_value(" ".join(args[1:])) if len(args) > 1 else "manual_rollback"
        return dump(memory_admin.rollback_committed_candidate(candidate_id, reason=reason)), False
    if command == "/memory-candidates":
        require_memory_admin(memory_admin)
        status = arg_value(args[0]).lower() if args else "pending"
        return dump(memory_admin.list_candidates(None if status == "all" else status)), False
    if command == "/gate-memory-candidate":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.run_candidate_gate(
                required_arg(args, "/gate-memory-candidate <candidate_id>")
            )
        ), False
    if command == "/run-batch-memory-gate":
        require_memory_admin(memory_admin)
        candidate_ids = _positional_args(args)
        if not candidate_ids:
            raise CommandError("用法：/run-batch-memory-gate <candidate_id> [candidate_id...]")
        return dump(memory_admin.run_batch_gate(candidate_ids)), False
    if command == "/memory-gate-decisions":
        require_memory_admin(memory_admin)
        limit = _int_arg(args, "--limit", default=50)
        candidate_id = _first_positional(args)
        return dump(
            memory_admin.list_gate_decisions(candidate_id=candidate_id, limit=limit)
        ), False
    if command in {"/memory-review-items", "/memory-reviews"}:
        require_memory_admin(memory_admin)
        limit = _int_arg(args, "--limit", default=50)
        status = (_first_positional(args) or "open").lower()
        return dump(
            memory_admin.list_review_items(
                status=None if status == "all" else status,
                limit=limit,
            )
        ), False
    if command == "/approve-memory-review":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.approve_review(
                required_arg(args, "/approve-memory-review <review_id>")
            )
        ), False
    if command == "/reject-memory-review":
        require_memory_admin(memory_admin)
        review_id = required_arg(args, "/reject-memory-review <review_id> [reason]")
        reason = arg_value(" ".join(args[1:])) if len(args) > 1 else "manual_review_reject"
        return dump(memory_admin.reject_review(review_id, reason=reason)), False
    if command == "/defer-memory-review":
        require_memory_admin(memory_admin)
        review_id = required_arg(args, "/defer-memory-review <review_id> [--days N]")
        days = _int_arg(args, "--days", default=30)
        return dump(memory_admin.defer_review(review_id, days=days)), False
    if command == "/memory-quarantines":
        require_memory_admin(memory_admin)
        limit = _int_arg(args, "--limit", default=50)
        status = (_first_positional(args) or "active").lower()
        return dump(
            memory_admin.list_quarantines(
                status=None if status == "all" else status,
                limit=limit,
            )
        ), False
    if command == "/expire-deferred-memory-candidates":
        require_memory_admin(memory_admin)
        return dump(memory_admin.expire_deferred_candidates()), False
    if command == "/process-expired-memory-deferrals":
        require_memory_admin(memory_admin)
        return dump(memory_admin.process_expired_deferrals()), False
    if command == "/commit-memory":
        require_memory_admin(memory_admin)
        return dump(memory_admin.commit([required_arg(args, "/commit-memory <candidate_id>")])), False
    if command == "/reject-memory":
        require_memory_admin(memory_admin)
        return dump(memory_admin.reject(required_arg(args, "/reject-memory <candidate_id>"))), False
    if command == "/revise-memory":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("用法：/revise-memory <memory_id> <新内容>")
        return dump(memory_admin.revise(arg_value(args[0]), arg_value(" ".join(args[1:])))), False
    if command == "/memory-evidence":
        require_memory_admin(memory_admin)
        return dump(memory_admin.evidence_for_memory(required_arg(args, "/memory-evidence <id>"))), False
    if command == "/memory-evidence-items":
        require_memory_admin(memory_admin)
        limit = _int_arg(args, "--limit", default=100)
        offset = _int_arg(args, "--offset", default=0)
        status = (_value_after(args, "--status") or _first_positional(args) or "active").lower()
        source_type = _value_after(args, "--source-type")
        sensitivity = _value_after(args, "--sensitivity")
        return dump(
            memory_admin.list_evidence(
                status=None if status == "all" else status,
                source_type=source_type,
                sensitivity=sensitivity,
                limit=limit,
                offset=offset,
            )
        ), False
    if command == "/memory-evidence-detail":
        require_memory_admin(memory_admin)
        return dump(memory_admin.get_evidence(required_arg(args, "/memory-evidence-detail <evidence_id>"))), False
    if command == "/link-memory-evidence":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("用法：/link-memory-evidence <memory_id> <evidence_id>")
        return dump(memory_admin.link_evidence(arg_value(args[0]), arg_value(args[1]))), False
    if command == "/unlink-memory-evidence":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("用法：/unlink-memory-evidence <memory_id> <evidence_id>")
        return dump(memory_admin.unlink_evidence(arg_value(args[0]), arg_value(args[1]))), False
    if command == "/delete-memory-evidence":
        require_memory_admin(memory_admin)
        evidence_id = required_arg(args, "/delete-memory-evidence <evidence_id> [reason]")
        reason = arg_value(" ".join(args[1:])) if len(args) > 1 else "manual_evidence_delete"
        return dump(memory_admin.delete_evidence(evidence_id, reason=reason)), False
    if command == "/memory-store-audit":
        require_memory_admin(memory_admin)
        return dump(memory_admin.store_audit()), False
    if command == "/repair-memory-fts":
        require_memory_admin(memory_admin)
        return dump(memory_admin.repair_fts_index(actor="manual_repl")), False
    if command == "/process-memory-index-outbox":
        require_memory_admin(memory_admin)
        return dump(memory_admin.process_index_outbox(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/memory-index-outbox":
        require_memory_admin(memory_admin)
        status = _value_after(args, "--status")
        return dump(memory_admin.list_index_outbox(status=status, limit=_int_arg(args, "--limit", default=50))), False
    if command == "/memory-index-health":
        require_memory_admin(memory_admin)
        return dump(memory_admin.index_health()), False
    if command == "/rebuild-memory-index":
        require_memory_admin(memory_admin)
        backend_value = _value_after(args, "--backend")
        backends = tuple(backend_value.split(",")) if backend_value else ("fts", "vector", "graph")
        return dump(
            memory_admin.rebuild_memory_indexes(
                backends=backends,
                batch_size=_int_arg(args, "--batch-size", default=100),
                process="--no-process" not in args,
            )
        ), False
    if command == "/memory-index-rebuilds":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_index_rebuilds(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/requeue-memory-index-deadletters":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.requeue_index_dead_letters(
                limit=_int_arg(args, "--limit", default=50),
                reason=_value_after(args, "--reason") or "manual_requeue",
            )
        ), False
    if command == "/cleanup-recall-traces":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.cleanup_recall_traces(
                older_than_days=_int_arg(args, "--days", default=30),
                keep_pinned="--include-pinned" not in args,
            )
        ), False
    if command == "/process-memory-projectors":
        require_memory_admin(memory_admin)
        return dump(memory_admin.process_projector_outbox(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/memory-projector-outbox":
        require_memory_admin(memory_admin)
        status = _value_after(args, "--status")
        return dump(memory_admin.list_projector_outbox(status=status, limit=_int_arg(args, "--limit", default=50))), False
    if command == "/memory-projector-checkpoints":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_projector_checkpoints()), False
    if command == "/memory-projection-snapshots":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.list_projection_snapshots(
                subsystem=_value_after(args, "--subsystem"),
                status=_value_after(args, "--status") or "active",
                limit=_int_arg(args, "--limit", default=50),
            )
        ), False
    if command == "/memory-projection-frame":
        require_memory_admin(memory_admin)
        subsystem = required_arg(args, "/memory-projection-frame <relationship|persona|worldops>")
        return dump(memory_admin.get_projection_frame(subsystem)), False
    if command == "/rebuild-memory-projections":
        require_memory_admin(memory_admin)
        subsystem_value = _value_after(args, "--subsystems")
        return dump(
            memory_admin.rebuild_projections(
                subsystems=subsystem_value.split(",") if subsystem_value else None,
                limit=_int_arg(args, "--limit", default=500),
            )
        ), False
    if command == "/memory-projection-rebuilds":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_projection_rebuilds(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/memory-projection-audits":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.list_projection_audits(
                action=_value_after(args, "--action"),
                subsystem=_value_after(args, "--subsystem"),
                limit=_int_arg(args, "--limit", default=50),
            )
        ), False
    if command == "/memory-projection-proposals":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.list_projection_proposals(
                status=_value_after(args, "--status"),
                limit=_int_arg(args, "--limit", default=50),
            )
        ), False
    if command == "/process-memory-retention":
        require_memory_admin(memory_admin)
        return dump(memory_admin.process_retention(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/run-memory-lifecycle":
        require_memory_admin(memory_admin)
        mode = (_first_positional(args) or "dry_run").lower()
        return dump(
            memory_admin.run_lifecycle(
                mode=mode,
                limit=_int_arg(args, "--limit", default=200),
                create_consolidation_candidates=("--create-candidates" in args) or None,
            )
        ), False
    if command == "/memory-lifecycle-states":
        require_memory_admin(memory_admin)
        tier = (_first_positional(args) or "all").lower()
        return dump(
            memory_admin.list_lifecycle_states(
                tier=None if tier == "all" else tier,
                limit=_int_arg(args, "--limit", default=100),
            )
        ), False
    if command == "/memory-lifecycle-health":
        require_memory_admin(memory_admin)
        return dump(memory_admin.lifecycle_health()), False
    if command == "/memory-heat-runs":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_heat_runs(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/memory-consolidation-plans":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.list_consolidation_plans(
                status=_value_after(args, "--status"),
                limit=_int_arg(args, "--limit", default=100),
            )
        ), False
    if command == "/memory-tombstones":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_tombstones(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/run-lifecycle-memory-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_lifecycle_eval()), False
    if command == "/memory-quality-suites":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_quality_eval_suites()), False
    if command == "/run-memory-quality-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_quality_eval(suites=_csv_arg(args, "--suites"))), False
    if command == "/memory-quality-runs":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_quality_runs(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/memory-quality-run":
        require_memory_admin(memory_admin)
        return dump(memory_admin.get_quality_run(required_arg(args, "/memory-quality-run <run_id>"))), False
    if command == "/memory-quality-trace-sync":
        require_memory_admin(memory_admin)
        return dump(memory_admin.sync_quality_trace_index(limit=_int_arg(args, "--limit", default=500))), False
    if command == "/memory-quality-dashboard":
        require_memory_admin(memory_admin)
        return dump(memory_admin.quality_dashboard()), False
    if command == "/memory-quality-metrics":
        require_memory_admin(memory_admin)
        return dump(memory_admin.collect_quality_metrics()), False
    if command == "/pin-memory-quality-baseline":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("Usage: /pin-memory-quality-baseline <run_id> <name> [--promote]")
        return dump(
            memory_admin.pin_quality_baseline(
                arg_value(args[0]),
                name=arg_value(args[1]),
                promote="--promote" in args,
            )
        ), False
    if command == "/memory-quality-baselines":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_quality_baselines(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/run-memory-release-gate":
        require_memory_admin(memory_admin)
        return dump(
            memory_admin.run_release_gate(
                change_scope=_csv_arg(args, "--scope"),
                baseline_id=_value_after(args, "--baseline"),
            )
        ), False
    if command == "/memory-admin-actions":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_admin_action_specs()), False
    if command == "/memory-admin-preview":
        require_memory_admin(memory_admin)
        if not args:
            raise CommandError("Usage: /memory-admin-preview <action_name> [json_params]")
        params = json.loads(arg_value(" ".join(args[1:]))) if len(args) > 1 else {}
        return dump(memory_admin.preview_admin_action(arg_value(args[0]), params)), False
    if command == "/memory-admin-execute":
        require_memory_admin(memory_admin)
        action_id = required_arg(args, "/memory-admin-execute <action_id> [confirmation_token]")
        token = arg_value(args[1]) if len(args) > 1 else None
        return dump(memory_admin.execute_admin_action(action_id, confirmation_token=token)), False
    if command == "/forget-memory":
        require_memory_admin(memory_admin)
        memory_id = required_arg(args, "/forget-memory <memory_id> [--redact]")
        return dump(memory_admin.forget_memory(memory_id, mode="redact" if "--redact" in args else "delete")), False
    if command == "/forget-memory-evidence":
        require_memory_admin(memory_admin)
        evidence_id = required_arg(args, "/forget-memory-evidence <evidence_id> [--redact]")
        return dump(memory_admin.forget_evidence(evidence_id, mode="redact" if "--redact" in args else "delete")), False
    if command == "/memory-forget-requests":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_forget_requests(limit=_int_arg(args, "--limit", default=50))), False
    if command == "/memory-store-health":
        require_memory_admin(memory_admin)
        return dump(memory_admin.store_health()), False
    if command == "/run-canonical-memory-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_canonical_eval()), False
    if command == "/canonical-memory-evals":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_canonical_eval_runs(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/run-recall-memory-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_recall_eval()), False
    if command == "/recall-memory-evals":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_recall_eval_runs(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/run-planner-memory-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_planner_eval()), False
    if command == "/planner-memory-evals":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_planner_eval_runs(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/plan-recall":
        require_memory_admin(memory_admin)
        return dump(memory_admin.plan_recall(joined_arg(args, "/plan-recall <query>"))), False
    if command == "/prefetch-recall":
        require_memory_admin(memory_admin)
        return dump(memory_admin.prefetch_recall(joined_arg(args, "/prefetch-recall <query>"))), False
    if command == "/recall-plan-cache":
        require_memory_admin(memory_admin)
        return dump(memory_admin.list_recall_plan_cache(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/recall-plan-traces":
        require_memory_admin(memory_admin)
        limit = int(arg_value(args[0])) if args else 20
        return dump(memory_admin.list_recall_plan_traces(limit)), False
    if command == "/memory-episodes":
        require_memory_admin(memory_admin)
        status = arg_value(args[0]).lower() if args else "all"
        return dump(memory_admin.list_episodes(status=None if status == "all" else status)), False
    if command == "/memory-events":
        require_memory_admin(memory_admin)
        episode_id = arg_value(args[0]) if args else None
        return dump(memory_admin.list_events(episode_id=episode_id)), False
    if command == "/memory-extraction-jobs":
        require_memory_admin(memory_admin)
        status = arg_value(args[0]).lower() if args else "all"
        return dump(memory_admin.list_extraction_jobs(status=None if status == "all" else status)), False
    if command == "/memory-episode-trace":
        require_memory_admin(memory_admin)
        return dump(memory_admin.get_episode_trace(required_arg(args, "/memory-episode-trace <episode_id>"))), False
    if command == "/memory-replay-episode":
        require_memory_admin(memory_admin)
        return dump(memory_admin.replay_episode(required_arg(args, "/memory-replay-episode <episode_id>"))), False
    if command == "/memory-delete-propagations":
        require_memory_admin(memory_admin)
        source_ref = arg_value(args[0]) if args else None
        return dump(memory_admin.list_delete_propagations(source_ref=source_ref)), False
    if command == "/seal-memory-episode":
        require_memory_admin(memory_admin)
        return dump(memory_admin.seal_episode(required_arg(args, "/seal-memory-episode <episode_id>"))), False
    if command == "/delete-memory-event":
        require_memory_admin(memory_admin)
        return dump(memory_admin.propagate_event_delete(required_arg(args, "/delete-memory-event <event_id>"))), False
    if command == "/claim-extraction-job":
        require_memory_admin(memory_admin)
        return dump(memory_admin.claim_next_extraction_job()), False
    if command == "/run-extraction-job":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_extraction_once()), False
    if command == "/backfill-extraction-jobs":
        require_memory_admin(memory_admin)
        force = "--force" in args
        limit = _int_arg(args, "--limit", default=50)
        return dump(memory_admin.backfill_extraction_jobs(limit=limit, force=force)), False
    if command == "/memory-extraction-observability":
        require_memory_admin(memory_admin)
        limit = _int_arg(args, "--limit", default=10)
        return dump(memory_admin.extraction_observability(limit=limit)), False
    if command == "/start-extraction-job":
        require_memory_admin(memory_admin)
        return dump(memory_admin.start_extraction_job(required_arg(args, "/start-extraction-job <job_id>"))), False
    if command == "/complete-extraction-job":
        require_memory_admin(memory_admin)
        return dump(memory_admin.complete_extraction_job(required_arg(args, "/complete-extraction-job <job_id>"))), False
    if command == "/retry-extraction-job":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("Usage: /retry-extraction-job <job_id> <error>")
        return dump(
            memory_admin.fail_extraction_job(
                arg_value(args[0]),
                error=arg_value(" ".join(args[1:])),
                retry=True,
            )
        ), False
    if command == "/fail-extraction-job":
        require_memory_admin(memory_admin)
        if len(args) < 2:
            raise CommandError("用法：/fail-extraction-job <job_id> <error>")
        return dump(memory_admin.fail_extraction_job(arg_value(args[0]), error=arg_value(" ".join(args[1:])))), False
    if command == "/memory-revisions":
        require_memory_admin(memory_admin)
        return dump(memory_admin.revisions_for_memory(required_arg(args, "/memory-revisions <id>"))), False
    if command == "/recall-traces":
        require_memory_admin(memory_admin)
        limit = int(arg_value(args[0])) if args else 20
        return dump(memory_admin.list_recall_traces(limit)), False
    if command == "/surface-traces":
        require_memory_admin(memory_admin)
        limit = int(arg_value(args[0])) if args else 20
        return dump(memory_admin.list_surface_policy_traces(limit)), False
    if command == "/surface-eval":
        require_memory_admin(memory_admin)
        return dump(memory_admin.run_surface_eval()), False
    if command == "/memory-health":
        require_memory_admin(memory_admin)
        return dump(memory_admin.health()), False
    if command == "/memory":
        return "交互式 REPL 中输入 /memory 可进入 Memory Console。", False
    if command == "/memory-hooks":
        return dump(list_memory_hooks(state)), False
    if command == "/commit-memory-hook":
        return commit_memory_hook_command(args, state, memory_admin), False
    if command == "/reject-memory-hook":
        return reject_memory_hook_command(args, state, memory_admin), False

    if command == "/persona":
        if persona_admin is not None:
            return dump(persona_admin.export_state()), False
        return dump(state["persona"]), False
    if command == "/persona-core":
        if persona_admin is not None:
            return dump(persona_admin.store.definition()), False
        return dump(state["persona"]["persona_core"]), False
    if command == "/traits":
        if persona_admin is not None:
            return dump({"snapshot": persona_admin.store.active_snapshot(), "definitions": persona_admin.store.trait_definitions()}), False
        return dump(state["persona"]["persona_traits"]), False
    if command == "/set-trait":
        if persona_admin is not None:
            raise CommandError("Direct trait mutation is disabled. Use reviewed Persona candidates or a versioned definition release.")
        return set_trait(args, state), False
    if command == "/short-state":
        return dump(state["persona"]["short_term_state"]), False
    if command == "/set-mood":
        return set_short_state(args, state, "mood"), False
    if command == "/set-energy":
        return set_short_state(args, state, "energy"), False
    if command == "/interests":
        return dump(state["persona"]["interest_graph"]), False
    if command == "/set-interest":
        return set_interest(args, state), False
    if command == "/judgment":
        return dump(state["persona"]["judgment_policy"]), False
    if command == "/set-judgment":
        return set_judgment(args, state), False
    if command == "/growth-events":
        if persona_admin is not None:
            status = arg_value(args[0]) if args else None
            return dump(persona_admin.store.list_candidates(status=status, limit=100)), False
        return dump(state["persona"].get("growth_events", [])), False
    if command == "/apply-growth-event":
        if persona_admin is not None:
            return dump(persona_admin.approve_candidate(required_arg(args, "/apply-growth-event <candidate_id>"))), False
        return apply_growth_event_command(args, state), False
    if command == "/reject-growth-event":
        if persona_admin is not None:
            return dump(persona_admin.reject_candidate(required_arg(args, "/reject-growth-event <candidate_id>"))), False
        return reject_growth_event_command(args, state), False
    if command == "/persona-health":
        require_persona_admin(persona_admin)
        return dump(persona_admin.health()), False
    if command == "/persona-worker":
        require_persona_admin(persona_admin)
        return dump(persona_admin.process_pending(limit=_int_arg(args, "--limit", default=20))), False
    if command == "/persona-contributions":
        require_persona_admin(persona_admin)
        return dump(persona_admin.store.list_contributions(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/persona-rollback":
        require_persona_admin(persona_admin)
        contribution_id = required_arg(args, "/persona-rollback <contribution_id> [reason]")
        reason = arg_value(" ".join(args[1:])) if len(args) > 1 else "manual_rollback"
        return dump(persona_admin.rollback_contribution(contribution_id, reason=reason)), False
    if command == "/persona-rebuild":
        require_persona_admin(persona_admin)
        reason = arg_value(" ".join(args)) if args else "manual_rebuild"
        return dump(persona_admin.rebuild(reason=reason)), False
    if command == "/persona-forget-source":
        require_persona_admin(persona_admin)
        return dump(persona_admin.forget_source(required_arg(args, "/persona-forget-source <source_ref>"))), False
    if command == "/persona-growth":
        require_persona_admin(persona_admin)
        action = required_arg(args, "/persona-growth <pause|resume>").lower()
        if action not in {"pause", "resume"}:
            raise CommandError("Usage: /persona-growth <pause|resume>")
        return dump(persona_admin.set_growth_paused(action == "pause")), False
    if command == "/persona-self-reference":
        require_persona_admin(persona_admin)
        permission = required_arg(args, "/persona-self-reference <silent|implicit|explicit>").lower()
        return dump(persona_admin.set_self_reference_permission(permission)), False

    if command == "/affect":
        require_affect_admin(affect_admin)
        return dump(affect_admin.export_state()), False
    if command == "/affect-health":
        require_affect_admin(affect_admin)
        return dump(affect_admin.health()), False
    if command == "/affect-worker":
        require_affect_admin(affect_admin)
        return dump(
            affect_admin.process_pending(
                limit=_int_arg(args, "--limit", default=20),
                force="--force" in args,
            )
        ), False
    if command == "/affect-contributions":
        require_affect_admin(affect_admin)
        return dump(affect_admin.store.list_contributions(limit=_int_arg(args, "--limit", default=100))), False
    if command == "/affect-rollback":
        require_affect_admin(affect_admin)
        contribution_id = required_arg(args, "/affect-rollback <contribution_id> [reason]")
        reason = arg_value(" ".join(args[1:])) if len(args) > 1 else "manual_rollback"
        return dump(affect_admin.rollback_contribution(contribution_id, reason=reason)), False
    if command == "/affect-rebuild":
        require_affect_admin(affect_admin)
        reason = arg_value(" ".join(args)) if args else "manual_rebuild"
        return dump(affect_admin.rebuild(reason=reason)), False
    if command == "/affect-forget-source":
        require_affect_admin(affect_admin)
        return dump(
            affect_admin.forget_source(
                required_arg(args, "/affect-forget-source <source_ref>"),
            )
        ), False
    if command == "/affect-processing":
        require_affect_admin(affect_admin)
        action = required_arg(args, "/affect-processing <pause|resume>").lower()
        if action not in {"pause", "resume"}:
            raise CommandError("Usage: /affect-processing <pause|resume>")
        return dump(
            affect_admin.set_processing_paused(
                action == "pause",
                user_id=state.get("session", {}).get("user_id"),
            )
        ), False
    if command == "/affect-expression":
        require_affect_admin(affect_admin)
        permission = required_arg(args, "/affect-expression <silent|implicit|explicit>").lower()
        return dump(
            affect_admin.set_expression_permission(
                permission,
                user_id=state.get("session", {}).get("user_id"),
            )
        ), False

    if command == "/worldops":
        return dump({k: v for k, v in state["worldops"].items() if k != "content_pack"} | {"content_pack_summary": {"world_name": state["worldops"]["content_pack"]["world_name"], "events": len(state["worldops"]["content_pack"].get("events", []))}}), False
    if command == "/worldops-events":
        return dump(state["worldops"]["content_pack"].get("events", [])), False
    if command == "/activate-world-event":
        return activate_world_event(args, state), False
    if command == "/set-world-expression":
        return set_world_expression(args, state), False
    if command == "/start-micro-quest":
        return start_micro_quest(args, state), False
    if command == "/complete-micro-quest":
        return complete_micro_quest(args, state), False
    if command == "/ignore-world-event":
        return ignore_world_event(args, state), False
    if command == "/worldops-memory-hooks":
        return dump(state["worldops"].get("memory_hooks", [])), False

    if command == "/behavior":
        return dump(state["behavior"]), False
    if command == "/set-behavior":
        return set_behavior(args, state), False

    if command == "/export-state":
        return export_state(args, state), False

    raise CommandError(f"未知命令：{command}。输入 /help 查看命令。")


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_turn_debug_snapshot(state: JsonDict, *, mode: str = "summary") -> JsonDict:
    result = state["observability"].get("last_run_result", {}) or {}
    context_pack = state["observability"].get("last_context_pack", {}) or {}
    if mode not in {"summary", "full"}:
        raise CommandError("Usage: /turn-debug [summary|full]")
    if mode == "full":
        return {
            "mode": "full",
            "user_input": result.get("user_input", ""),
            "turn_kind": result.get("turn_kind"),
            "proactive_trigger": result.get("proactive_trigger"),
            "context_pack": context_pack,
            "response_plan": state["observability"].get("last_response_plan", {}),
            "expression_event": result.get("expression_event"),
            "state_diff": state["observability"].get("last_state_diff", {}),
            "trace": state["observability"].get("last_trace", []),
            "timing": result.get("timing", {}),
            "token_usage": result.get("token_usage", {}),
        }
    return {
        "mode": "summary",
        "user_input": result.get("user_input", ""),
        "turn_kind": result.get("turn_kind"),
        "turn_interpretation": context_pack.get("turn_interpretation", {}),
        "turn_frame": context_pack.get("turn_frame", {}),
        "context_use_policy": context_pack.get("context_use_policy", {}),
        "session_context": context_pack.get("session_context", {}),
        "event_bus": context_pack.get("event_bus", {}),
        "recollection_frame": context_pack.get("recollection_frame", {}),
        "persona_frame": context_pack.get("persona_frame", {}),
        "origin_frame": context_pack.get("origin_frame", {}),
        "worldops_frame": context_pack.get("worldops_frame", {}),
        "dialogue_control_frame": context_pack.get("dialogue_control_frame", {}),
        "interruptibility_frame": context_pack.get("interruptibility_frame", {}),
        "signals": context_pack.get("signals", []),
        "subsystem_health": context_pack.get("subsystem_health", []),
        "initiative_window": context_pack.get("initiative_window", {}),
        "arbitration_result": context_pack.get("arbitration_result", {}),
        "response_plan": state["observability"].get("last_response_plan", {}),
        "expression_event": result.get("expression_event"),
        "state_diff": state["observability"].get("last_state_diff", {}),
        "timing": result.get("timing", {}),
    }


def arg_value(value: str) -> str:
    value = value.strip().strip('"')
    if len(value) >= 2 and value[0] == "<" and value[-1] == ">":
        return value[1:-1].strip()
    return value


def required_arg(args: list[str], usage: str) -> str:
    if not args:
        raise CommandError(f"用法：{usage}")
    return arg_value(args[0])


def joined_arg(args: list[str], usage: str) -> str:
    if not args:
        raise CommandError(f"用法：{usage}")
    return arg_value(" ".join(args))


def _int_arg(args: list[str], flag: str, *, default: int) -> int:
    if flag not in args:
        return default
    index = args.index(flag)
    if index + 1 >= len(args):
        raise CommandError(f"用法：{flag} <number>")
    return int(arg_value(args[index + 1]))


def _first_positional(args: list[str]) -> str | None:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("--"):
            skip_next = True
            continue
        return arg_value(arg)
    return None


def _value_after(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        raise CommandError(f"用法：{flag} <value>")
    return arg_value(args[index + 1])


def _csv_arg(args: list[str], flag: str) -> list[str] | None:
    value = _value_after(args, flag)
    if value is None:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values or None


def handle_interruptibility_command(args: list[str], state: JsonDict, service: Any) -> JsonDict:
    action = arg_value(args[0]).lower() if args else "status"
    if action == "status":
        return service.status(state)
    if action == "set":
        if len(args) < 3:
            raise CommandError(
                "用法：/interruptibility set <control> <value> [--scope session|duration|persistent] [--duration 15m]"
            )
        control = arg_value(args[1])
        value = _control_value(arg_value(args[2]))
        scope = _value_after(args, "--scope") or "session"
        duration = _duration_seconds(_value_after(args, "--duration"))
        if duration is not None:
            scope = "duration"
        return service.set_control(
            state,
            control=control,
            value=value,
            scope=scope,
            duration_seconds=duration,
        )
    if action == "confirm":
        token = arg_value(args[1]) if len(args) > 1 else None
        return service.confirm(state, token)
    if action == "revoke":
        control = arg_value(args[1]) if len(args) > 1 else "all"
        return service.revoke(state, control)
    if action == "traces":
        limit = int(arg_value(args[1])) if len(args) > 1 else 50
        return {"traces": service.list_traces(state, limit=limit)}
    if action == "export":
        output_path = arg_value(args[1]) if len(args) > 1 else None
        return service.export_traces(state, output_path=output_path)
    if action == "feedback":
        kind = required_arg(args[1:], "/interruptibility feedback <kind>")
        last_frame = state.get("interruptibility", {}).get("last_frame", {})
        return service.record_feedback(state, kind=kind, trace_id=last_frame.get("trace_id"))
    raise CommandError(
        "用法：/interruptibility [status|set|confirm|revoke|traces|export|feedback]"
    )


def _control_value(value: str) -> Any:
    normalized = value.lower()
    if normalized in {"true", "on", "yes", "1"}:
        return True
    if normalized in {"false", "off", "no", "0"}:
        return False
    return value


def _duration_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?P<number>\d+)(?P<unit>[smhd]?)", value.strip().lower())
    if not match:
        raise CommandError("--duration 支持 30s、15m、2h、1d")
    multipliers = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(match.group("number")) * multipliers[match.group("unit")]


def _positional_args(args: list[str]) -> list[str]:
    values: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("--"):
            skip_next = True
            continue
        values.append(arg_value(arg))
    return values


def require_memory_admin(memory_admin: Any | None) -> None:
    if memory_admin is None:
        raise CommandError("Memory Runtime 未启用。")


def require_persona_admin(persona_admin: Any | None) -> None:
    if persona_admin is None:
        raise CommandError("Persona System is not enabled.")


def require_affect_admin(affect_admin: Any | None) -> None:
    if affect_admin is None:
        raise CommandError("Affect System is not enabled.")


def help_text() -> str:
    return """Sora Agent State Sandbox 命令

说明：尖括号表示占位符，实际输入时不用带尖括号。
例：/delete-memory mem_seed_workflow_001

对话：
  <任意文字>
      走完整 Agent 链路：Event Bus -> 多子系统 Signal -> Initiative Window -> Arbiter -> Express。

观察与调试：
  /state
      查看当前状态摘要：memory 数量、persona 摘要、active WorldOps、behavior、session。
  /trace
      查看上一轮可读轨迹面板，适合确认 Runtime phases、Signal timings 和 Arbiter 裁决。
  /trace-json
      查看上一轮原始 trace 事件流，适合调试节点事件。
  /state-diff
      查看上一轮产生了哪些状态影响候选，比如记忆使用、WorldOps 使用、表达路由。
  /response-plan
      查看上一轮 Agent Runtime 的回应计划，不包含真实自动写入长期状态。
  /signals
      查看上一轮各子系统产出的标准 Signal，适合判断哪些系统想发声。
  /subsystems
      查看上一轮子系统 Adapter 的状态、延迟、Signal 数量和降级原因。
  /interpretation
      查看上一轮 Turn Interpreter 的意图、指代消解、领域风险和世界观框架判断。
  /context-pack
      查看上一轮完整 context_pack；适合一次性检查所有注入给 LLM 的运行时上下文。
  /interruptibility [status|set|confirm|revoke|traces|export|feedback]
      查看或管理回应深度、追问、切题、quiet、response mute、state surface consent 与 activity lease。
  /turn-debug [summary|full]
      按流水线查看上一轮输入后的中间结果：意图识别、turn_frame、signals、Arbiter、response_plan、state_diff 等。
  /arbitration
      查看上一轮 Arbiter 结果：selected/background/suppressed signals 与回复指令。
  /initiative
      查看主动性状态和上一轮 initiative_window，适合调试 Sora 是否该主动开口。
  /proactive
      查看后台主动搭话配置、最近尝试、上次发言时间和冷却参数。
  /simulate-idle [3m]
      立即模拟一次无用户输入的空闲唤醒；支持 30s、3m、1h 等时长。
  /set-idle-threshold <分钟>
      修改后台空闲触发阈值。默认 3 分钟；例：/set-idle-threshold 3
  /origin
      查看当前 Origin Template，也就是 Sora 的初始生活底色。
  /origins
      列出所有可用 Origin Template。
  /set-origin <id>
      手动切换 Origin Template，并重新应用初始 trait 偏移、WorldOps 入口和主动性 bias。
  /reroll-origin
      随机重选一个 Origin Template，适合测试冷启动差异。
  /prompts
      查看当前 prompt 版本和各 prompt 文件路径。
  /prompt system|user_context|dialogue_control
      打印指定 prompt 文件内容，方便直接查看正在生效的规则。
  /show-history 或 /history
      查看短期对话窗口，当前保留最近 20 轮 user/Sora 消息。
  /clear-history
      清空短期对话窗口，用于验证无上下文时的回复稳定性。

Memory：
  /memory
      进入本地 Memory Console；可查看候选、证据、修订和召回轨迹。
  /memories
      列出 SQLite canonical memory。
  /add-memory <内容> --type <类型> --tags <tag1,tag2>
      手动添加一条记忆。例：/add-memory 用户喜欢先聊方案 --type preference --tags workflow,agent
  /search-memory <关键词>
      按关键词搜索 active memory。
  /disable-memory <id>
      禁用一条 memory，让召回不再使用它。
  /delete-memory <id>
      标记删除一条 memory；当前是软删除，不直接物理移除。
  /restore-memory <id>
      恢复一条 disabled/deleted memory。
  /rollback-memory-candidate <candidate_id> [reason]
      回滚已 committed 的候选，并同步 memory 状态、FTS 与 revision。
  /memory-candidates [pending|committed|rejected|all]
      查看 Memory Runtime 写入候选。
  /gate-memory-candidate <candidate_id>
      对 pending/deferred 候选运行 Gate Pipeline。
  /run-batch-memory-gate <candidate_id> [candidate_id...]
      批量运行 Gate Pipeline。
  /memory-gate-decisions [candidate_id] [--limit N]
      查看 GateDecision 审计记录。
  /memory-review-items [open|resolved|all] [--limit N]
      查看人工评审队列。
  /approve-memory-review <review_id> /reject-memory-review <review_id> [reason]
      处理 Gate 送审候选。
  /defer-memory-review <review_id> [--days N]
      手动推迟一个 review 项，默认 30 天后过期。
  /memory-quarantines [active|all] [--limit N]
      查看隔离区记录。
  /expire-deferred-memory-candidates
      将超过 deferred_until 的候选自动拒绝。
  /process-expired-memory-deferrals
      deferred 过期处理别名，用于对齐 Module 03 设计文档。
  /commit-memory <candidate_id> /reject-memory <candidate_id>
      提交或拒绝记忆候选。
  /memory-evidence <id> /memory-revisions <id>
      查看记忆证据与修订历史。
  /memory-evidence-items [active|deleted|all] [--source-type X] [--sensitivity X] [--limit N] [--offset N]
      列出 canonical evidence。
  /memory-evidence-detail <evidence_id>
      查看单条 evidence。
  /link-memory-evidence <memory_id> <evidence_id>
      手动将 evidence 关联到 memory，并记录 revision。
  /unlink-memory-evidence <memory_id> <evidence_id>
      手动解绑 evidence；如 memory 无 active evidence 会自动禁用。
  /delete-memory-evidence <evidence_id> [reason]
      软删除 evidence，并级联拒绝候选、禁用无证据 memory、移除 FTS。
  /memory-store-audit /repair-memory-fts
      检查 canonical store 一致性，或重建 active memory 的 FTS 索引。
  /process-memory-index-outbox /memory-index-outbox
      处理或查看 FTS/vector/graph index outbox。
  /process-memory-projectors /memory-projector-outbox /memory-projector-checkpoints
  /memory-projection-snapshots /memory-projection-frame <subsystem> /rebuild-memory-projections
      处理或查看 relationship/worldops/persona projector outbox 与 checkpoint。
  /process-memory-retention /forget-memory <id> [--redact] /forget-memory-evidence <id> [--redact]
      执行 retention worker 或用户 forget/redaction 流程。
  /run-memory-lifecycle [dry_run|heat_only|consolidate|retention|forget|backfill] [--limit N]
      运行 Module 08 lifecycle worker，默认 dry_run，只写审计 run/snapshot。
  /memory-lifecycle-states [hot|warm|cold|archived|all] /memory-lifecycle-health
      查看 heat/tier 状态与 lifecycle 健康信息。
  /memory-heat-runs /memory-consolidation-plans /memory-tombstones /run-lifecycle-memory-eval
      查看 heat run、巩固计划、tombstone，或运行 lifecycle eval。
  /memory-store-health /run-canonical-memory-eval /canonical-memory-evals
      查看 canonical store 健康状态，运行或查看 canonical eval。
  /plan-recall <query> /prefetch-recall <query> /run-planner-memory-eval
      调试 Recall Planner、写入预取计划，或运行 planner eval。
  /recall-plan-cache /recall-plan-traces /planner-memory-evals
      查看 planner cache、planner trace 和 planner eval 历史。
  /run-extraction-job
      领取并执行一个 pending extraction_job；默认调用真实 OpenAI-compatible LLM。
  /backfill-extraction-jobs [--limit N] [--force]
      为已封口/已抽取 episode 创建补抽取 job；--force 会允许重跑已有 completed job。
  /memory-extraction-observability [--limit N]
      查看 extractor job 状态、候选分布、token/cost/latency 和最近 job。
  /recall-traces [数量] /memory-health
      查看召回轨迹和本地 Runtime 健康状态。
  /memory-quality-dashboard /run-memory-quality-eval [--suites a,b] /run-memory-release-gate [--scope a,b]
      Module 10 quality ops: dashboard, unified eval registry, trace index, baseline diff and release gate.
  /memory-admin-actions /memory-admin-preview <action> [json] /memory-admin-execute <action_id> [token]
      Module 10 governed admin action wrapper with dry-run, audit, confirmation and rollback plan.
  /pin-memory-quality-baseline <run_id> <name> [--promote] /memory-quality-baselines
      Pin or inspect QualityOps baselines for release regression checks.
  /memory-quality-trace-sync /memory-quality-metrics /memory-quality-runs
      Sync trace index, collect SLO metrics and inspect quality run history.
  /memory-hooks
      查看待提交或已处理的记忆写入候选。
  /commit-memory-hook <id>
      手动确认一个记忆候选，写入长期 Memory。
  /reject-memory-hook <id>
      拒绝一个记忆候选，不写入长期 Memory。

Persona：
  /persona
      查看完整 persona 状态。
  /persona-core
      查看 Sora 的核心身份、世界观链接和安全边界。
  /traits
      查看长期特质参数。
  /set-trait <name> <value> --debug
      手动调整长期特质，并记录 debug override。例：/set-trait playfulness 0.55 --debug
  /short-state
      查看短期 mood/energy/current_focus。
  /set-mood <mood>
      设置短期心情。例：/set-mood 好奇
  /set-energy <energy>
      设置短期精力。例：/set-energy 低
  /interests
      查看兴趣图谱。
  /set-interest <topic> sora_interest=0.7 shared_density=0.8
      手动调整兴趣图谱。shared_density 会写入 shared_experience_density。
  /judgment
      查看判断倾向参数。
  /set-judgment <dimension> <value> --debug
      手动调整判断倾向，并记录 debug override。例：/set-judgment risk_tolerance 0.35 --debug
  /growth-events
      查看 Persona 成长候选事件；当前只观察，不自动应用。
  /apply-growth-event <id>
      手动应用一个成长候选，小幅调整对应 Persona trait。
  /reject-growth-event <id>
      拒绝一个成长候选，不改变 Persona。

Affect:
  /affect /affect-health
      Inspect the canonical Affect export or operational health without raw user text.
  /affect-processing pause|resume
      Pause or resume Affect processing for the active user.
  /affect-expression silent|implicit|explicit
      Set the active user's Affect expression consent.
  /affect-worker [--limit N] [--force]
      Process bounded semantic appraisal jobs with the configured real provider.
  /affect-contributions [--limit N]
      Inspect replayable Affect contributions.
  /affect-rollback <contribution_id> [reason] /affect-rebuild [reason]
      Roll back one contribution or run a deterministic shadow-parity rebuild.
  /affect-forget-source <source_ref>
      Irreversibly redact and tombstone a source, then rebuild derived state.

WorldOps：
  /worldops
      查看 WorldOps runtime 摘要和 active event。
  /worldops-events
      列出世界观事件池。
  /activate-world-event <id>
      激活一个世界事件，让下一轮回复可受其影响。
  /set-world-expression <id> L0|L1|L2|L3
      手动设置事件默认表达等级：L0 静默，L1 轻提，L2 气泡/短句，L3 邀请互动。
  /start-micro-quest <id>
      启动某个事件的微任务，适合验证 WorldOps 进入互动支线。
  /complete-micro-quest <id> --choice <内容>
      完成微任务并生成 memory hook 候选，不会直接写长期记忆。
  /ignore-world-event <id>
      标记用户忽略/拒绝该事件，并从 active event 中移除。
  /worldops-memory-hooks
      查看 WorldOps 产生的共同经历写入候选。

Behavior：
  /behavior
      查看当前 mock 行为感知状态和可用 preset。
  /set-behavior casual_idle|project_design|focus_work|game_companion|watch_companion
      切换 mock 行为场景；切换后一轮 changed=true，随后变为沿用。

状态管理：
  /export-state [path]
      导出完整 sandbox state，默认写到 sora-sandbox-state-export.json。
  /help 或 /?
      显示本帮助。
  /exit /quit /q
      保存状态并退出。"""


def list_prompts() -> str:
    registry = load_prompt_registry()
    return dump({
        "version": registry.version,
        "system": str(registry.system),
        "user_context": str(registry.user_context),
        "dialogue_control": str(registry.dialogue_control),
    })


def show_prompt(args: list[str]) -> str:
    if not args:
        raise CommandError("用法：/prompt system|user_context|dialogue_control")
    name = arg_value(args[0])
    registry = load_prompt_registry()
    prompts = {
        "system": registry.system,
        "user_context": registry.user_context,
        "dialogue_control": registry.dialogue_control,
    }
    if name not in prompts:
        raise CommandError("未知 prompt 名称。可用：system, user_context, dialogue_control")
    return prompts[name].read_text(encoding="utf-8")


def show_history(state: JsonDict) -> str:
    turns = state["session"].get("recent_turns", [])
    if not turns:
        return "暂无短期对话历史。"
    lines = [f"短期对话历史：{len(turns)} 条（约 {len(turns) // 2} 轮，窗口上限 20 轮）"]
    for turn in turns[-10:]:
        role = "用户" if turn.get("role") == "user" else "Sora"
        content = str(turn.get("content", ""))
        if len(content) > 100:
            content = content[:97] + "..."
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def set_origin_command(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/set-origin <id>")
    try:
        template = select_origin_template(state, arg_value(args[0]), mode="manual_set")
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return dump({
        "selected": template,
        "origin": state.get("origin", {}),
        "persona_core": state.get("persona", {}).get("persona_core", {}),
    })


def reroll_origin_command(state: JsonDict) -> str:
    try:
        template = select_origin_template(state, mode="manual_reroll")
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return dump({
        "selected": template,
        "origin": state.get("origin", {}),
        "persona_core": state.get("persona", {}).get("persona_core", {}),
    })


def add_memory(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/add-memory <内容> --type preference --tags a,b")
    memory_type = "episode"
    tags: list[str] = []
    content_parts: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--type" and i + 1 < len(args):
            memory_type = args[i + 1]
            i += 2
        elif args[i] == "--tags" and i + 1 < len(args):
            tags = [tag.strip() for tag in args[i + 1].split(",") if tag.strip()]
            i += 2
        else:
            content_parts.append(args[i])
            i += 1
    content = " ".join(content_parts).strip('"')
    item = add_memory_item(state, content, memory_type, tags)
    return f"已添加 memory: {item['id']}"


def add_memory_runtime(args: list[str], memory_admin: Any) -> JsonDict:
    if not args:
        raise CommandError("用法：/add-memory <内容> --type preference --tags a,b")
    memory_type = "preference"
    tags: list[str] = []
    content_parts: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--type" and i + 1 < len(args):
            memory_type = arg_value(args[i + 1])
            i += 2
        elif args[i] == "--tags" and i + 1 < len(args):
            tags = [tag.strip() for tag in arg_value(args[i + 1]).split(",") if tag.strip()]
            i += 2
        else:
            content_parts.append(args[i])
            i += 1
    content = arg_value(" ".join(content_parts))
    if not content:
        raise CommandError("记忆内容不能为空。")
    try:
        return memory_admin.add_manual(content, memory_type=memory_type, tags=tags)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def search_memory(args: list[str], state: JsonDict) -> str:
    query = " ".join(args).strip('"')
    rows = []
    for item in state["memory"]["items"]:
        haystack = " ".join([item.get("content", ""), " ".join(item.get("tags", []))])
        if query in haystack and item.get("status") == "active":
            rows.append(item)
    return dump(rows)


def set_memory_status(args: list[str], state: JsonDict, status: str) -> str:
    if not args:
        raise CommandError("需要 memory id。")
    memory_id = arg_value(args[0])
    for item in state["memory"]["items"]:
        if item["id"] == memory_id:
            item["status"] = status
            return f"{memory_id} -> {status}"
    raise CommandError(f"找不到 memory: {memory_id}")


def commit_memory_hook_command(
    args: list[str],
    state: JsonDict,
    memory_admin: Any | None = None,
) -> str:
    if not args:
        raise CommandError("用法：/commit-memory-hook <id>")
    try:
        result = commit_memory_hook(state, arg_value(args[0]))
        if memory_admin is None:
            return dump(result)

        legacy_memory = result["memory"]
        summary = legacy_memory["content"]
        pending = [
            candidate
            for candidate in memory_admin.list_candidates("pending")
            if candidate.get("summary") == summary
        ]
        if pending:
            canonical = memory_admin.commit([pending[0]["candidate_id"]])["committed"][0]
        else:
            canonical = memory_admin.add_manual(
                summary,
                memory_type=legacy_memory.get("type", "episode"),
                tags=legacy_memory.get("tags", []),
            )
        _, hook = find_memory_hook(state, arg_value(args[0]))
        hook["committed_memory_id"] = canonical["id"]
        state["memory"]["items"] = [
            item for item in state["memory"]["items"] if item.get("id") != legacy_memory["id"]
        ]
        result["hook"]["committed_memory_id"] = canonical["id"]
        result["memory"] = canonical
        return dump(result)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def reject_memory_hook_command(
    args: list[str],
    state: JsonDict,
    memory_admin: Any | None = None,
) -> str:
    if not args:
        raise CommandError("用法：/reject-memory-hook <id>")
    try:
        _, hook = find_memory_hook(state, arg_value(args[0]))
        summary = hook.get("episode_summary") or hook.get("content")
        result = reject_memory_hook(state, arg_value(args[0]))
        if memory_admin is not None and summary:
            for candidate in memory_admin.list_candidates("pending"):
                if candidate.get("summary") == summary:
                    memory_admin.reject(
                        candidate["candidate_id"],
                        reason=f"legacy_hook_rejected:{arg_value(args[0])}",
                    )
                    break
        return dump(result)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def set_trait(args: list[str], state: JsonDict) -> str:
    if len(args) < 2:
        raise CommandError("用法：/set-trait <name> <value> --debug")
    name = arg_value(args[0])
    value = float(arg_value(args[1]))
    traits = state["persona"]["persona_traits"]
    if name not in traits:
        raise CommandError(f"未知 trait: {name}")
    old = traits[name]["value"]
    value = max(traits[name]["min_bound"], min(traits[name]["max_bound"], value))
    traits[name]["value"] = value
    state["persona"].setdefault("debug_overrides", []).append({
        "target": f"persona_traits.{name}",
        "old": old,
        "new": value,
        "reason": "manual_repl_debug",
        "at": now_ms(),
    })
    return f"{name}: {old} -> {value} (debug override)"


def set_short_state(args: list[str], state: JsonDict, key: str) -> str:
    if not args:
        raise CommandError(f"用法：/set-{key} <value>")
    old = state["persona"]["short_term_state"].get(key)
    new = " ".join(args).strip('"')
    state["persona"]["short_term_state"][key] = new
    return f"{key}: {old} -> {new}"


def set_interest(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/set-interest <topic> sora_interest=0.7 shared_density=0.8")
    topic = arg_value(args[0])
    item = state["persona"]["interest_graph"].setdefault(topic, {
        "user_interest": 0.0,
        "shared_experience_density": 0.0,
        "sora_interest": 0.0,
        "emotional_valence": 0.0,
        "curiosity_reason": "manual_debug",
        "current_attitude": "观察中",
    })
    for pair in args[1:]:
        if "=" in pair:
            key, raw = pair.split("=", 1)
            normalized = "shared_experience_density" if key == "shared_density" else key
            item[normalized] = float(raw)
    return dump({topic: item})


def set_judgment(args: list[str], state: JsonDict) -> str:
    if len(args) < 2:
        raise CommandError("用法：/set-judgment <dimension> <value> --debug")
    name = arg_value(args[0])
    value = float(arg_value(args[1]))
    policy = state["persona"]["judgment_policy"]
    if name not in policy:
        raise CommandError(f"未知 judgment dimension: {name}")
    old = policy[name]["value"]
    policy[name]["value"] = max(0.0, min(1.0, value))
    state["persona"].setdefault("debug_overrides", []).append({
        "target": f"judgment_policy.{name}",
        "old": old,
        "new": policy[name]["value"],
        "reason": "manual_repl_debug",
        "at": now_ms(),
    })
    return f"{name}: {old} -> {policy[name]['value']} (debug override)"


def apply_growth_event_command(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/apply-growth-event <id>")
    try:
        return dump(apply_growth_event(state, arg_value(args[0])))
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def reject_growth_event_command(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/reject-growth-event <id>")
    try:
        return dump(reject_growth_event(state, arg_value(args[0])))
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def activate_world_event(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("需要 event id。")
    event_id = arg_value(args[0])
    event = find_world_event(state["worldops"], event_id)
    if not event:
        raise CommandError(f"找不到 WorldOps event: {event_id}")
    state["worldops"]["active_event_id"] = event_id
    return f"已激活 WorldOps event: {event_id}"


def set_world_expression(args: list[str], state: JsonDict) -> str:
    if len(args) < 2:
        raise CommandError("用法：/set-world-expression <id> L0|L1|L2|L3")
    event_id = arg_value(args[0])
    event = find_world_event(state["worldops"], event_id)
    if not event:
        raise CommandError(f"找不到 WorldOps event: {event_id}")
    level = arg_value(args[1]).upper()
    if level not in {"L0", "L1", "L2", "L3", "L4"}:
        raise CommandError("表达等级必须是 L0-L4。")
    event["expression_default"] = level
    return f"{event['event_id']} expression_default -> {level}"


def start_micro_quest(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("需要 event id。")
    event_id = arg_value(args[0])
    event = find_world_event(state["worldops"], event_id)
    if not event:
        raise CommandError(f"找不到 WorldOps event: {event_id}")
    if not event.get("micro_quest", {}).get("enabled"):
        raise CommandError("该事件没有启用 micro_quest。")
    session = {
        "quest_id": f"quest_{now_ms()}",
        "event_id": event["event_id"],
        "stage": "invite",
        "goal": event["micro_quest"]["goal"],
        "status": "active",
        "can_exit_anytime": True,
        "no_penalty": True,
        "started_at": now_ms(),
    }
    state["worldops"].setdefault("micro_quest_sessions", []).append(session)
    state["worldops"]["active_event_id"] = event["event_id"]
    return dump(session)


def complete_micro_quest(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/complete-micro-quest <id> --choice <内容>")
    event_id = arg_value(args[0])
    choice = ""
    if "--choice" in args:
        idx = args.index("--choice")
        choice = " ".join(args[idx + 1:]).strip('"')
    for session in state["worldops"].get("micro_quest_sessions", []):
        if session["event_id"] == event_id and session["status"] == "active":
            session["status"] = "completed"
            session["stage"] = "memory"
            session["choice"] = choice
            hook = {
                "hook_id": f"worldops_hook_{now_ms()}",
                "event_id": event_id,
                "quest_id": session["quest_id"],
                "episode_summary": f"用户和 Sora 完成了微任务：{choice}",
                "write_policy": "manual_commit_required",
                "created_at": now_ms(),
            }
            state["worldops"].setdefault("memory_hooks", []).append(hook)
            return dump({"session": session, "memory_hook": hook})
    raise CommandError(f"找不到 active micro quest: {event_id}")


def ignore_world_event(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("需要 event id。")
    event_id = arg_value(args[0])
    state["worldops"].setdefault("runtime_decisions", []).append({
        "event_id": event_id,
        "decision": "ignored_by_user",
        "effect": "decrease_similar_event_frequency",
        "at": now_ms(),
    })
    if state["worldops"].get("active_event_id") == event_id:
        state["worldops"]["active_event_id"] = None
    return f"{event_id} 已标记为 ignored，不写共同经历。"


def set_behavior(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/set-behavior <preset>")
    preset = arg_value(args[0])
    presets = state["behavior"]["presets"]
    if preset not in presets:
        raise CommandError(f"未知 behavior preset: {preset}")
    previous = state["behavior"].get("current", {})
    changed = previous.get("preset") != preset
    last_changed_at = now_ms() if changed else previous.get("last_changed_at", now_ms())
    state["behavior"]["current"] = {
        "preset": preset,
        **presets[preset],
        "changed": changed,
        "updated_at": now_ms(),
        "last_changed_at": last_changed_at,
    }
    marker = "切换" if changed else "沿用"
    return f"behavior -> {preset} ({marker})"


def set_idle_threshold(args: list[str], state: JsonDict) -> str:
    if not args:
        raise CommandError("用法：/set-idle-threshold <分钟>")
    try:
        minutes = float(arg_value(args[0]))
    except ValueError:
        raise CommandError("空闲阈值必须是分钟数，例如 3。") from None
    if minutes <= 0:
        raise CommandError("空闲阈值必须大于 0。")
    seconds = max(1, int(minutes * 60))
    initiative = state.setdefault("initiative", {})
    initiative["idle_threshold_sec"] = seconds
    initiative["scheduler_enabled"] = True
    return f"主动搭话空闲阈值已设置为 {seconds} 秒（{minutes:g} 分钟）。"


def export_state(args: list[str], state: JsonDict) -> str:
    target = Path(args[0]) if args else Path("sora-sandbox-state-export.json")
    target.write_text(dump(state), encoding="utf-8")
    return f"已导出：{target.resolve()}"
