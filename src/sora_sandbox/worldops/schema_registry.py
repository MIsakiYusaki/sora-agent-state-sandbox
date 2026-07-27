from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contracts import (
    NARRATIVE_CONTRIBUTION_CONTRACT,
    PERSONAL_TIMELINE_MUTATION_CONTRACT,
    TASK_EVIDENCE_CONTRACT,
    USER_CHOICE_INTERPRETATION_CONTRACT,
    WORLD_BUNDLE_MANIFEST_CONTRACT,
    WORLD_CONTENT_BUNDLE_CONTRACT,
    WORLDOPS_EVENT_ENVELOPE_CONTRACT,
    WORLDOPS_FRAME_CONTRACT,
    WORLDOPS_SYNC_SNAPSHOT_CONTRACT,
)


CONTRACT_SCHEMA_FILES = {
    WORLD_CONTENT_BUNDLE_CONTRACT: "WorldContentBundle.v1.schema.json",
    WORLD_BUNDLE_MANIFEST_CONTRACT: "WorldBundleManifest.v1.schema.json",
    WORLDOPS_FRAME_CONTRACT: "WorldOpsFrame.v1.schema.json",
    USER_CHOICE_INTERPRETATION_CONTRACT: "UserChoiceInterpretation.v1.schema.json",
    TASK_EVIDENCE_CONTRACT: "TaskEvidence.v1.schema.json",
    NARRATIVE_CONTRIBUTION_CONTRACT: "NarrativeContributionProposal.v1.schema.json",
    PERSONAL_TIMELINE_MUTATION_CONTRACT: "PersonalTimelineMutation.v1.schema.json",
    WORLDOPS_EVENT_ENVELOPE_CONTRACT: "WorldOpsEventEnvelope.v1.schema.json",
    WORLDOPS_SYNC_SNAPSHOT_CONTRACT: "WorldOpsSyncSnapshot.v1.schema.json",
}


def schema_root() -> Path:
    return Path(__file__).with_name("schemas")


def load_contract_schema(contract: str) -> dict[str, Any]:
    try:
        filename = CONTRACT_SCHEMA_FILES[contract]
    except KeyError as exc:
        raise ValueError(f"unknown WorldOps contract: {contract}") from exc
    payload = json.loads((schema_root() / filename).read_text(encoding="utf-8"))
    if payload.get("$id") != f"https://project-sora.local/schemas/worldops/{filename}":
        raise RuntimeError(f"WorldOps schema has an unexpected $id: {filename}")
    return payload


def validate_schema_registry() -> dict[str, Any]:
    loaded = {contract: load_contract_schema(contract) for contract in CONTRACT_SCHEMA_FILES}
    invalid = [
        contract
        for contract, schema in loaded.items()
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("type") != "object"
    ]
    return {
        "status": "passed" if not invalid else "failed",
        "contracts": sorted(loaded),
        "invalid_contracts": invalid,
    }


def validate_contract_payload(payload: Any, contract: str) -> list[str]:
    errors: list[str] = []
    _validate_node(payload, load_contract_schema(contract), path="$", errors=errors)
    return errors


def _validate_node(value: Any, schema: dict[str, Any], *, path: str, errors: list[str]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        errors.append(f"{path}: expected {expected_type}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required property is missing")
        properties = dict(schema.get("properties") or {})
        for key, child in value.items():
            if key in properties:
                _validate_node(child, dict(properties[key]), path=f"{path}.{key}", errors=errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
    if isinstance(value, list):
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(f"{path}: items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_node(item, item_schema, path=f"{path}[{index}]", errors=errors)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than minLength")
        if "pattern" in schema and not re.fullmatch(str(schema["pattern"]), value):
            errors.append(f"{path}: does not match pattern")


def _matches_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    mapping = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return bool(mapping.get(str(expected), lambda item: True)(value))
