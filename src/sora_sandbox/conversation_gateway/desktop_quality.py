"""Scoped Module 10 inspection and explicit audited event resolution."""
import hashlib
import math
import re
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from pydantic import Field, field_validator

from .contracts import ProtocolModel, GatewayErrorCode, validate_identifier
from .service import GatewayServiceError

QualityKind = Literal["runs", "baselines", "events"]


class QualityRow(ProtocolModel):
    id: str
    label: str
    status: str
    created_at: str | None = None
    revision: str | None = None
    related_run_id: str | None = None
    severity: str | None = None
    source_type: str | None = None
    source_fingerprint: str | None = None
    resolved_at: str | None = None


class QualityCase(ProtocolModel):
    id: str
    suite: str
    name: str
    status: str
    severity: str
    failure_category: str | None
    metrics: dict[str, float]


class QualityComparison(ProtocolModel):
    status: str
    baseline_id: str | None
    regression_count: int = Field(ge=0)
    unaccepted_regression_count: int = Field(ge=0)
    baseline_pass_rate: float | None
    current_pass_rate: float | None


class QualityPage(ProtocolModel):
    schema_version: Literal["DesktopQualityPage.v1"] = "DesktopQualityPage.v1"
    kind: QualityKind
    items: list[QualityRow]
    truncated: bool
    selected: QualityRow | None = None
    cases: list[QualityCase] | None = None
    case_count: int | None = None
    next_case_offset: int | None = None
    summary: dict[str, float] = Field(default_factory=dict)
    comparison: QualityComparison | None = None


class QualityReceipt(ProtocolModel):
    status: Literal["resolved", "already_resolved"]
    event_id: str
    audit_event_id: str


class QualityResolution(ProtocolModel):
    event_id: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution: Literal["investigated", "superseded", "false_positive"]
    confirmed: Literal[True]

    @field_validator("event_id")
    @classmethod
    def identifier(cls, value):
        return validate_identifier(value)

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirm(cls, value):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True


def _code(value: Any) -> str:
    # These fields are registered evaluation labels/codes, not free-form messages.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:]{0,119}", value) else "protected"


def _metrics(values: Any) -> dict[str, float]:
    allowed = {"total_cases", "passed_cases", "failed_cases", "pass_rate", "accuracy", "precision", "recall", "f1", "latency_ms", "duration_ms", "score"}
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in allowed and type(value) in {int, float} and math.isfinite(value)}


def install_quality_routes(app: FastAPI, *, ops, owner):
    def view(kind, item):
        label = item.get("run_type") if kind == "runs" else item.get("event_type") if kind == "events" else "quality_baseline"
        related_run_id = item.get("run_id") if kind == "baselines" else None
        if kind == "events" and item.get("source_type") == "quality_run" and item.get("source_id"):
            try:
                related_run_id = ops.get_quality_run(item["source_id"])["id"]
            except ValueError:
                pass
        return QualityRow(id=item["id"], label=_code(label), status=_code(item.get("status")), created_at=item.get("created_at"),
            related_run_id=related_run_id,
            severity=_code(item.get("severity")) if kind == "events" else None,
            source_type=_code(item.get("source_type")) if kind == "events" else None,
            source_fingerprint=hashlib.sha256(str(item["source_id"]).encode()).hexdigest()[:16] if kind == "events" and item.get("source_id") else None,
            resolved_at=item.get("resolved_at") if kind == "events" else None,
            revision=ops.quality_event_revision(item) if kind == "events" else None)

    @app.get("/v1/debug/quality", response_model=QualityPage)
    def inspect(request: Request, kind: QualityKind, record_id: str | None = Query(default=None, max_length=128), baseline_id: str | None = Query(default=None, max_length=128), case_offset: int = Query(default=0, ge=0, le=1_000_000)):
        owner(request, "debug.quality.read")
        try:
            for identifier in (record_id, baseline_id):
                if identifier is not None:
                    validate_identifier(identifier)
            records = (ops.list_quality_runs if kind == "runs" else ops.list_baselines if kind == "baselines" else ops.list_quality_events)(limit=101)
            result = {"kind": kind, "items": [view(kind, item) for item in records[:100]], "truncated": len(records) > 100}
            if record_id:
                record = (ops.get_quality_run if kind == "runs" else ops.get_baseline if kind == "baselines" else ops.get_quality_event)(record_id)
                result["selected"] = view(kind, record)
                result["summary"] = _metrics(record.get("summary") if kind == "runs" else (record.get("snapshot") or {}).get("summary"))
                if kind == "runs":
                    cases = record.get("cases") or []
                    result["case_count"] = len(cases)
                    result["next_case_offset"] = case_offset + 100 if len(cases) > case_offset + 100 else None
                    result["cases"] = [QualityCase(id=item["id"], suite=_code(item.get("suite_name")), name=_code(item.get("case_name")), status=_code(item.get("status")), severity=_code(item.get("severity")),
                        failure_category=_code(item["failure_category"]) if item.get("failure_category") else None, metrics=_metrics(item.get("metrics"))) for item in cases[case_offset:case_offset + 100]]
                    if baseline_id:
                        comparison = ops.compare_baseline(record_id, baseline_id=baseline_id, persist_event=False)
                        result["comparison"] = {"status": _code(comparison.get("status")), "baseline_id": comparison.get("baseline_id"),
                            "regression_count": len(comparison.get("regressions") or []), "unaccepted_regression_count": len(comparison.get("unaccepted_regressions") or []),
                            "baseline_pass_rate": comparison.get("baseline_pass_rate"), "current_pass_rate": comparison.get("current_pass_rate")}
            return QualityPage(**result)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Quality record unavailable; reload current records", status_code=409) from None

    @app.post("/v1/debug/quality/resolve", response_model=QualityReceipt)
    def resolve(request: Request, payload: QualityResolution):
        identity = owner(request, "debug.quality.resolve")
        actor = "desktop_quality_" + hashlib.sha256("\x1f".join((identity.profile_id, identity.subject_id, identity.agent_id, identity.client_instance_id)).encode()).hexdigest()
        try:
            return ops.resolve_quality_event(payload.event_id, expected_revision=payload.expected_revision, resolution=payload.resolution, actor=actor)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Quality event changed; reload before resolving", status_code=409) from None
