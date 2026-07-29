from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


RiskLevel = str
EvalRunner = Callable[[], JsonDict]
AdminHandler = Callable[[JsonDict], JsonDict]

_COUNTABLE_TABLES = {
    "recall_trace",
    "recall_plan_trace",
    "memory_surface_policy_trace",
    "memory_projection_audit",
    "memory_quality_metric_point",
    "memory_quality_event",
}


def _loads(value: str | None, default: Any) -> Any:
    return json_loads(value, default)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)
    except ValueError:
        return False


@dataclass(frozen=True)
class EvalSuiteAdapter:
    name: str
    runner: EvalRunner
    source_module: str
    default_severity: str = "error"


@dataclass(frozen=True)
class AdminActionSpec:
    name: str
    risk_level: RiskLevel
    preview: AdminHandler
    execute: AdminHandler
    rollback_plan: AdminHandler | None = None
    description: str = ""


class MemoryQualityOpsService:
    """Quality, observability, and admin plane for the memory runtime."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        store_health: Callable[[], JsonDict],
        module_versions: Callable[[], JsonDict] | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._store_health = store_health
        self._module_versions = module_versions or (lambda: {"memory_quality_ops": "v1"})
        self._eval_suites: dict[str, EvalSuiteAdapter] = {}
        self._admin_actions: dict[str, AdminActionSpec] = {}
        self._export_sections: dict[str, Callable[[], JsonDict]] = {}
        self._register_builtin_actions()

    def register_eval_suite(
        self,
        name: str,
        runner: EvalRunner,
        *,
        source_module: str,
        default_severity: str = "error",
    ) -> None:
        self._eval_suites[name] = EvalSuiteAdapter(
            name=name,
            runner=runner,
            source_module=source_module,
            default_severity=default_severity,
        )

    def list_eval_suites(self) -> list[JsonDict]:
        return [
            {
                "name": suite.name,
                "source_module": suite.source_module,
                "default_severity": suite.default_severity,
            }
            for suite in sorted(self._eval_suites.values(), key=lambda item: item.name)
        ]

    def register_export_section(self, name: str, provider: Callable[[], JsonDict]) -> None:
        self._export_sections[name] = provider

    def run_quality_eval(
        self,
        *,
        suites: list[str] | None = None,
        run_type: str = "module_eval",
        trigger: str = "manual",
        policy_profile: str = "core",
        dataset_version: str = "default",
        baseline_id: str | None = None,
        scope: JsonDict | None = None,
    ) -> JsonDict:
        selected = self._select_suites(suites)
        run_id = new_id("qrun")
        created_at = utc_now()
        scope_payload = {
            "requested_suites": suites or "all",
            "selected_suites": [suite.name for suite in selected],
            **(scope or {}),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_quality_run (
                    id, run_type, status, trigger, policy_profile, dataset_version,
                    baseline_id, scope_json, module_versions_json, summary_json,
                    blockers_json, created_at, completed_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, '{}', '[]', ?, NULL)
                """,
                (
                    run_id,
                    run_type,
                    trigger,
                    policy_profile,
                    dataset_version,
                    baseline_id,
                    json_dumps(scope_payload),
                    json_dumps(self._module_versions()),
                    created_at,
                ),
            )
            self._conn.commit()

        suite_summaries: list[JsonDict] = []
        blockers: list[str] = []
        case_count = 0
        passed_count = 0
        failed_count = 0
        for suite in selected:
            try:
                source_result = suite.runner()
                mapped = self._store_suite_result(
                    run_id,
                    suite,
                    source_result,
                    policy_profile=policy_profile,
                )
            except Exception as exc:  # pragma: no cover - defensive safety net
                mapped = self._store_suite_exception(run_id, suite, exc)
            suite_summaries.append(mapped)
            case_count += int(mapped["total_cases"])
            passed_count += int(mapped["passed_cases"])
            failed_count += int(mapped["failed_cases"])
            if mapped["status"] == "failed":
                blockers.append(f"eval_failed:{suite.name}")

        pass_rate = round(passed_count / case_count, 6) if case_count else 1.0
        status = "failed" if blockers else "passed"
        summary = {
            "suite_count": len(selected),
            "total_cases": case_count,
            "passed_cases": passed_count,
            "failed_cases": failed_count,
            "pass_rate": pass_rate,
            "suites": suite_summaries,
        }
        if baseline_id:
            summary["baseline_diff"] = self.compare_baseline(
                run_id,
                baseline_id=baseline_id,
                persist_event=False,
            )
            if summary["baseline_diff"].get("status") == "failed":
                blockers.append("baseline_regression")
                status = "failed"
        with self._lock:
            self._conn.execute(
                """
                UPDATE memory_quality_run
                SET status = ?, summary_json = ?, blockers_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, json_dumps(summary), json_dumps(blockers), utc_now(), run_id),
            )
            self._conn.commit()
        return self.get_quality_run(run_id)

    def list_quality_runs(self, *, limit: int = 20, run_type: str | None = None) -> list[JsonDict]:
        with self._lock:
            if run_type:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_quality_run
                    WHERE run_type = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (run_type, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_quality_run
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._run_from_row(row, include_cases=False) for row in rows]

    def get_quality_run(self, run_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_quality_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"quality run not found: {run_id}")
            case_rows = self._conn.execute(
                """
                SELECT * FROM memory_quality_case_result
                WHERE run_id = ?
                ORDER BY suite_name, created_at, case_name
                """,
                (run_id,),
            ).fetchall()
        run = self._run_from_row(row, include_cases=False)
        run["cases"] = [self._case_from_row(case_row) for case_row in case_rows]
        return run

    def run_scenario_replay(
        self,
        *,
        package_name: str = "default_memory_scenario_replay",
        suites: list[str] | None = None,
        policy_profile: str = "core",
        dataset_version: str = "default",
    ) -> JsonDict:
        return self.run_quality_eval(
            suites=suites,
            run_type="scenario_replay",
            trigger="manual_replay",
            policy_profile=policy_profile,
            dataset_version=dataset_version,
            scope={"replay_package": package_name, "replay": True},
        )

    def sync_trace_index(self, *, limit: int = 500, replay: bool = False) -> JsonDict:
        synced: dict[str, int] = {
            "recall_trace": 0,
            "recall_plan_trace": 0,
            "memory_surface_policy_trace": 0,
            "memory_projection_audit": 0,
        }
        with self._lock:
            for row in self._conn.execute(
                "SELECT * FROM recall_trace ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall():
                self._upsert_trace_index(
                    trace_id=row["id"],
                    correlation_id=f"recall:{row['id']}",
                    module="memory_recall",
                    entity_type="recall_trace",
                    entity_id=row["id"],
                    source_table="recall_trace",
                    source_id=row["id"],
                    causality_refs={"recall_trace_id": row["id"]},
                    redaction_level="gist_only",
                    policy_profile=None,
                    provider_versions={"backend": row["backend"]},
                    cost_latency_tokens={
                        "latency_ms": row["latency_ms"],
                        "token_budget": row["token_budget"],
                        "token_used": row["token_used"],
                        "cost": _loads(row["cost_json"], {}),
                    },
                    replay=replay,
                    created_at=row["created_at"],
                )
                synced["recall_trace"] += 1
            for row in self._conn.execute(
                "SELECT * FROM recall_plan_trace ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall():
                correlation_id = (
                    f"recall:{row['recall_trace_id']}"
                    if row["recall_trace_id"]
                    else self._turn_correlation(row["session_id"], row["turn_id"], row["plan_id"])
                )
                self._upsert_trace_index(
                    trace_id=row["plan_id"],
                    correlation_id=correlation_id,
                    session_id=row["session_id"],
                    turn_id=row["turn_id"],
                    module="recall_planner",
                    entity_type="recall_plan",
                    entity_id=row["plan_id"],
                    source_table="recall_plan_trace",
                    source_id=row["plan_id"],
                    causality_refs={
                        "plan_id": row["plan_id"],
                        "recall_trace_id": row["recall_trace_id"],
                    },
                    redaction_level="gist_only",
                    policy_profile=None,
                    provider_versions={"planner_version": row["planner_version"]},
                    cost_latency_tokens={"llm_usage": _loads(row["llm_usage_json"], {})},
                    replay=replay,
                    created_at=row["created_at"],
                )
                synced["recall_plan_trace"] += 1
            for row in self._conn.execute(
                "SELECT * FROM memory_surface_policy_trace ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall():
                correlation_id = (
                    f"recall:{row['recall_trace_id']}"
                    if row["recall_trace_id"]
                    else self._turn_correlation(row["session_id"], row["turn_id"], row["id"])
                )
                self._upsert_trace_index(
                    trace_id=row["id"],
                    correlation_id=correlation_id,
                    session_id=row["session_id"],
                    turn_id=row["turn_id"],
                    module="surface_policy",
                    entity_type="surface_trace",
                    entity_id=row["id"],
                    source_table="memory_surface_policy_trace",
                    source_id=row["id"],
                    causality_refs={
                        "surface_trace_id": row["id"],
                        "recall_trace_id": row["recall_trace_id"],
                    },
                    redaction_level="gist_only",
                    policy_profile=row["policy_profile"],
                    provider_versions={
                        "policy_version": row["policy_version"],
                        "provider": row["provider"],
                    },
                    cost_latency_tokens={
                        "token_budget": row["token_budget"],
                        "token_used": row["token_used"],
                        "provider_usage": _loads(row["provider_usage_json"], []),
                    },
                    replay=replay,
                    created_at=row["created_at"],
                )
                synced["memory_surface_policy_trace"] += 1
            for row in self._conn.execute(
                "SELECT * FROM memory_projection_audit ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall():
                correlation_id = (
                    f"memory:{row['memory_id']}"
                    if row["memory_id"]
                    else f"projection:{row['id']}"
                )
                self._upsert_trace_index(
                    trace_id=row["id"],
                    correlation_id=correlation_id,
                    module="projection_gateway",
                    entity_type="projection_audit",
                    entity_id=row["id"],
                    source_table="memory_projection_audit",
                    source_id=row["id"],
                    causality_refs={
                        "audit_id": row["id"],
                        "event_id": row["event_id"],
                        "memory_id": row["memory_id"],
                        "subsystem": row["subsystem"],
                    },
                    redaction_level="id_only",
                    policy_profile=None,
                    provider_versions={"subsystem": row["subsystem"], "action": row["action"]},
                    cost_latency_tokens={},
                    replay=replay,
                    created_at=row["created_at"],
                )
                synced["memory_projection_audit"] += 1
            self._conn.commit()
        total = sum(synced.values())
        self.record_metric_point(
            "memory.trace_index.synced",
            total,
            unit="trace",
            dimensions={"replay": replay},
            source_ref="sync_trace_index",
        )
        return {"status": "ok", "synced": synced, "total_synced": total}

    def trace_index_coverage(self) -> JsonDict:
        with self._lock:
            raw_counts = {
                "recall_trace": self._count_table("recall_trace"),
                "recall_plan_trace": self._count_table("recall_plan_trace"),
                "memory_surface_policy_trace": self._count_table("memory_surface_policy_trace"),
                "memory_projection_audit": self._count_table("memory_projection_audit"),
            }
            indexed_rows = self._conn.execute(
                """
                SELECT source_table, COUNT(*) AS count
                FROM memory_trace_index
                GROUP BY source_table
                """
            ).fetchall()
        indexed = {row["source_table"]: int(row["count"]) for row in indexed_rows}
        total_raw = sum(raw_counts.values())
        total_indexed = sum(min(indexed.get(table, 0), count) for table, count in raw_counts.items())
        coverage = round(total_indexed / total_raw, 6) if total_raw else 1.0
        return {
            "status": "ok" if coverage >= 0.99 else "warn",
            "coverage": coverage,
            "raw_counts": raw_counts,
            "indexed_counts": indexed,
        }

    def record_metric_point(
        self,
        metric_name: str,
        value: float,
        *,
        unit: str = "count",
        dimensions: JsonDict | None = None,
        source_ref: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> JsonDict:
        metric_id = new_id("qmetric")
        created_at = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_quality_metric_point (
                    id, metric_name, value, unit, dimensions_json, source_ref,
                    window_start, window_end, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric_id,
                    metric_name,
                    float(value),
                    unit,
                    json_dumps(dimensions or {}),
                    source_ref,
                    window_start,
                    window_end,
                    created_at,
                ),
            )
            self._conn.commit()
        return {
            "id": metric_id,
            "metric_name": metric_name,
            "value": float(value),
            "unit": unit,
            "dimensions": dimensions or {},
            "source_ref": source_ref,
            "created_at": created_at,
        }

    def collect_metrics(self, *, source_ref: str = "quality_ops") -> JsonDict:
        health = self._store_health()
        coverage = self.trace_index_coverage()
        latest_run = self.list_quality_runs(limit=1)
        metrics = [
            self.record_metric_point(
                "memory.health.active_memories",
                float(health.get("active_memories", 0)),
                unit="memory",
                source_ref=source_ref,
            ),
            self.record_metric_point(
                "memory.health.blockers",
                float(len(health.get("blockers", []))),
                unit="blocker",
                source_ref=source_ref,
            ),
            self.record_metric_point(
                "memory.trace_index.coverage",
                float(coverage["coverage"]),
                unit="ratio",
                source_ref=source_ref,
            ),
            self.record_metric_point(
                "memory.index_outbox.pending",
                float(health.get("index_outbox", {}).get("pending", 0)),
                unit="event",
                source_ref=source_ref,
            ),
            self.record_metric_point(
                "memory.projector.pending",
                float(health.get("projectors", {}).get("pending", 0)),
                unit="event",
                source_ref=source_ref,
            ),
        ]
        if latest_run:
            metrics.append(
                self.record_metric_point(
                    "memory.quality.latest_pass_rate",
                    float(latest_run[0].get("summary", {}).get("pass_rate", 1.0)),
                    unit="ratio",
                    dimensions={"run_id": latest_run[0]["id"]},
                    source_ref=source_ref,
                )
            )
        slos = self.evaluate_slos(health=health, coverage=coverage)
        if slos["status"] == "failed":
            self.record_quality_event(
                event_type="slo_violation",
                severity="blocker",
                message="Memory quality SLO failed",
                source_type="quality_metrics",
                source_id=source_ref,
                payload=slos,
            )
        elif slos["status"] == "warning":
            self.record_quality_event(
                event_type="slo_warning",
                severity="warning",
                status="resolved",
                message="Memory quality SLO warning",
                source_type="quality_metrics",
                source_id=source_ref,
                payload=slos,
            )
        return {"status": "ok", "metrics": metrics, "slos": slos}

    def evaluate_slos(
        self,
        *,
        health: JsonDict | None = None,
        coverage: JsonDict | None = None,
    ) -> JsonDict:
        health = health or self._store_health()
        coverage = coverage or self.trace_index_coverage()
        checks = [
            {
                "name": "privacy_incidents_zero",
                "status": "passed" if self._open_event_count("privacy_incident") == 0 else "failed",
                "severity": "blocker",
            },
            {
                "name": "trace_index_coverage",
                "status": "passed" if coverage["coverage"] >= 0.99 else "warning",
                "severity": "warning",
                "value": coverage["coverage"],
            },
            {
                "name": "store_health",
                "status": "passed" if health.get("status") in {"ok", "warn"} else "failed",
                "severity": "blocker",
                "value": health.get("status"),
            },
            {
                "name": "outbox_dead_letters_zero",
                "status": "passed"
                if not health.get("index_outbox", {}).get("dead_letter")
                and not health.get("projectors", {}).get("dead_letter")
                else "failed",
                "severity": "blocker",
            },
        ]
        status = "failed" if any(item["status"] == "failed" for item in checks) else (
            "warning" if any(item["status"] == "warning" for item in checks) else "passed"
        )
        return {"status": status, "checks": checks}

    def dashboard(self) -> JsonDict:
        health = self._store_health()
        coverage = self.trace_index_coverage()
        slos = self.evaluate_slos(health=health, coverage=coverage)
        return {
            "status": "error" if slos["status"] == "failed" else health.get("status", "ok"),
            "health": health,
            "trace_index": coverage,
            "slos": slos,
            "latest_quality_runs": self.list_quality_runs(limit=5),
            "baselines": self.list_baselines(limit=5),
            "open_events": self.list_quality_events(status="open", limit=20),
            "recent_admin_actions": self.list_admin_actions(limit=10),
        }

    def pin_baseline(
        self,
        run_id: str,
        *,
        name: str,
        policy_profile: str = "core",
        dataset_version: str = "default",
        status: str = "candidate",
        accepted_failures: list[JsonDict] | None = None,
        promote: bool = False,
    ) -> JsonDict:
        run = self.get_quality_run(run_id)
        baseline_id = new_id("qbase")
        created_at = utc_now()
        final_status = "active" if promote else status
        supersedes_id = None
        with self._lock:
            if promote:
                current = self._conn.execute(
                    """
                    SELECT id FROM memory_quality_baseline
                    WHERE policy_profile = ? AND dataset_version = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (policy_profile, dataset_version),
                ).fetchone()
                supersedes_id = current["id"] if current else None
                self._conn.execute(
                    """
                    UPDATE memory_quality_baseline
                    SET status = 'superseded'
                    WHERE policy_profile = ? AND dataset_version = ? AND status = 'active'
                    """,
                    (policy_profile, dataset_version),
                )
            self._conn.execute(
                """
                INSERT INTO memory_quality_baseline (
                    id, name, run_id, status, policy_profile, dataset_version,
                    snapshot_json, accepted_failures_json, supersedes_id,
                    created_at, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    baseline_id,
                    name,
                    run_id,
                    final_status,
                    policy_profile,
                    dataset_version,
                    json_dumps(self._baseline_snapshot(run)),
                    json_dumps(accepted_failures or []),
                    supersedes_id,
                    created_at,
                    created_at if promote else None,
                ),
            )
            self._conn.commit()
        return self.get_baseline(baseline_id)

    def list_baselines(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        policy_profile: str | None = None,
    ) -> list[JsonDict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if policy_profile:
            clauses.append("policy_profile = ?")
            params.append(policy_profile)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM memory_quality_baseline
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._baseline_from_row(row) for row in rows]

    def get_baseline(self, baseline_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_quality_baseline WHERE id = ?",
                (baseline_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"quality baseline not found: {baseline_id}")
        return self._baseline_from_row(row)

    def compare_baseline(
        self,
        run_id: str,
        *,
        baseline_id: str | None = None,
        persist_event: bool = True,
    ) -> JsonDict:
        run = self.get_quality_run(run_id)
        baseline = self.get_baseline(baseline_id) if baseline_id else self._active_baseline(run)
        if not baseline:
            return {"status": "skipped", "reason": "no_active_baseline"}
        current = self._baseline_snapshot(run)
        expected_cases = baseline["snapshot"].get("cases", {})
        actual_cases = current["cases"]
        current_suites = {key.split("::", 1)[0] for key in actual_cases}
        regressions: list[JsonDict] = []
        for key, expected in expected_cases.items():
            suite_name = key.split("::", 1)[0]
            if suite_name not in current_suites:
                continue
            actual = actual_cases.get(key)
            if not actual:
                regressions.append({"case": key, "reason": "missing_case"})
                continue
            if expected.get("status") == "passed" and actual.get("status") != "passed":
                regressions.append(
                    {
                        "case": key,
                        "reason": "passed_to_failed",
                        "expected": expected.get("status"),
                        "actual": actual.get("status"),
                    }
                )
        relevant_expected = [
            case for key, case in expected_cases.items()
            if key.split("::", 1)[0] in current_suites
        ]
        baseline_pass_rate = (
            sum(1 for case in relevant_expected if case.get("status") == "passed")
            / len(relevant_expected)
            if relevant_expected
            else float(baseline["snapshot"].get("summary", {}).get("pass_rate", 1.0))
        )
        current_pass_rate = float(current.get("summary", {}).get("pass_rate", 1.0))
        if current_pass_rate + 0.000001 < baseline_pass_rate:
            regressions.append(
                {
                    "case": "__summary__",
                    "reason": "pass_rate_regression",
                    "expected": baseline_pass_rate,
                    "actual": current_pass_rate,
                }
            )
        accepted = {
            str(item.get("case"))
            for item in baseline.get("accepted_failures", [])
            if not item.get("expires_at") or not _is_expired(str(item.get("expires_at")))
        }
        unaccepted = [item for item in regressions if str(item.get("case")) not in accepted]
        result = {
            "status": "failed" if unaccepted else "passed",
            "baseline_id": baseline["id"],
            "run_id": run_id,
            "regressions": regressions,
            "unaccepted_regressions": unaccepted,
            "baseline_pass_rate": baseline_pass_rate,
            "current_pass_rate": current_pass_rate,
        }
        if persist_event and unaccepted:
            self.record_quality_event(
                event_type="baseline_regression",
                severity="blocker",
                message=f"Quality run {run_id} regressed from baseline {baseline['id']}",
                source_type="quality_run",
                source_id=run_id,
                payload=result,
            )
        return result

    def run_release_gate(
        self,
        *,
        change_scope: list[str] | None = None,
        baseline_id: str | None = None,
        policy_profile: str = "core",
        dataset_version: str = "default",
    ) -> JsonDict:
        suites = self._suites_for_change_scope(change_scope or ["all"])
        self.sync_trace_index(limit=1000)
        eval_run = self.run_quality_eval(
            suites=suites,
            run_type="release_gate",
            trigger="release_gate",
            policy_profile=policy_profile,
            dataset_version=dataset_version,
            baseline_id=baseline_id,
            scope={"change_scope": change_scope or ["all"]},
        )
        metrics = self.collect_metrics(source_ref=f"release_gate:{eval_run['id']}")
        baseline_diff = self.compare_baseline(
            eval_run["id"],
            baseline_id=baseline_id,
            persist_event=False,
        )
        blockers = list(eval_run.get("blockers", []))
        if metrics["slos"]["status"] == "failed":
            blockers.append("slo_failed")
        if baseline_diff["status"] == "failed":
            blockers.append("baseline_regression")
        status = "blocked" if blockers else "passed"
        result = {
            "status": status,
            "run_id": eval_run["id"],
            "suites": suites,
            "change_scope": change_scope or ["all"],
            "blockers": sorted(set(blockers)),
            "slos": metrics["slos"],
            "baseline_diff": baseline_diff,
        }
        self.record_quality_event(
            event_type="release_gate",
            severity="blocker" if status == "blocked" else "info",
            status="open" if status == "blocked" else "resolved",
            message=f"Release gate {status}",
            source_type="quality_run",
            source_id=eval_run["id"],
            payload=result,
        )
        return result

    def register_admin_action(self, spec: AdminActionSpec) -> None:
        self._admin_actions[spec.name] = spec

    def list_admin_action_specs(self) -> list[JsonDict]:
        return [
            {
                "action_name": spec.name,
                "risk_level": spec.risk_level,
                "description": spec.description,
            }
            for spec in sorted(self._admin_actions.values(), key=lambda item: item.name)
        ]

    def preview_admin_action(
        self,
        action_name: str,
        params: JsonDict | None = None,
        *,
        actor: str = "local_user",
        role: str = "admin",
        reason: str = "manual_admin_action",
        ttl_seconds: int = 600,
    ) -> JsonDict:
        spec = self._require_action(action_name)
        if not self._role_allows(role, spec.risk_level):
            self.record_quality_event(
                event_type="admin_permission_denied",
                severity="warning",
                status="resolved",
                source_type="admin_action",
                source_id=action_name,
                message=f"Admin role {role} cannot preview {spec.risk_level} action {action_name}",
                payload={"actor": actor, "role": role, "risk_level": spec.risk_level},
            )
            raise ValueError(f"role {role} cannot preview {spec.risk_level} action {action_name}")
        params = params or {}
        preview = spec.preview(params)
        rollback = spec.rollback_plan(params) if spec.rollback_plan else {}
        action_id = new_id("qadmin")
        token = new_id("confirm")
        requires_approval = spec.risk_level in {"high", "break_glass"}
        created_at = utc_now()
        expires_at = _iso_after(ttl_seconds)
        dry_run = self.global_dry_run_enabled()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_admin_action (
                    id, action_name, risk_level, actor, role, reason,
                    params_hash, params_json, preview_json, impact_summary_json,
                    rollback_plan_json, approval_status, confirmation_token_hash,
                    token_expires_at, execution_status, result_json,
                    audit_event_id, dry_run, created_at, executed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, ?, ?, NULL, ?)
                """,
                (
                    action_id,
                    action_name,
                    spec.risk_level,
                    actor,
                    role,
                    reason,
                    _hash_json(params),
                    json_dumps(params),
                    json_dumps(preview),
                    json_dumps(preview.get("impact_summary", preview)),
                    json_dumps(rollback),
                    "required" if requires_approval else "not_required",
                    _hash_text(token) if requires_approval else None,
                    expires_at if requires_approval else None,
                    "previewed",
                    int(dry_run),
                    created_at,
                    expires_at,
                ),
            )
            self._conn.commit()
        result = self.get_admin_action(action_id)
        if requires_approval:
            result["confirmation_token"] = token
        return result

    def execute_admin_action(
        self,
        action_id: str,
        *,
        confirmation_token: str | None = None,
        actor: str = "local_user",
    ) -> JsonDict:
        action = self._get_admin_action_private(action_id)
        spec = self._require_action(action["action_name"])
        if action["execution_status"] != "previewed":
            return self.get_admin_action(action_id)
        if action.get("expires_at") and _is_expired(action["expires_at"]):
            self._mark_admin_action(action_id, "expired", {"error": "preview_expired"})
            return self.get_admin_action(action_id)
        if self.global_dry_run_enabled() and spec.risk_level in {"medium", "high", "break_glass"}:
            self._mark_admin_action(
                action_id,
                "dry_run_blocked",
                {"status": "dry_run", "reason": "global_dry_run_enabled"},
            )
            return self.get_admin_action(action_id)
        if action["approval_status"] == "required":
            if not confirmation_token:
                raise ValueError("confirmation_token is required for high risk admin action")
            if _hash_text(confirmation_token) != action.get("confirmation_token_hash"):
                raise ValueError("confirmation_token does not match")
            if _is_expired(action.get("token_expires_at")):
                self._mark_admin_action(action_id, "expired", {"error": "confirmation_expired"})
                return self.get_admin_action(action_id)
        current_params_hash = _hash_json(action["params"])
        if current_params_hash != action["params_hash"]:
            raise ValueError("admin action params hash changed after preview")
        result = spec.execute(action["params"])
        audit = self.record_quality_event(
            event_type="admin_action",
            severity="warning" if spec.risk_level in {"high", "break_glass"} else "info",
            status="resolved",
            source_type="admin_action",
            source_id=action_id,
            message=f"Admin action executed: {action['action_name']}",
            payload={"actor": actor, "result": result, "risk_level": spec.risk_level},
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE memory_admin_action
                SET execution_status = 'executed',
                    approval_status = CASE
                        WHEN approval_status = 'required' THEN 'approved'
                        ELSE approval_status
                    END,
                    result_json = ?,
                    audit_event_id = ?,
                    executed_at = ?
                WHERE id = ?
                """,
                (json_dumps(result), audit["id"], utc_now(), action_id),
            )
            self._conn.commit()
        return self.get_admin_action(action_id)

    def list_admin_actions(
        self,
        *,
        limit: int = 20,
        execution_status: str | None = None,
    ) -> list[JsonDict]:
        with self._lock:
            if execution_status:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_admin_action
                    WHERE execution_status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (execution_status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_admin_action
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._admin_action_from_row(row) for row in rows]

    def get_admin_action(self, action_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_admin_action WHERE id = ?",
                (action_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"admin action not found: {action_id}")
        return self._admin_action_from_row(row)

    def set_global_dry_run(self, enabled: bool, *, ttl_seconds: int | None = None) -> JsonDict:
        value = {
            "enabled": bool(enabled),
            "expires_at": _iso_after(ttl_seconds) if enabled and ttl_seconds else None,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_quality_setting (key, value_json, updated_at)
                VALUES ('admin.global_dry_run', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (json_dumps(value), utc_now()),
            )
            self._conn.commit()
        return {"key": "admin.global_dry_run", "value": value}

    def global_dry_run_enabled(self) -> bool:
        setting = self._get_setting("admin.global_dry_run")
        if not setting.get("enabled"):
            return False
        expires_at = setting.get("expires_at")
        if expires_at and _is_expired(str(expires_at)):
            self.set_global_dry_run(False)
            return False
        return True

    def record_quality_event(
        self,
        *,
        event_type: str,
        severity: str,
        message: str,
        status: str = "open",
        source_type: str | None = None,
        source_id: str | None = None,
        correlation_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> JsonDict:
        event_id = new_id("qevent")
        created_at = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_quality_event (
                    id, event_type, severity, status, source_type, source_id,
                    correlation_id, message, payload_json, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    severity,
                    status,
                    source_type,
                    source_id,
                    correlation_id,
                    message,
                    json_dumps(payload or {}),
                    created_at,
                    created_at if status == "resolved" else None,
                ),
            )
            self._conn.commit()
        return {
            "id": event_id,
            "event_type": event_type,
            "severity": severity,
            "status": status,
            "source_type": source_type,
            "source_id": source_id,
            "correlation_id": correlation_id,
            "message": message,
            "payload": payload or {},
            "created_at": created_at,
        }

    def list_quality_events(
        self,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 20,
    ) -> list[JsonDict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM memory_quality_event
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def cleanup_metrics(self, *, older_than_days: int = 30) -> JsonDict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            before = self._count_table("memory_quality_metric_point")
            self._conn.execute(
                """
                DELETE FROM memory_quality_metric_point
                WHERE created_at < ?
                  AND source_ref NOT LIKE 'release_gate:%'
                """,
                (cutoff,),
            )
            after = self._count_table("memory_quality_metric_point")
            self._conn.commit()
        return {"status": "ok", "deleted_count": before - after, "remaining_count": after}

    def export_quality_report(
        self,
        *,
        run_id: str | None = None,
        output_path: str | Path | None = None,
    ) -> JsonDict:
        report = {
            "exported_at": utc_now(),
            "dashboard": self.dashboard(),
            "run": self.get_quality_run(run_id) if run_id else None,
            "extensions": self._quality_export_extensions(),
        }
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json_dumps(report), encoding="utf-8")
            report["output_path"] = str(path)
        self.record_quality_event(
            event_type="quality_export",
            severity="info",
            status="resolved",
            message="Quality report exported",
            source_type="quality_run" if run_id else "dashboard",
            source_id=run_id,
            payload={"output_path": str(output_path) if output_path else None},
        )
        return report

    def _quality_export_extensions(self) -> JsonDict:
        extensions: JsonDict = {}
        for name, provider in sorted(self._export_sections.items()):
            try:
                extensions[name] = provider()
            except Exception as exc:  # report export must remain available
                extensions[name] = {
                    "status": "degraded",
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                }
        return extensions

    def _register_builtin_actions(self) -> None:
        self.register_admin_action(
            AdminActionSpec(
                name="run_quality_eval",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_quality_run", "memory_quality_case_result"],
                        "suites": params.get("suites") or "all",
                    }
                },
                execute=lambda params: self.run_quality_eval(
                    suites=params.get("suites"),
                    run_type=str(params.get("run_type") or "module_eval"),
                    trigger="admin_action",
                    policy_profile=str(params.get("policy_profile") or "core"),
                    dataset_version=str(params.get("dataset_version") or "default"),
                ),
                description="Run registered memory eval suites.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="run_scenario_replay",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_quality_run", "memory_quality_case_result"],
                        "package_name": params.get("package_name") or "default_memory_scenario_replay",
                    }
                },
                execute=lambda params: self.run_scenario_replay(
                    package_name=str(params.get("package_name") or "default_memory_scenario_replay"),
                    suites=params.get("suites"),
                    policy_profile=str(params.get("policy_profile") or "core"),
                    dataset_version=str(params.get("dataset_version") or "default"),
                ),
                description="Run a scenario replay package through the eval registry.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="sync_trace_index",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_trace_index"],
                        "limit": int(params.get("limit") or 500),
                    }
                },
                execute=lambda params: self.sync_trace_index(limit=int(params.get("limit") or 500)),
                description="Synchronize trace index from existing trace tables.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="collect_quality_metrics",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {"mutates": ["memory_quality_metric_point"]}
                },
                execute=lambda params: self.collect_metrics(source_ref="admin_action"),
                description="Collect memory quality metrics and SLO checks.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="pin_quality_baseline",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_quality_baseline"],
                        "run_id": params.get("run_id"),
                        "promote": bool(params.get("promote")),
                    }
                },
                execute=lambda params: self.pin_baseline(
                    str(params["run_id"]),
                    name=str(params.get("name") or "quality-baseline"),
                    policy_profile=str(params.get("policy_profile") or "core"),
                    dataset_version=str(params.get("dataset_version") or "default"),
                    status=str(params.get("status") or "candidate"),
                    accepted_failures=params.get("accepted_failures") or [],
                    promote=bool(params.get("promote")),
                ),
                rollback_plan=lambda params: {
                    "type": "baseline_pointer_rollback",
                    "note": "Baseline rows are immutable; restore the previous active baseline pointer if promotion was wrong.",
                },
                description="Pin or promote a quality baseline.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="export_quality_report",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_quality_event"],
                        "output_path": params.get("output_path"),
                    }
                },
                execute=lambda params: self.export_quality_report(
                    run_id=params.get("run_id"),
                    output_path=params.get("output_path"),
                ),
                description="Export a redacted quality report as JSON.",
            )
        )
        self.register_admin_action(
            AdminActionSpec(
                name="run_release_gate",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": [
                            "memory_quality_run",
                            "memory_quality_case_result",
                            "memory_quality_metric_point",
                            "memory_quality_event",
                        ],
                        "change_scope": params.get("change_scope") or ["all"],
                    }
                },
                execute=lambda params: self.run_release_gate(
                    change_scope=params.get("change_scope"),
                    baseline_id=params.get("baseline_id"),
                    policy_profile=str(params.get("policy_profile") or "core"),
                    dataset_version=str(params.get("dataset_version") or "default"),
                ),
                description="Run the change-aware release gate.",
            )
        )

    def _select_suites(self, suites: list[str] | None) -> list[EvalSuiteAdapter]:
        if suites is None:
            selected = list(self._eval_suites.values())
        else:
            missing = [name for name in suites if name not in self._eval_suites]
            if missing:
                raise ValueError(f"unknown eval suite(s): {', '.join(sorted(missing))}")
            selected = [self._eval_suites[name] for name in suites]
        return sorted(selected, key=lambda suite: suite.name)

    def _store_suite_result(
        self,
        run_id: str,
        suite: EvalSuiteAdapter,
        source_result: JsonDict,
        *,
        policy_profile: str,
    ) -> JsonDict:
        cases = list(source_result.get("cases") or [])
        if not cases and int(source_result.get("total_cases") or 0) == 0:
            cases = [
                {
                    "case_name": f"{suite.name}_smoke",
                    "status": source_result.get("status", "passed"),
                    "details": source_result,
                }
            ]
        source_run_id = str(source_result.get("run_id") or source_result.get("id") or "")
        passed = 0
        failed = 0
        with self._lock:
            for case in cases:
                status = str(case.get("status") or "failed")
                if status == "passed":
                    passed += 1
                else:
                    failed += 1
                case_name = str(case.get("case_name") or case.get("name") or case.get("id") or "case")
                self._conn.execute(
                    """
                    INSERT INTO memory_quality_case_result (
                        id, run_id, suite_name, case_name, status, severity,
                        expected_json, actual_json, metric_json, trace_refs_json,
                        diff_json, failure_category, source_run_ref,
                        source_case_ref, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("qcase"),
                        run_id,
                        suite.name,
                        case_name,
                        status,
                        suite.default_severity if status != "passed" else "info",
                        json_dumps(self._extract_prefixed(case, "expected")),
                        json_dumps(self._extract_prefixed(case, "actual")),
                        json_dumps(self._case_metrics(case)),
                        json_dumps(self._case_trace_refs(case, policy_profile)),
                        json_dumps(self._case_diff(case)),
                        self._failure_category(case),
                        source_run_id,
                        str(case.get("id") or case_name),
                        utc_now(),
                    ),
                )
            self._conn.commit()
        total = len(cases)
        return {
            "suite_name": suite.name,
            "source_module": suite.source_module,
            "source_run_id": source_run_id,
            "status": "failed" if failed else "passed",
            "total_cases": total,
            "passed_cases": passed,
            "failed_cases": failed,
            "summary": source_result.get("summary", {}),
        }

    def _store_suite_exception(
        self,
        run_id: str,
        suite: EvalSuiteAdapter,
        exc: Exception,
    ) -> JsonDict:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_quality_case_result (
                    id, run_id, suite_name, case_name, status, severity,
                    expected_json, actual_json, metric_json, trace_refs_json,
                    diff_json, failure_category, source_run_ref, source_case_ref,
                    created_at
                ) VALUES (?, ?, ?, ?, 'failed', 'blocker', '{}', ?, '{}', '{}', ?, ?, NULL, NULL, ?)
                """,
                (
                    new_id("qcase"),
                    run_id,
                    suite.name,
                    f"{suite.name}_runner_exception",
                    json_dumps({"exception": repr(exc)}),
                    json_dumps({"error": str(exc)}),
                    "runner_exception",
                    utc_now(),
                ),
            )
            self._conn.commit()
        return {
            "suite_name": suite.name,
            "source_module": suite.source_module,
            "source_run_id": None,
            "status": "failed",
            "total_cases": 1,
            "passed_cases": 0,
            "failed_cases": 1,
            "summary": {"exception": repr(exc)},
        }

    def _upsert_trace_index(
        self,
        *,
        trace_id: str,
        correlation_id: str,
        module: str,
        entity_type: str,
        entity_id: str,
        source_table: str,
        source_id: str,
        causality_refs: JsonDict,
        redaction_level: str,
        policy_profile: str | None,
        provider_versions: JsonDict,
        cost_latency_tokens: JsonDict,
        created_at: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        replay: bool = False,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_trace_index (
                id, trace_id, correlation_id, session_id, turn_id, module,
                entity_type, entity_id, source_table, source_id,
                causality_refs_json, redaction_level, policy_profile,
                provider_versions_json, cost_latency_tokens_json, replay,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_table, source_id, module, entity_type) DO UPDATE SET
                trace_id = excluded.trace_id,
                correlation_id = excluded.correlation_id,
                session_id = excluded.session_id,
                turn_id = excluded.turn_id,
                entity_id = excluded.entity_id,
                causality_refs_json = excluded.causality_refs_json,
                redaction_level = excluded.redaction_level,
                policy_profile = excluded.policy_profile,
                provider_versions_json = excluded.provider_versions_json,
                cost_latency_tokens_json = excluded.cost_latency_tokens_json,
                replay = excluded.replay,
                updated_at = excluded.updated_at
            """,
            (
                new_id("qtrace"),
                trace_id,
                correlation_id,
                session_id,
                turn_id,
                module,
                entity_type,
                entity_id,
                source_table,
                source_id,
                json_dumps(causality_refs),
                redaction_level,
                policy_profile,
                json_dumps(provider_versions),
                json_dumps(cost_latency_tokens),
                int(replay),
                created_at,
                utc_now(),
            ),
        )

    @staticmethod
    def _turn_correlation(session_id: str | None, turn_id: str | None, fallback: str) -> str:
        if session_id and turn_id:
            return f"turn:{session_id}:{turn_id}"
        if session_id:
            return f"session:{session_id}"
        return f"trace:{fallback}"

    def _count_table(self, table: str) -> int:
        if table not in _COUNTABLE_TABLES:
            raise ValueError(f"unexpected count table: {table}")
        return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _open_event_count(self, event_type: str) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_quality_event
                    WHERE event_type = ? AND status = 'open'
                    """,
                    (event_type,),
                ).fetchone()[0]
            )

    @staticmethod
    def _extract_prefixed(case: JsonDict, prefix: str) -> JsonDict:
        return {key: value for key, value in case.items() if key.startswith(prefix)}

    @staticmethod
    def _case_metrics(case: JsonDict) -> JsonDict:
        metrics = {}
        for key, value in case.items():
            if isinstance(value, int | float | bool):
                metrics[key] = value
        return metrics

    @staticmethod
    def _case_trace_refs(case: JsonDict, policy_profile: str) -> JsonDict:
        refs = {
            "policy_profile": policy_profile,
        }
        for key in ("trace_id", "recall_trace_id", "plan_id", "memory_id", "candidate_id"):
            if case.get(key):
                refs[key] = case[key]
        if isinstance(case.get("trace_refs"), dict):
            refs.update(case["trace_refs"])
        return refs

    @staticmethod
    def _case_diff(case: JsonDict) -> JsonDict:
        diff = {}
        for key, value in case.items():
            if key.startswith("expected_"):
                actual_key = "actual_" + key[len("expected_") :]
                if actual_key in case and case[actual_key] != value:
                    diff[key[len("expected_") :]] = {
                        "expected": value,
                        "actual": case[actual_key],
                    }
        return diff

    @staticmethod
    def _failure_category(case: JsonDict) -> str | None:
        if str(case.get("status")) == "passed":
            return None
        for key in ("failure_category", "reason", "error"):
            if case.get(key):
                return str(case[key])
        return "assertion_failed"

    def _baseline_snapshot(self, run: JsonDict) -> JsonDict:
        return {
            "run_id": run["id"],
            "summary": run.get("summary", {}),
            "cases": {
                f"{case['suite_name']}::{case['case_name']}": {
                    "status": case["status"],
                    "severity": case["severity"],
                    "failure_category": case.get("failure_category"),
                }
                for case in run.get("cases", [])
            },
        }

    def _active_baseline(self, run: JsonDict) -> JsonDict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM memory_quality_baseline
                WHERE policy_profile = ? AND dataset_version = ? AND status = 'active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (run["policy_profile"], run["dataset_version"]),
            ).fetchone()
        return self._baseline_from_row(row) if row else None

    def _suites_for_change_scope(self, change_scope: list[str]) -> list[str]:
        normalized = {item.lower() for item in change_scope}
        if "all" in normalized:
            return list(sorted(self._eval_suites))
        selected: set[str] = set()
        mapping = {
            "extractor": {"extractor"},
            "gate": {"canonical"},
            "canonical": {"canonical"},
            "store": {"canonical"},
            "recall": {"recall", "planner"},
            "planner": {"planner"},
            "surface": {"surface"},
            "lifecycle": {"lifecycle"},
            "forget": {"lifecycle", "canonical"},
            "projection": {"projection_gateway"},
            "projector": {"projection_gateway"},
            "interruptibility": {"interruptibility"},
            "dialogue_control": {"interruptibility"},
            "initiative": {"interruptibility"},
            "conversation_runtime": {"conversation_runtime"},
            "text_runtime": {"conversation_runtime"},
            "e2e": {"conversation_runtime"},
            "persona": {"persona"},
            "persona_growth": {"persona", "projection_gateway"},
        }
        for scope in normalized:
            selected.update(mapping.get(scope, set()))
        return [suite for suite in sorted(selected) if suite in self._eval_suites] or list(
            sorted(self._eval_suites)
        )

    def _require_action(self, action_name: str) -> AdminActionSpec:
        if action_name not in self._admin_actions:
            raise ValueError(f"unknown admin action: {action_name}")
        return self._admin_actions[action_name]

    @staticmethod
    def _role_allows(role: str, risk_level: str) -> bool:
        normalized = role.lower().strip()
        matrix = {
            "observer": {"read_only"},
            "operator": {"read_only", "low", "medium"},
            "reviewer": {"read_only", "low", "medium"},
            "admin": {"read_only", "low", "medium", "high"},
            "break_glass": {"read_only", "low", "medium", "high", "break_glass"},
        }
        return risk_level in matrix.get(normalized, set())

    def _mark_admin_action(self, action_id: str, status: str, result: JsonDict) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE memory_admin_action
                SET execution_status = ?, result_json = ?, executed_at = ?
                WHERE id = ?
                """,
                (status, json_dumps(result), utc_now(), action_id),
            )
            self._conn.commit()

    def _get_setting(self, key: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM memory_quality_setting WHERE key = ?",
                (key,),
            ).fetchone()
        return _loads(row["value_json"], {}) if row else {}

    @staticmethod
    def _run_from_row(row: sqlite3.Row, *, include_cases: bool) -> JsonDict:
        return {
            "id": row["id"],
            "run_type": row["run_type"],
            "status": row["status"],
            "trigger": row["trigger"],
            "policy_profile": row["policy_profile"],
            "dataset_version": row["dataset_version"],
            "baseline_id": row["baseline_id"],
            "scope": _loads(row["scope_json"], {}),
            "module_versions": _loads(row["module_versions_json"], {}),
            "summary": _loads(row["summary_json"], {}),
            "blockers": _loads(row["blockers_json"], []),
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            **({"cases": []} if include_cases else {}),
        }

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "suite_name": row["suite_name"],
            "case_name": row["case_name"],
            "status": row["status"],
            "severity": row["severity"],
            "expected": _loads(row["expected_json"], {}),
            "actual": _loads(row["actual_json"], {}),
            "metrics": _loads(row["metric_json"], {}),
            "trace_refs": _loads(row["trace_refs_json"], {}),
            "diff": _loads(row["diff_json"], {}),
            "failure_category": row["failure_category"],
            "source_run_ref": row["source_run_ref"],
            "source_case_ref": row["source_case_ref"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _baseline_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "name": row["name"],
            "run_id": row["run_id"],
            "status": row["status"],
            "policy_profile": row["policy_profile"],
            "dataset_version": row["dataset_version"],
            "snapshot": _loads(row["snapshot_json"], {}),
            "accepted_failures": _loads(row["accepted_failures_json"], []),
            "supersedes_id": row["supersedes_id"],
            "created_at": row["created_at"],
            "promoted_at": row["promoted_at"],
        }

    def _get_admin_action_private(self, action_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_admin_action WHERE id = ?",
                (action_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"admin action not found: {action_id}")
        action = self._admin_action_from_row(row)
        action["confirmation_token_hash"] = row["confirmation_token_hash"]
        action["token_expires_at"] = row["token_expires_at"]
        return action

    @staticmethod
    def _admin_action_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "action_name": row["action_name"],
            "risk_level": row["risk_level"],
            "actor": row["actor"],
            "role": row["role"],
            "reason": row["reason"],
            "params_hash": row["params_hash"],
            "params": _loads(row["params_json"], {}),
            "preview": _loads(row["preview_json"], {}),
            "impact_summary": _loads(row["impact_summary_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "approval_status": row["approval_status"],
            "execution_status": row["execution_status"],
            "result": _loads(row["result_json"], {}),
            "audit_event_id": row["audit_event_id"],
            "dry_run": bool(row["dry_run"]),
            "created_at": row["created_at"],
            "executed_at": row["executed_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "severity": row["severity"],
            "status": row["status"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "correlation_id": row["correlation_id"],
            "message": row["message"],
            "payload": _loads(row["payload_json"], {}),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }
