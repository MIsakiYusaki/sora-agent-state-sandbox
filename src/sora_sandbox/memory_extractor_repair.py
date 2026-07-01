from __future__ import annotations

from copy import deepcopy
from typing import Any

from .memory_extractor_schema import EXTRACTOR_RESULT_SCHEMA_VERSION
from .memory_schema import JsonDict


OPERATION_ALIASES = {
    "add": "ADD",
    "update": "UPDATE",
    "merge": "MERGE",
    "delete": "DELETE",
    "noop": "NOOP",
    "no_op": "NOOP",
    "none": "NOOP",
    "review": "REVIEW",
}

TYPE_ALIASES = {
    "project": "project_principle",
    "principle": "project_principle",
    "relationship": "relationship_signal",
    "worldops": "world_ops",
    "worldops_episode": "worldops_episode",
    "world_op": "world_ops",
    "procedure": "procedural_hint",
}


def repair_extractor_json(payload: JsonDict, input_package: JsonDict) -> tuple[JsonDict, list[str]]:
    """Normalize common real-LLM JSON drift before strict validation."""

    repaired: JsonDict = deepcopy(payload)
    repairs: list[str] = []
    repaired.setdefault("schema_version", EXTRACTOR_RESULT_SCHEMA_VERSION)
    repaired.setdefault("job_id", input_package.get("job", {}).get("job_id"))
    repaired.setdefault("episode_id", input_package.get("episode", {}).get("episode_id"))
    repaired.setdefault("candidates", [])
    repaired.setdefault("contradictions", [])
    if not isinstance(repaired.get("trace"), dict):
        repaired["trace"] = {"raw_trace": repaired.get("trace")}
        repairs.append("trace_reset_to_object")
    repaired.setdefault("trace", {})

    if isinstance(repaired.get("candidate"), dict) and not repaired.get("candidates"):
        repaired["candidates"] = [repaired.pop("candidate")]
        repairs.append("candidate_to_candidates")

    if not isinstance(repaired.get("candidates"), list):
        repaired["candidates"] = []
        repairs.append("candidates_reset_to_empty_list")
    repaired["candidates"] = [
        item if isinstance(item, dict) else _noop_candidate_from_text(str(item))
        for item in repaired["candidates"]
    ]

    for candidate in repaired["candidates"]:
        _repair_candidate(candidate, input_package, repairs)

    first_candidate_id = next(
        (str(item.get("candidate_id")) for item in repaired["candidates"] if item.get("candidate_id")),
        None,
    )
    first_target_memory_id = next(
        (str(item.get("target_memory_id")) for item in repaired["candidates"] if item.get("target_memory_id")),
        None,
    )
    first_related_memory_id = next(
        (str(item.get("id")) for item in input_package.get("related_memories", []) if item.get("id")),
        None,
    )
    if isinstance(repaired.get("contradictions"), list):
        for contradiction in repaired["contradictions"]:
            if isinstance(contradiction, dict) and not contradiction.get("new_candidate_id") and first_candidate_id:
                contradiction["new_candidate_id"] = first_candidate_id
                repairs.append("contradiction_new_candidate_id_filled")
            if isinstance(contradiction, dict) and not contradiction.get("existing_memory_id"):
                existing_memory_id = first_target_memory_id or first_related_memory_id
                if existing_memory_id:
                    contradiction["existing_memory_id"] = existing_memory_id
                    repairs.append("contradiction_existing_memory_id_filled")
            if isinstance(contradiction, dict):
                resolution = str(contradiction.get("resolution") or "").upper()
                if resolution not in {"UPDATE", "REVIEW", "NOOP", "DELETE"}:
                    contradiction["resolution"] = "UPDATE" if contradiction.get("existing_memory_id") else "REVIEW"
                    repairs.append("contradiction_resolution_defaulted")

    repaired["trace"]["json_repair"] = repairs
    return repaired, repairs


def _repair_candidate(candidate: JsonDict, input_package: JsonDict, repairs: list[str]) -> None:
    if not candidate.get("candidate_id"):
        candidate["candidate_id"] = f"draft_repaired_{len(repairs)}"
        repairs.append("candidate_id_filled")
    _rename(candidate, "type", "memory_type", repairs)
    _rename(candidate, "text", "summary", repairs)
    _rename(candidate, "content", "summary", repairs)
    _rename(candidate, "evidence", "evidence_refs", repairs)

    scores = candidate.get("scores")
    if isinstance(scores, dict):
        for key in ("confidence", "importance", "stability", "specificity", "novelty", "conflict_score"):
            if key not in candidate and key in scores:
                candidate[key] = scores[key]
                repairs.append(f"scores.{key}_flattened")

    if "operation" in candidate:
        raw_operation = str(candidate["operation"]).strip()
        candidate["operation"] = OPERATION_ALIASES.get(raw_operation.lower(), raw_operation.upper())
    if "memory_type" in candidate:
        raw_type = str(candidate["memory_type"] or "").strip()
        candidate["memory_type"] = TYPE_ALIASES.get(raw_type.lower(), raw_type)
    if not candidate.get("memory_type") or str(candidate.get("memory_type")).lower() == "none":
        candidate["memory_type"] = _infer_memory_type(candidate, input_package)
        repairs.append("memory_type_inferred")
    else:
        inferred = _infer_memory_type(candidate, input_package)
        if inferred != candidate["memory_type"] and _should_override_memory_type(candidate, inferred):
            candidate["memory_type"] = inferred
            repairs.append("memory_type_domain_normalized")

    for key in ("confidence", "importance", "stability", "specificity", "novelty", "conflict_score"):
        if key in candidate:
            converted = _float_or_none(candidate[key])
            if converted is not None:
                if candidate[key] != converted:
                    repairs.append(f"{key}_number_coerced")
                candidate[key] = max(0.0, min(1.0, converted))
            else:
                candidate[key] = _default_score(key)
                repairs.append(f"{key}_defaulted")

    refs = candidate.get("evidence_refs")
    if refs is None:
        return
    if isinstance(refs, (str, dict)):
        candidate["evidence_refs"] = [refs]
        repairs.append("evidence_refs_wrapped")
    if not isinstance(candidate.get("evidence_refs"), list):
        candidate["evidence_refs"] = []
        repairs.append("evidence_refs_reset_to_empty_list")
        return

    episode_id = str(input_package.get("episode", {}).get("episode_id") or "")
    first_user_event_id = next(
        (
            str(event.get("event_id") or event.get("id"))
            for event in input_package.get("events", [])
            if event.get("actor") == "user" and (event.get("event_id") or event.get("id"))
        ),
        "",
    )
    for index, ref in enumerate(list(candidate["evidence_refs"])):
        if isinstance(ref, str):
            if ref in {"$first_user_event", "$first_event"} and first_user_event_id:
                ref = first_user_event_id
            candidate["evidence_refs"][index] = {"event_id": ref, "episode_id": episode_id}
            repairs.append("evidence_ref_string_to_object")
        elif isinstance(ref, dict):
            if ref.get("event_id") in {"$first_user_event", "$first_event"} and first_user_event_id:
                ref["event_id"] = first_user_event_id
                repairs.append("evidence_ref_placeholder_resolved")
            ref.setdefault("episode_id", episode_id)


def _rename(payload: JsonDict, old: str, new: str, repairs: list[str]) -> None:
    if new not in payload and old in payload:
        payload[new] = payload.pop(old)
        repairs.append(f"{old}_renamed_to_{new}")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_score(key: str) -> float:
    return {
        "confidence": 0.0,
        "importance": 0.5,
        "stability": 0.5,
        "specificity": 0.5,
        "novelty": 1.0,
        "conflict_score": 0.0,
    }[key]


def _noop_candidate_from_text(text: str) -> JsonDict:
    return {
        "operation": "NOOP",
        "memory_type": "procedural_hint",
        "summary": text or "No durable memory candidate.",
        "evidence_refs": [],
        "confidence": 0.0,
        "importance": 0.0,
        "stability": 0.0,
        "specificity": 0.0,
        "novelty": 1.0,
        "conflict_score": 0.0,
        "sensitivity": "low",
        "reason": "non_object_candidate_repaired_to_noop",
    }


def _infer_memory_type(candidate: JsonDict, input_package: JsonDict) -> str:
    operation = str(candidate.get("operation") or "").upper()
    summary = str(candidate.get("summary") or "").lower()
    tags = " ".join(str(tag).lower() for tag in candidate.get("tags", []))
    event_text = " ".join(
        str(event.get("payload", {}).get("content") or event.get("payload", ""))
        for event in input_package.get("events", [])
    ).lower()
    text = summary + " " + tags + " " + event_text
    if operation == "NOOP":
        return "procedural_hint"
    if any(term in text for term in ("closeness", "shared history", "companionship", "relationship")):
        return "relationship_signal"
    if any(term in text for term in ("project sora", "module", "extractor", "architecture", "eval", "prompt")):
        return "project_principle"
    return "preference"


def _should_override_memory_type(candidate: JsonDict, inferred: str) -> bool:
    current = str(candidate.get("memory_type") or "")
    if current == "preference" and inferred in {"project_principle", "relationship_signal"}:
        return True
    if current == "project_principle" and inferred == "relationship_signal":
        return True
    if current == "procedural_hint" and str(candidate.get("operation") or "").upper() != "NOOP":
        return True
    return False
