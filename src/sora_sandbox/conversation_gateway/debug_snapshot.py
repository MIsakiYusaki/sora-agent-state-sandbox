from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

from ..desktop_contracts import (
    DesktopDebugBridge,
    DesktopDebugSnapshot,
    DesktopDebugSubsystem,
)
from .security import VerifiedIdentity
from .service import ConversationGatewayService


def build_desktop_debug_snapshot(
    *,
    stack: Any,
    service: ConversationGatewayService,
    identity: VerifiedIdentity,
    conversation_id: str,
) -> DesktopDebugSnapshot:
    """Build a redacted L0 runtime snapshot for the trusted desktop host."""

    readiness = service.readiness()
    memory_health = _health(stack.memory)
    persona_health = _health(stack.persona)
    affect_health = _health(stack.affect)
    relationship_health = _health(stack.relationship)
    worldops_health = _health(stack.worldops)
    provider_health = (
        _health(stack.model_provider_system)
        if stack.model_provider_system is not None
        else {"status": "unavailable"}
    )
    interruptibility_health = _binding_health(
        stack,
        "interruptibility",
        required_methods=("evaluate", "status", "set_control", "revoke"),
    )
    quality = _quality(stack.memory)
    timeline = _timeline(service, identity, conversation_id, memory=stack.memory)
    routes = _routes(stack.model_provider_system)

    systems = [
        _subsystem(
            "conversation_runtime",
            "文字会话运行时",
            dict(readiness.get("runtime") or {}),
            "真实 turn、流式输出、取消和提交链路",
        ),
        _subsystem(
            "interruptibility",
            "对话边界与可打断性",
            interruptibility_health,
            "自然语言控制和正交权限框架已在运行",
            details={
                "binding": interruptibility_health.get("binding"),
                "missing_methods": interruptibility_health.get("missing_methods"),
            },
        ),
        _subsystem(
            "memory",
            "记忆系统 01-10",
            memory_health,
            "抽取、Gate、主存储、召回、生命周期与质量层已组装",
            details=_memory_summary(memory_health),
        ),
        _subsystem(
            "persona",
            "人格系统",
            persona_health,
            "有界、可回滚的人格轨迹服务已组装",
            details=_worker_summary(persona_health),
        ),
        _subsystem(
            "affect",
            "情绪与内部状态",
            affect_health,
            "短期状态、衰减与调节服务已组装",
            details=_worker_summary(affect_health),
        ),
        _subsystem(
            "relationship",
            "关系连续性",
            relationship_health,
            "按用户隔离的关系贡献与修复服务已组装",
            details=_relationship_summary(relationship_health),
        ),
        _subsystem(
            "worldops",
            "叙事世界",
            worldops_health,
            "签名内容、事件任务和个人时间线服务已组装",
            details=_worldops_summary(worldops_health),
        ),
        _subsystem(
            "model_provider",
            "模型服务与路由",
            provider_health,
            "Provider、凭据引用、能力验证和 workload 路由已组装",
            details=_provider_summary(provider_health),
        ),
        _subsystem(
            "module_10",
            "评测、观测与管理",
            {"status": quality.get("status", "unknown")},
            "评测注册表、发布门禁和受控管理动作已组装",
            details={
                "eval_suite_count": quality.get("eval_suite_count"),
                "admin_action_count": quality.get("admin_action_count"),
            },
        ),
    ]

    return DesktopDebugSnapshot(
        collected_at_ms=int(time.time() * 1000),
        profile_id_hash=hashlib.sha256(identity.profile_id.encode("utf-8")).hexdigest()[:16],
        conversation_id=conversation_id,
        gateway={
            "status": readiness.get("status", "unknown"),
            "protocol_version": readiness.get("protocol_version"),
            "draining": _optional_bool(readiness, "draining"),
            "active_turns": _optional_int(readiness, "active_turns"),
            "runtime_status": _status(dict(readiness.get("runtime") or {})),
            "registry_status": _status(dict(readiness.get("registry") or {})),
        },
        timeline=timeline,
        routes=routes,
        systems=systems,
        memory=_memory_summary(memory_health),
        async_work=_async_summary(
            memory_health=memory_health,
            persona_health=persona_health,
            affect_health=affect_health,
            relationship_health=relationship_health,
            worldops_health=worldops_health,
        ),
        quality=quality,
        bridges=[
            DesktopDebugBridge(
                bridge_id="gateway_events",
                label="当前会话事件",
                status="available",
                summary="身份隔离的 Gateway 事件接口已接入；实际收到的节点在时间线中单独展示",
            ),
            DesktopDebugBridge(
                bridge_id="turn_diagnostics",
                label="逐轮全链路诊断",
                status="available" if timeline["status"] in {"available", "empty"} else "partial",
                summary=(
                    "从加密运行时日志投影逐轮耗时、事件、模型调用和帧契约元数据"
                    if timeline["status"] == "available"
                    else "当前会话尚无可诊断轮次"
                    if timeline["status"] == "empty"
                    else "逐轮诊断未接入或读取失败；不能判定本轮耗时与调用记录"
                ),
            ),
            DesktopDebugBridge(
                bridge_id="debug_snapshot",
                label="脱敏运行快照",
                status="available",
                summary="Gateway 通过 debug.read 权限提供按配置隔离的 L0 快照",
            ),
            DesktopDebugBridge(
                bridge_id="user_controls",
                label="用户控制与数据权利",
                status="available",
                summary="模型服务、记忆可见性、忘记和对话控制已接真实接口",
            ),
            DesktopDebugBridge(
                bridge_id="typed_frames",
                label="逐轮类型化上下文帧",
                status="available" if callable(getattr(service.runtime, "diagnostic_frame", None)) else "partial",
                summary="按需读取逐轮白名单字段值、band 与来源；生成帧和历史交付快照分开，原文与隐藏推理不透出",
            ),
            DesktopDebugBridge(
                bridge_id="typed_commands",
                label="受控命令执行",
                status="partial",
                summary=(
                    "维护动作已接入短期会话、类型化预览、确认和审计；高风险数据动作仍保持关闭"
                    if getattr(getattr(stack.memory, "runtime", stack.memory), "quality_ops", None) is not None
                    else "真实命令注册表已投影；受控执行器尚未组装"
                ),
            ),
        ],
    )


def _attach_memory_jobs(timeline: dict[str, Any], memory: Any, conversation_id: str) -> None:
    """Only augment the already authenticated, current-conversation turn list."""
    turns = timeline.get("turns") or []
    if not turns:
        return
    try:
        from ..memory_job_observability import memory_job_observer
        observation = memory_job_observer(memory.runtime).snapshot(
            conversation_id, [turn["turn_id"] for turn in turns],
        )
        for turn in turns:
            turn["memory_jobs"] = {**observation, "jobs": [job for job in observation["jobs"]
                                  if job["turn_id"] == turn["turn_id"]
                                  and job["correlation_id"] == turn["correlation_id"]]}
    except Exception:
        for turn in turns:
            turn["memory_jobs"] = {"status": "unavailable", "jobs": []}


def _health(component: Any) -> dict[str, Any]:
    try:
        value = component.health()
    except Exception:
        return {"status": "degraded"}
    return dict(value) if isinstance(value, Mapping) else {"status": "unknown"}


def _binding_health(
    stack: Any,
    attribute: str,
    *,
    required_methods: tuple[str, ...],
) -> dict[str, Any]:
    component = getattr(stack, attribute, None)
    if component is None:
        return {
            "status": "unavailable",
            "binding": "missing",
            "missing_methods": list(required_methods),
        }
    missing = [name for name in required_methods if not callable(getattr(component, name, None))]
    return {
        "status": "active" if not missing else "degraded",
        "binding": "configured",
        "missing_methods": missing,
    }


