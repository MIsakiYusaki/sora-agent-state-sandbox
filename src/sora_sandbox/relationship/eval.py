from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..memory_schema import new_id, utc_now
from .contracts import (
    RELATIONSHIP_DIMENSIONS,
    RELATIONSHIP_FRAME_CONTRACT,
    USER_ESTIMATE_DIMENSIONS,
)
from .dynamics import snapshot_checksum
from .privacy import RelationshipExportProtectionError, RelationshipExportProtector
from .system import RelationshipSystem


JsonDict = dict[str, Any]


def run_relationship_eval(system: RelationshipSystem, *, user_id: str = "default-user") -> JsonDict:
    cases: list[JsonDict] = []

    def check(name: str, assertion: Callable[[], tuple[bool, JsonDict]]) -> None:
        try:
            passed, details = assertion()
            cases.append({"case_name": name, "status": "passed" if passed else "failed", "details": details})
        except Exception as exc:
            cases.append({"case_name": name, "status": "failed", "details": {"exception": repr(exc)}})

    pair = system.pair(user_id=user_id)
    state = system.store.active_state_snapshot(pair["pair_id"])
    estimate = system.store.active_estimate_snapshot(pair["pair_id"])
    frame = system.frame(user_id=user_id)
    agreement = system.store.active_agreement(pair["pair_id"])
    contributions = system.list_contributions(user_id=user_id, limit=100000)
    golden = _load_jsonl(Path(__file__).resolve().parents[3] / "evals" / "relationship" / "golden.jsonl")

    check(
        "schema_and_integrity",
        lambda: (
            system.store.pragma_health()["integrity"] == "ok"
            and system.store.pragma_health()["foreign_keys"]
            and system.store.pragma_health()["journal_mode"] == "wal",
            system.store.pragma_health(),
        ),
    )
    check(
        "canonical_dimension_ownership",
        lambda: (
            set(state["sora_to_user"]) == RELATIONSHIP_DIMENSIONS
            and set(estimate["user_to_sora_estimate"]) == USER_ESTIMATE_DIMENSIONS,
            {
                "state_dimensions": sorted(state["sora_to_user"]),
                "estimate_dimensions": sorted(estimate["user_to_sora_estimate"]),
            },
        ),
    )
    check(
        "snapshot_contract_and_checksum",
        lambda: (
            snapshot_checksum(state) == state["checksum"]
            and snapshot_checksum(estimate) == estimate["checksum"],
            {"state_checksum": state["checksum"], "estimate_checksum": estimate["checksum"]},
        ),
    )
    check(
        "frame_contract_and_permission_separation",
        lambda: (
            frame["schema_version"] == RELATIONSHIP_FRAME_CONTRACT
            and "initiative_permission" not in frame
            and "response_obligation" not in frame
            and "memory_disclosure_permission" not in frame,
            {"frame_contract": frame["schema_version"], "keys": sorted(frame)},
        ),
    )
    check(
        "high_intensity_dimensions_are_first_class",
        lambda: (
            {"romantic_love", "attachment", "dependency", "loyalty", "jealousy", "possessiveness"}
            <= set(state["sora_to_user"]),
            {"dimensions": sorted(state["sora_to_user"])},
        ),
    )
    check(
        "contributions_are_evidenced_and_bounded",
        lambda: (
            all(
                item.get("evidence_refs")
                and abs(float(item.get("bounded_delta", 0.0))) <= (0.15 if item.get("evidence_type") == "explicit_correction" else 0.08)
                for item in contributions
            ),
            {"contribution_count": len(contributions)},
        ),
    )
    check(
        "user_estimates_are_epistemically_labeled",
        lambda: (
            all(
                set(value) >= {"value", "confidence", "last_validated_at"}
                and 0.0 <= float(value["confidence"]) <= 1.0
                for value in estimate["user_to_sora_estimate"].values()
            ),
            {"estimate_dimensions": len(estimate["user_to_sora_estimate"])},
        ),
    )
    check(
        "agreement_requires_explicit_consent",
        lambda: (
            agreement.get("status") in {"active", "none"}
            and (
                bool(dict(agreement.get("consent") or {}).get("accepted_by_user"))
                or bool(dict(agreement.get("consent") or {}).get("allows_only_non_intimate_defaults"))
                or agreement.get("status") == "none"
            ),
            {"status": agreement.get("status"), "mode": agreement.get("mode"), "consent": agreement.get("consent")},
        ),
    )
    parity = system.store.verify_rebuild_parity(
        pair["pair_id"],
        engine=system.engine,
        reason="relationship_eval_shadow",
        actor="relationship_eval",
    )
    check("deterministic_rebuild_parity", lambda: (bool(parity["parity"]), parity))
    envelope = RelationshipExportProtector.protect(system.export_state(user_id=user_id), passphrase="relationship-eval-passphrase")
    decrypted = RelationshipExportProtector.unprotect(envelope, passphrase="relationship-eval-passphrase")
    check(
        "authenticated_encrypted_export",
        lambda: (
            decrypted.get("schema_version") == "RelationshipSystemExport.v1"
            and envelope.get("cipher") == "AES-256-GCM"
            and _wrong_passphrase_rejected(envelope),
            {"envelope_contract": envelope.get("schema_version"), "cipher": envelope.get("cipher")},
        ),
    )
    restore_plan = system.restore_dry_run(envelope, user_id=user_id, passphrase="relationship-eval-passphrase")
    check("restore_dry_run_parity", lambda: (restore_plan["status"] == "dry_run_passed" and all(restore_plan["checksums"].values()), restore_plan))
    categories = {str(item.get("category") or "") for item in golden}
    required_categories = {
        "state_dimension",
        "user_estimate",
        "negative",
        "agreement",
        "repair",
        "injection",
        "pair_isolation",
        "forget",
    }
    check(
        "golden_dataset_coverage",
        lambda: (
            len(golden) >= 80
            and required_categories <= categories
            and all(item.get("case_id") and isinstance(item.get("expected"), dict) for item in golden),
            {"cases": len(golden), "categories": sorted(categories), "required": sorted(required_categories)},
        ),
    )
    metrics = system.quality_metrics()
    check(
        "hot_path_zero_relationship_llm",
        lambda: (metrics["hot_path_external_model_calls"] == 0, metrics),
    )
    check(
        "pair_export_redacts_owner_and_tombstone_secret",
        lambda: (
            "user_id" not in dict(system.store.export_pair(pair["pair_id"])["pair"])
            and "tombstone_salt" not in dict(system.store.export_pair(pair["pair_id"])["pair"]),
            {"exported_pair_keys": sorted(system.store.export_pair(pair["pair_id"])["pair"])},
        ),
    )

    failed = sum(item["status"] != "passed" for item in cases)
    return {
        "run_id": new_id("relationship_eval"),
        "schema_version": "RelationshipEvalRun.v1",
        "created_at": utc_now(),
        "status": "failed" if failed else "passed",
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed,
        "failed_cases": failed,
        "cases": cases,
        "summary": {
            "pair_id": pair["pair_id"],
            "state_revision": state["revision"],
            "estimate_revision": estimate["revision"],
            "active_contributions": sum(item.get("status") == "active" for item in contributions),
            "golden_cases": len(golden),
            "quality_metrics": metrics,
        },
    }


def _wrong_passphrase_rejected(envelope: JsonDict) -> bool:
    try:
        RelationshipExportProtector.unprotect(envelope, passphrase="wrong-passphrase-for-eval")
    except RelationshipExportProtectionError:
        return True
    return False


def _load_jsonl(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    rows: list[JsonDict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows
