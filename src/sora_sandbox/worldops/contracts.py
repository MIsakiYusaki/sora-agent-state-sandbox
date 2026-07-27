from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


JsonDict = dict[str, Any]

WORLD_CONTENT_BUNDLE_CONTRACT = "WorldContentBundle.v1"
WORLD_BUNDLE_MANIFEST_CONTRACT = "WorldBundleManifest.v1"
WORLDOPS_FRAME_CONTRACT = "WorldOpsFrame.v1"
USER_CHOICE_INTERPRETATION_CONTRACT = "UserChoiceInterpretation.v1"
TASK_EVIDENCE_CONTRACT = "TaskEvidence.v1"
NARRATIVE_CONTRIBUTION_CONTRACT = "NarrativeContributionProposal.v1"
PERSONAL_TIMELINE_MUTATION_CONTRACT = "PersonalTimelineMutation.v1"
WORLDOPS_EVENT_ENVELOPE_CONTRACT = "WorldOpsEventEnvelope.v1"
WORLDOPS_PROJECTION_ADAPTER_VERSION = "worldops_projection_adapter.v1"
WORLDOPS_SCHEMA_VERSION = 1

EVENT_STATES = {"dormant", "hinted", "invited", "active", "completed", "archived"}
TERMINAL_EVENT_STATES = {"completed", "archived"}
CONTRIBUTION_STATUSES = {
    "received",
    "quarantined",
    "under_review",
    "merged",
    "accepted",
    "rejected",
    "withdrawn",
    "published",
}
EDITORIAL_DECISIONS = {
    "canon_fact",
    "official_storylet",
    "world_echo",
    "arc_inspiration",
    "merge_duplicate",
    "reject",
}
EXPRESSION_LEVELS = {"L0", "L1", "L2", "L3"}
PRIVACY_CLASSES = {"public", "internal_pseudonymous", "private_local", "restricted"}
IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{1,127}$")


class WorldOpsContractError(ValueError):
    """Raised when a caller crosses the WorldOps boundary with invalid data."""


class WorldOpsConflictError(RuntimeError):
    """Raised when optimistic revision or timeline ownership checks fail."""


class WorldOpsSecurityError(RuntimeError):
    """Raised when signatures, identity, or anti-rollback checks fail."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_identifier(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise WorldOpsContractError(f"{name} must be a stable identifier")
    return text


def _unit(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WorldOpsContractError(f"{name} must be numeric") from exc
    if not 0.0 <= number <= 1.0:
        raise WorldOpsContractError(f"{name} must be in [0, 1]")
    return number


@dataclass(frozen=True, slots=True)
class IdentityScope:
    tenant_id: str
    user_id: str
    sora_id: str
    timeline_id: str

    def validate(self) -> None:
        for name in ("tenant_id", "user_id", "sora_id", "timeline_id"):
            _required_identifier(getattr(self, name), name)

    @property
    def key(self) -> str:
        self.validate()
        return ":".join((self.tenant_id, self.user_id, self.sora_id, self.timeline_id))

    @property
    def pseudonymous_subject_id(self) -> str:
        return "ps_" + content_hash(self.key)[:32]

    def to_dict(self, *, public: bool = False) -> JsonDict:
        if public:
            return {"pseudonymous_subject_id": self.pseudonymous_subject_id}
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorldBundleManifest:
    bundle_id: str
    world_id: str
    canon_version: str
    release_sequence: int
    minimum_accepted_sequence: int
    key_id: str
    issued_at: str
    expires_at: str
    revocation_list_version: int
    content_hash: str
    channel: str = "stable"
    cohort: str = "default"
    schema_version: str = WORLD_BUNDLE_MANIFEST_CONTRACT
    signature: str | None = None
    rollback_authorization: str | None = None
    base_bundle_id: str | None = None

    def validate(self) -> None:
        if self.schema_version != WORLD_BUNDLE_MANIFEST_CONTRACT:
            raise WorldOpsContractError("unsupported bundle manifest contract")
        for name in ("bundle_id", "world_id", "canon_version", "key_id", "channel", "cohort"):
            _required_identifier(getattr(self, name), name)
        if self.release_sequence < 1:
            raise WorldOpsContractError("release_sequence must be positive")
        if not 0 <= self.minimum_accepted_sequence <= self.release_sequence:
            raise WorldOpsContractError("minimum_accepted_sequence is invalid")
        if self.revocation_list_version < 0:
            raise WorldOpsContractError("revocation_list_version cannot be negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise WorldOpsContractError("content_hash must be a sha256 hex digest")
        issued = datetime.fromisoformat(self.issued_at)
        expires = datetime.fromisoformat(self.expires_at)
        if issued.tzinfo is None or expires.tzinfo is None or expires <= issued:
            raise WorldOpsContractError("manifest timestamps must be timezone-aware and ordered")

    def signing_payload(self) -> bytes:
        payload = self.to_dict()
        payload.pop("signature", None)
        return canonical_json(payload).encode("utf-8")

    def to_dict(self) -> JsonDict:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorldBundleManifest":
        manifest = cls(
            bundle_id=str(payload.get("bundle_id") or ""),
            world_id=str(payload.get("world_id") or ""),
            canon_version=str(payload.get("canon_version") or ""),
            release_sequence=int(payload.get("release_sequence", 0)),
            minimum_accepted_sequence=int(payload.get("minimum_accepted_sequence", 0)),
            key_id=str(payload.get("key_id") or ""),
            issued_at=str(payload.get("issued_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            revocation_list_version=int(payload.get("revocation_list_version", 0)),
            content_hash=str(payload.get("content_hash") or ""),
            channel=str(payload.get("channel") or "stable"),
            cohort=str(payload.get("cohort") or "default"),
            schema_version=str(payload.get("schema_version") or WORLD_BUNDLE_MANIFEST_CONTRACT),
            signature=(str(payload["signature"]) if payload.get("signature") else None),
            rollback_authorization=(
                str(payload["rollback_authorization"])
                if payload.get("rollback_authorization")
                else None
            ),
            base_bundle_id=(str(payload["base_bundle_id"]) if payload.get("base_bundle_id") else None),
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True, slots=True)
class UserChoiceInterpretation:
    interpretation_id: str
    event_instance_id: str
    input_id: str
    choice_id: str | None
    task_evidence: tuple[JsonDict, ...]
    confidence: float
    ambiguous: bool
    clarification: str | None
    unsupported_claims: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    model_trace: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = USER_CHOICE_INTERPRETATION_CONTRACT

    def validate(self, *, allowed_choice_ids: set[str], allowed_task_ids: set[str]) -> None:
        if self.schema_version != USER_CHOICE_INTERPRETATION_CONTRACT:
            raise WorldOpsContractError("unsupported choice interpretation contract")
        for name in ("interpretation_id", "event_instance_id", "input_id"):
            _required_identifier(getattr(self, name), name)
        _unit(self.confidence, "confidence")
        if self.choice_id is not None and self.choice_id not in allowed_choice_ids:
            raise WorldOpsContractError("choice interpretation references an unknown choice")
        for item in self.task_evidence:
            task_id = str(item.get("task_id") or "")
            if task_id not in allowed_task_ids:
                raise WorldOpsContractError("task evidence references an unknown task")

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["task_evidence"] = [dict(item) for item in self.task_evidence]
        payload["unsupported_claims"] = list(self.unsupported_claims)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["model_trace"] = dict(self.model_trace)
        return payload


def validate_content_bundle(bundle: Mapping[str, Any]) -> JsonDict:
    payload = dict(bundle)
    if payload.get("contract") != WORLD_CONTENT_BUNDLE_CONTRACT:
        raise WorldOpsContractError("unsupported content bundle contract")
    world = dict(payload.get("world") or {})
    world_id = _required_identifier(world.get("world_id"), "world.world_id")
    if not str(world.get("name") or "").strip():
        raise WorldOpsContractError("world.name is required")
    events = payload.get("events")
    if not isinstance(events, list):
        raise WorldOpsContractError("events must be a list")
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise WorldOpsContractError("each event must be an object")
        event_id = _required_identifier(event.get("event_id"), "event_id")
        if event_id in seen:
            raise WorldOpsContractError(f"duplicate event_id: {event_id}")
        seen.add(event_id)
        _validate_event_definition(event)
    payload["world"] = world | {"world_id": world_id}
    return payload


def _validate_event_definition(event: Mapping[str, Any]) -> None:
    if not str(event.get("title") or "").strip():
        raise WorldOpsContractError("event title is required")
    if not str(event.get("summary") or event.get("one_sentence") or "").strip():
        raise WorldOpsContractError("event summary is required")
    initial_state = str(event.get("initial_state") or "dormant")
    if initial_state not in EVENT_STATES:
        raise WorldOpsContractError("event initial_state is invalid")
    choices = event.get("choices") or []
    choice_ids: set[str] = set()
    for choice in choices:
        choice_id = _required_identifier((choice or {}).get("choice_id"), "choice_id")
        if choice_id in choice_ids:
            raise WorldOpsContractError(f"duplicate choice_id: {choice_id}")
        choice_ids.add(choice_id)
    task_ids: set[str] = set()
    for task in event.get("tasks") or []:
        task_id = _required_identifier((task or {}).get("task_id"), "task_id")
        if task_id in task_ids:
            raise WorldOpsContractError(f"duplicate task_id: {task_id}")
        task_ids.add(task_id)
        if not isinstance(task.get("completion"), dict):
            raise WorldOpsContractError(f"task {task_id} requires a completion predicate")
    for transition in event.get("transitions") or []:
        source = str((transition or {}).get("from") or "")
        target = str((transition or {}).get("to") or "")
        if source not in EVENT_STATES or target not in EVENT_STATES:
            raise WorldOpsContractError("event transition references an invalid state")
        if source in TERMINAL_EVENT_STATES and target != "archived":
            raise WorldOpsContractError("terminal event states cannot resume without migration")


def make_event_envelope(
    event_type: str,
    *,
    subject: Mapping[str, Any],
    payload: Mapping[str, Any],
    correlation_id: str | None,
    causation_id: str | None,
    producer: str,
    event_id: str,
    idempotency_key: str,
    privacy_class: str = "private_local",
) -> JsonDict:
    _required_identifier(event_id, "event_id")
    _required_identifier(event_type, "event_type")
    if privacy_class not in PRIVACY_CLASSES:
        raise WorldOpsContractError("invalid privacy_class")
    return {
        "contract": WORLDOPS_EVENT_ENVELOPE_CONTRACT,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": utc_now(),
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "producer": producer,
        "contract_version": 1,
        "subject": dict(subject),
        "payload": dict(payload),
        "privacy_class": privacy_class,
        "idempotency_key": idempotency_key,
    }


def validate_event_envelope(event: Mapping[str, Any]) -> None:
    if event.get("contract") != WORLDOPS_EVENT_ENVELOPE_CONTRACT:
        raise WorldOpsContractError("unsupported event envelope")
    _required_identifier(event.get("event_id"), "event_id")
    _required_identifier(event.get("event_type"), "event_type")
    _required_identifier(event.get("producer"), "producer")
    if not str(event.get("idempotency_key") or ""):
        raise WorldOpsContractError("idempotency_key is required")
    if not isinstance(event.get("subject"), dict) or not isinstance(event.get("payload"), dict):
        raise WorldOpsContractError("event subject and payload must be objects")


def stable_list(values: Sequence[Any]) -> list[str]:
    return sorted({str(value) for value in values if str(value).strip()})