def _quality(memory: Any) -> dict[str, Any]:
    try:
        dashboard = memory.quality_dashboard()
    except Exception:
        dashboard = None
    try:
        suites = memory.list_quality_eval_suites()
    except Exception:
        suites = None
    try:
        actions = memory.list_admin_action_specs()
    except Exception:
        actions = None
    dashboard_record = dict(dashboard) if isinstance(dashboard, Mapping) else None
    suite_records = _observed_records(suites)
    action_records = _observed_records(actions)
    latest_runs = _observed_dashboard_records(dashboard_record, "latest_quality_runs")
    baselines = _observed_dashboard_records(dashboard_record, "baselines")
    open_events = _observed_dashboard_records(dashboard_record, "open_events")
    return {
        "status": (
            str(dashboard_record.get("status") or "unknown")
            if dashboard_record is not None
            else "degraded"
        ),
        "eval_suite_count": len(suite_records) if suite_records is not None else None,
        "quality_run_count": len(latest_runs) if latest_runs is not None else None,
        "baseline_count": len(baselines) if baselines is not None else None,
        "open_event_count": len(open_events) if open_events is not None else None,
        "admin_action_count": len(action_records) if action_records is not None else None,
        "eval_suites": [
            {
                "name": item.get("name"),
                "source_module": item.get("source_module"),
                "default_severity": item.get("default_severity"),
            }
            for item in suite_records
        ] if suite_records is not None else None,
        "latest_runs": [
            _pick(item, "id", "status", "run_type", "trigger", "created_at")
            for item in latest_runs
        ] if latest_runs is not None else None,
        "baselines": [
            _pick(item, "id", "name", "status", "policy_profile", "created_at")
            for item in baselines
        ] if baselines is not None else None,
        "open_events": [
            _pick(item, "id", "event_type", "severity", "status", "created_at")
            for item in open_events
        ] if open_events is not None else None,
        "admin_actions": [
            _pick(item, "action_name", "risk_level", "description")
            for item in action_records
        ] if action_records is not None else None,
    }


def _timeline(
    service: ConversationGatewayService,
    identity: VerifiedIdentity,
    conversation_id: str,
    *,
    memory: Any,
) -> dict[str, Any]:
    binding = service.registry.conversation_for_identity(
        profile_id=identity.profile_id,
        subject_id=identity.subject_id,
        agent_id=identity.agent_id,
        external_conversation_id=conversation_id,
    )
    if binding is None:
        return {"status": "empty", "turns": [], "retention_limit": 20}
    diagnostic_turns = getattr(service.runtime, "diagnostic_turns", None)
    if not callable(diagnostic_turns):
        return {"status": "unavailable", "turns": [], "retention_limit": 20}
    try:
        turns = list(diagnostic_turns(binding.runtime_conversation_id, limit=20))
    except Exception:
        return {"status": "degraded", "turns": [], "retention_limit": 20}
    timeline = {"status": "available", "turns": turns, "retention_limit": 20}
    # Desktop IDs are external aliases. Committed-turn receipts use the
    # canonical Runtime ID from this same identity-scoped binding. Never query
    # Memory by the caller-supplied alias or remove its ownership filters.
    _attach_memory_jobs(timeline, memory, binding.runtime_conversation_id)
    return timeline


def _routes(provider_system: Any) -> dict[str, Any]:
    if provider_system is None:
        return {
            "status": "unavailable",
            "registry_revision": None,
            "binding_count": None,
            "bindings": None,
        }
    try:
        exported = dict(provider_system.export_redacted())
    except Exception:
        return {
            "status": "degraded",
            "registry_revision": None,
            "binding_count": None,
            "bindings": None,
        }
    exported_bindings = _observed_records(exported.get("bindings"))
    if exported_bindings is None:
        return {
            "status": "degraded",
            "registry_revision": exported.get("registry_revision"),
            "binding_count": None,
            "bindings": None,
        }
    bindings: list[dict[str, Any]] = []
    for item in exported_bindings:
        primary = _mapping(item.get("primary"))
        fallbacks = _observed_records(item.get("fallbacks"))
        bindings.append(
            {
                "workload": item.get("workload"),
                "model": primary.get("model_id"),
                "fallback_models": (
                    [target.get("model_id") for target in fallbacks]
                    if fallbacks is not None
                    else None
                ),
                "requirements": _optional_list(item, "requirements"),
                "cross_provider_fallback": _optional_bool(
                    item, "cross_provider_fallback"
                ),
                "revision": item.get("revision"),
            }
        )
    return {
        "status": "available",
        "registry_revision": exported.get("registry_revision"),
        "binding_count": len(bindings),
        "bindings": bindings,
    }


def _pick(item: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item.get(key) for key in keys}


def _subsystem(
    subsystem_id: str,
    label: str,
    health: Mapping[str, Any],
    summary: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> DesktopDebugSubsystem:
    raw = _status(health)
    if raw in {"ok", "ready", "healthy", "active", "passed"}:
        status = "active"
    elif raw in {"error", "failed", "unavailable", "stopped"}:
        status = "unavailable"
    else:
        status = "degraded"
    if status == "active":
        visible_summary = summary
    elif status == "unavailable":
        visible_summary = "健康快照报告该系统当前不可用；以下只显示已观测字段"
    else:
        visible_summary = "健康证据不完整或系统处于降级状态；以下只显示已观测字段"
    return DesktopDebugSubsystem(
        subsystem_id=subsystem_id,
        label=label,
        status=status,
        summary=visible_summary,
        details=dict(details or {}),
    )


def _status(value: Mapping[str, Any]) -> str:
    return str(value.get("status") or "unknown").strip().lower()


def _memory_summary(health: Mapping[str, Any]) -> dict[str, Any]:
    canonical = dict(health.get("canonical_store") or {})
    index = dict(canonical.get("index_outbox") or {})
    projectors = dict(canonical.get("projectors") or {})
    return {
        "status": _status(health),
        "active_memories": _optional_int(health, "active_memories"),
        "pending_candidates": _optional_int(health, "pending_candidates"),
        "open_reviews": _optional_int(health, "open_reviews"),
        "active_quarantines": _optional_int(health, "active_quarantines"),
        "pending_extraction_jobs": _optional_int(health, "pending_extraction_jobs"),
        "failed_extraction_jobs": _optional_int(health, "failed_extraction_jobs"),
        "index_pending": _optional_int(index, "pending"),
        "index_failed": _optional_int(index, "failed"),
        "index_dead_letter": _optional_int(index, "dead_letter"),
        "projector_pending": _optional_int(projectors, "pending"),
        "projector_failed": _optional_int(projectors, "failed"),
        "projector_dead_letter": _optional_int(projectors, "dead_letter"),
    }


def _worker_summary(health: Mapping[str, Any]) -> dict[str, Any]:
    worker = dict(health.get("worker") or {})
    outbox = dict(health.get("outbox") or {})
    return {
        "status": _status(health),
        "rollout_mode": health.get("rollout_mode"),
        "worker_enabled": worker.get("enabled"),
        "worker_alive": worker.get("alive"),
        "worker_error_present": bool(worker.get("last_error")) if "last_error" in worker else None,
        "queue_depth": _optional_int(_mapping(health.get("write_coordinator")), "queue_depth"),
        "pending": _first_optional_int(worker, "pending", "queued"),
        "quarantined": _optional_int(worker, "quarantined"),
        "dead_letter": _optional_int(worker, "dead_letter"),
        "outbox_pending": _optional_int(outbox, "pending"),
        "outbox_failed": _optional_int(outbox, "failed"),
        "outbox_dead_letter": _optional_int(outbox, "dead_letter"),
    }


def _relationship_summary(health: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": _status(health),
        "rollout_mode": health.get("rollout_mode"),
        "worker_alive": health.get("worker_alive"),
        "pending": _optional_int(health, "pending_inbox"),
        "dead_letter": _optional_int(health, "dead_letters"),
        "write_queue_depth": _optional_int(health, "write_queue_depth"),
    }


def _worldops_summary(health: Mapping[str, Any]) -> dict[str, Any]:
    worker = dict(health.get("worker") or {})
    coordinator = dict(health.get("write_coordinator") or {})
    outbox = dict(health.get("outbox") or {})
    return {
        "status": _status(health),
        "deployment": health.get("deployment"),
        "worker_alive": worker.get("running"),
        "write_queue_depth": _optional_int(coordinator, "queue_depth"),
        "under_backpressure": _optional_bool(coordinator, "under_backpressure"),
        "outbox_pending": _optional_int(outbox, "pending"),
        "outbox_dead_letter": _optional_int(outbox, "dead_letter"),
        "incomplete_consumer_effects": _optional_int(
            outbox, "incomplete_consumer_effects"
        ),
        "open_sync_conflicts": _optional_int(health, "open_sync_conflicts"),
    }


def _provider_summary(health: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": _status(health),
        "conversation_ready": _optional_bool(health, "conversation_ready"),
        "binding_count": _optional_int(health, "binding_count"),
        "missing_conversation_workloads": _optional_list(
            health, "missing_conversation_workloads"
        ),
        "active_credential_failure_count": _optional_list_count(
            health, "active_credential_failures"
        ),
    }


def _async_summary(
    *,
    memory_health: Mapping[str, Any],
    affect_health: Mapping[str, Any],
    relationship_health: Mapping[str, Any],
    worldops_health: Mapping[str, Any],
    persona_health: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    memory = _memory_summary(memory_health)
    affect = _worker_summary(affect_health)
    relationship = _relationship_summary(relationship_health)
    worldops = _worldops_summary(worldops_health)
    persona = _worker_summary(persona_health or {})
    return {
        "pending": _sum_if_observed(
            memory.get("pending_extraction_jobs"),
            memory.get("index_pending"),
            memory.get("projector_pending"),
            persona.get("pending"),
            affect.get("pending"),
            affect.get("outbox_pending"),
            relationship.get("pending"),
            worldops.get("write_queue_depth"),
            worldops.get("outbox_pending"),
        ),
        "failed": _sum_if_observed(
            memory.get("index_failed"),
            memory.get("projector_failed"),
            memory.get("failed_extraction_jobs"),
            affect.get("outbox_failed"),
        ),
        "dead_letter": _sum_if_observed(
            memory.get("index_dead_letter"),
            memory.get("projector_dead_letter"),
            affect.get("dead_letter"),
            affect.get("outbox_dead_letter"),
            relationship.get("dead_letter"),
            worldops.get("outbox_dead_letter"),
            worldops.get("incomplete_consumer_effects"),
        ),
        "memory": memory,
        "persona": persona,
        "affect": affect,
        "relationship": relationship,
        "worldops": worldops,
        "failure_coverage_complete": False,
        "failure_coverage": ["memory.extraction_jobs", "memory.index_outbox", "memory.projector_outbox", "affect.outbox"],
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(record: Mapping[str, Any], key: str) -> int | None:
    if key not in record:
        return None
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _first_optional_int(record: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _optional_int(record, key)
        if value is not None:
            return value
    return None


def _optional_bool(record: Mapping[str, Any], key: str) -> bool | None:
    value = record.get(key)
    return value if isinstance(value, bool) else None


def _optional_list(record: Mapping[str, Any], key: str) -> list[Any] | None:
    value = record.get(key)
    return list(value) if isinstance(value, (list, tuple)) else None


def _optional_list_count(record: Mapping[str, Any], key: str) -> int | None:
    value = _optional_list(record, key)
    return len(value) if value is not None else None


def _observed_records(value: Any) -> list[Mapping[str, Any]] | None:
    if not isinstance(value, (list, tuple)):
        return None
    if any(not isinstance(item, Mapping) for item in value):
        return None
    return list(value)


def _observed_dashboard_records(
    dashboard: Mapping[str, Any] | None,
    key: str,
) -> list[Mapping[str, Any]] | None:
    if dashboard is None or key not in dashboard:
        return None
    return _observed_records(dashboard.get(key))


def _sum_if_observed(*values: Any) -> int | None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    return sum(values)
