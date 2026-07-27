from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..memory_schema import new_id
from .contracts import (
    WORLD_CONTENT_BUNDLE_CONTRACT,
    JsonDict,
    WorldBundleManifest,
    WorldOpsContractError,
    WorldOpsSecurityError,
    canonical_json,
    content_hash,
    utc_now,
    validate_content_bundle,
)


class BundleSigner(Protocol):
    key_id: str

    def sign(self, payload: bytes) -> str:
        ...

    def public_key_bytes(self) -> bytes:
        ...


class BundleVerifier(Protocol):
    key_id: str

    def verify(self, payload: bytes, signature: str) -> None:
        ...


class Ed25519BundleSigner:
    algorithm = "Ed25519"

    def __init__(self, private_key: Ed25519PrivateKey, *, key_id: str) -> None:
        self._private_key = private_key
        self.key_id = key_id

    @classmethod
    def generate(cls, *, key_id: str = "worldops.dev.1") -> "Ed25519BundleSigner":
        return cls(Ed25519PrivateKey.generate(), key_id=key_id)

    @classmethod
    def load_or_create(cls, path: str | Path, *, key_id: str = "worldops.local.1") -> "Ed25519BundleSigner":
        target = Path(path)
        if target.exists():
            private_key = serialization.load_pem_private_key(target.read_bytes(), password=None)
            if not isinstance(private_key, Ed25519PrivateKey):
                raise WorldOpsSecurityError("WorldOps signing key is not Ed25519")
            return cls(private_key, key_id=key_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        target.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return cls(private_key, key_id=key_id)

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self._private_key.sign(payload)).decode("ascii")

    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def verifier(self) -> "Ed25519BundleVerifier":
        return Ed25519BundleVerifier(self.public_key_bytes(), key_id=self.key_id)


class Ed25519BundleVerifier:
    algorithm = "Ed25519"

    def __init__(self, public_key: bytes, *, key_id: str) -> None:
        self._public_key = Ed25519PublicKey.from_public_bytes(public_key)
        self.key_id = key_id

    def verify(self, payload: bytes, signature: str) -> None:
        try:
            decoded = base64.b64decode(signature, validate=True)
            self._public_key.verify(decoded, payload)
        except (InvalidSignature, ValueError) as exc:
            raise WorldOpsSecurityError("bundle signature verification failed") from exc


class CanonCompiler:
    """Validates authored content and emits immutable, signed client bundles."""

    def __init__(self, signer: BundleSigner) -> None:
        self.signer = signer

    def compile(
        self,
        source: Mapping[str, Any],
        *,
        canon_version: str,
        release_sequence: int,
        minimum_accepted_sequence: int = 0,
        revocation_list_version: int = 0,
        channel: str = "stable",
        cohort: str = "default",
        expires_in_days: int = 180,
        base_bundle_id: str | None = None,
        rollback_authorization: str | None = None,
    ) -> JsonDict:
        normalized = self.normalize_source(source, canon_version=canon_version)
        report = self.lint(normalized)
        if report["errors"]:
            raise WorldOpsContractError("content compilation failed: " + "; ".join(report["errors"]))
        bundle_hash = content_hash(normalized)
        world_id = normalized["world"]["world_id"]
        bundle_id = f"bundle.{world_id}.{release_sequence}"
        now = datetime.now(timezone.utc)
        manifest = WorldBundleManifest(
            bundle_id=bundle_id,
            world_id=world_id,
            canon_version=canon_version,
            release_sequence=release_sequence,
            minimum_accepted_sequence=minimum_accepted_sequence,
            key_id=self.signer.key_id,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(days=max(1, expires_in_days))).isoformat(),
            revocation_list_version=revocation_list_version,
            content_hash=bundle_hash,
            channel=channel,
            cohort=cohort,
            base_bundle_id=base_bundle_id,
            rollback_authorization=rollback_authorization,
        )
        manifest.validate()
        signature = self.signer.sign(manifest.signing_payload())
        signed = replace(manifest, signature=signature)
        return {
            "manifest": signed.to_dict(),
            "bundle": normalized,
            "compile_report": report,
            "compiled_at": utc_now(),
        }

    def normalize_source(self, source: Mapping[str, Any], *, canon_version: str) -> JsonDict:
        if source.get("contract") == WORLD_CONTENT_BUNDLE_CONTRACT:
            payload = copy.deepcopy(dict(source))
            payload["canon_version"] = canon_version
            return validate_content_bundle(payload)
        if isinstance(source.get("content_pack"), dict):
            return self._from_legacy_seed(source, canon_version=canon_version)
        world = dict(source.get("world") or {})
        payload = {
            "contract": WORLD_CONTENT_BUNDLE_CONTRACT,
            "canon_version": canon_version,
            "world": world,
            "entities": copy.deepcopy(list(source.get("entities") or [])),
            "factions": copy.deepcopy(list(source.get("factions") or [])),
            "relations": copy.deepcopy(list(source.get("relations") or [])),
            "statements": copy.deepcopy(list(source.get("statements") or [])),
            "events": copy.deepcopy(list(source.get("events") or [])),
            "assets": copy.deepcopy(list(source.get("assets") or [])),
            "locales": copy.deepcopy(dict(source.get("locales") or {})),
            "defaults": copy.deepcopy(dict(source.get("defaults") or {})),
        }
        return validate_content_bundle(payload)

    def lint(self, bundle: Mapping[str, Any]) -> JsonDict:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            validate_content_bundle(bundle)
        except WorldOpsContractError as exc:
            errors.append(str(exc))
            return {"status": "failed", "errors": errors, "warnings": warnings, "counts": {}}
        event_ids = {str(item["event_id"]) for item in bundle.get("events") or []}
        entity_ids = {str(item.get("entity_id")) for item in bundle.get("entities") or []}
        faction_ids = {str(item.get("faction_id")) for item in bundle.get("factions") or []}
        for event in bundle.get("events") or []:
            event_id = str(event["event_id"])
            choice_ids = {str(item.get("choice_id")) for item in event.get("choices") or []}
            for dependency in event.get("depends_on") or []:
                if dependency not in event_ids:
                    errors.append(f"{event_id} depends on unknown event {dependency}")
            for entity_id in event.get("entity_ids") or []:
                if entity_ids and entity_id not in entity_ids:
                    errors.append(f"{event_id} references unknown entity {entity_id}")
            for faction_id in event.get("faction_ids") or []:
                if faction_ids and faction_id not in faction_ids:
                    errors.append(f"{event_id} references unknown faction {faction_id}")
            reachable = self._reachable_states(event)
            for state in {str(item.get("from")) for item in event.get("transitions") or []}:
                if state not in reachable:
                    warnings.append(f"{event_id} has unreachable transition source {state}")
            for transition in event.get("transitions") or []:
                for choice_id in transition.get("choice_ids") or []:
                    if str(choice_id) not in choice_ids:
                        errors.append(f"{event_id} transition references unknown choice {choice_id}")
            if not event.get("choices") and not event.get("tasks"):
                warnings.append(f"{event_id} is ambient-only")
        cycle = self._dependency_cycle(bundle.get("events") or [])
        if cycle:
            errors.append("event dependency cycle: " + " -> ".join(cycle))
        return {
            "status": "failed" if errors else "passed",
            "errors": errors,
            "warnings": warnings,
            "counts": {
                "events": len(bundle.get("events") or []),
                "entities": len(bundle.get("entities") or []),
                "factions": len(bundle.get("factions") or []),
                "relations": len(bundle.get("relations") or []),
                "tasks": sum(len(item.get("tasks") or []) for item in bundle.get("events") or []),
            },
        }

    @staticmethod
    def _reachable_states(event: Mapping[str, Any]) -> set[str]:
        reachable = {str(event.get("initial_state") or "dormant")}
        changed = True
        transitions = list(event.get("transitions") or [])
        while changed:
            changed = False
            for item in transitions:
                if item.get("from") in reachable and item.get("to") not in reachable:
                    reachable.add(str(item["to"]))
                    changed = True
        return reachable

    @staticmethod
    def _dependency_cycle(events: list[JsonDict]) -> list[str]:
        graph = {str(item["event_id"]): [str(dep) for dep in item.get("depends_on") or []] for item in events}
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(node: str, path: list[str]) -> list[str]:
            if node in visiting:
                start = path.index(node)
                return path[start:] + [node]
            if node in visited:
                return []
            visiting.add(node)
            for target in graph.get(node, []):
                cycle = walk(target, path + [target])
                if cycle:
                    return cycle
            visiting.remove(node)
            visited.add(node)
            return []

        for node in graph:
            cycle = walk(node, [node])
            if cycle:
                return cycle
        return []

    @staticmethod
    def _from_legacy_seed(source: Mapping[str, Any], *, canon_version: str) -> JsonDict:
        pack = dict(source.get("content_pack") or {})
        world_name = str(pack.get("world_name") or "Sora World")
        world_id = str(pack.get("world_id") or "world.default")
        events: list[JsonDict] = []
        for index, raw in enumerate(pack.get("events") or []):
            event = dict(raw)
            event_id = str(event.get("event_id") or f"event.legacy.{index + 1}")
            micro = dict(event.get("micro_quest") or {})
            interactive = bool(micro.get("enabled"))
            choices = (
                [
                    {"choice_id": "participate", "label": "参与", "aliases": ["愿意", "一起", "可以", "好"]},
                    {"choice_id": "defer", "label": "稍后", "aliases": ["以后", "等会", "晚点"]},
                    {"choice_id": "decline", "label": "拒绝", "aliases": ["不要", "不用", "算了"]},
                ]
                if interactive
                else [
                    {"choice_id": "acknowledge", "label": "知道了", "aliases": ["嗯", "好"]},
                    {"choice_id": "defer", "label": "稍后", "aliases": ["以后", "晚点"]},
                ]
            )
            tasks = (
                [
                    {
                        "task_id": "shared_choice",
                        "title": str(micro.get("goal") or "与 Sora 完成一次共同选择"),
                        "completion": {"op": "evidence_count", "evidence_type": "user_choice", "min": 1},
                    }
                ]
                if interactive
                else []
            )
            transitions = (
                [
                    {"from": "dormant", "to": "hinted", "trigger": "eligible"},
                    {"from": "hinted", "to": "invited", "trigger": "surface_selected"},
                    {"from": "invited", "to": "active", "trigger": "choice", "choice_ids": ["participate"]},
                    {"from": "invited", "to": "hinted", "trigger": "choice", "choice_ids": ["defer"]},
                    {"from": "invited", "to": "archived", "trigger": "choice", "choice_ids": ["decline"]},
                    {"from": "active", "to": "completed", "trigger": "tasks_complete"},
                    {"from": "completed", "to": "archived", "trigger": "retention"},
                ]
                if interactive
                else [
                    {"from": "dormant", "to": "hinted", "trigger": "eligible"},
                    {"from": "hinted", "to": "invited", "trigger": "surface_selected"},
                    {"from": "invited", "to": "archived", "trigger": "choice", "choice_ids": ["acknowledge"]},
                    {"from": "invited", "to": "hinted", "trigger": "choice", "choice_ids": ["defer"]},
                ]
            )
            events.append(
                {
                    "event_id": event_id,
                    "definition_version": int(event.get("definition_version", 1)),
                    "title": str(event.get("title") or event_id),
                    "summary": str(event.get("one_sentence") or event.get("summary") or event_id),
                    "initial_state": "dormant",
                    "expression_default": str(event.get("expression_default") or "L1"),
                    "active_mention_allowed": bool(event.get("active_mention_allowed", True)),
                    "urgency": int(event.get("urgency", 0)),
                    "tone": str(event.get("tone") or "neutral"),
                    "faction_ids": [str(item) for item in event.get("related_factions") or []],
                    "entity_ids": [str(item) for item in event.get("affected_personas") or []],
                    "eligibility": {"op": "always"},
                    "choices": choices,
                    "tasks": tasks,
                    "transitions": transitions,
                    "memory_policy": dict(
                        event.get("memory_policy")
                        or {"write_only_if_user_participates": True}
                    ),
                    "contribution_policy": {"eligible": interactive, "default_anonymous": True},
                }
            )
        payload = {
            "contract": WORLD_CONTENT_BUNDLE_CONTRACT,
            "canon_version": canon_version,
            "world": {"world_id": world_id, "name": world_name},
            "entities": [],
            "factions": [],
            "relations": [],
            "statements": [],
            "events": events,
            "assets": [],
            "locales": {},
            "defaults": dict(pack.get("default_rules") or {}),
            "migration": {"source": "legacy_worldops_seed", "compiled_id": new_id("migration")},
        }
        return validate_content_bundle(payload)


def verify_compiled_bundle(compiled: Mapping[str, Any], verifier: BundleVerifier) -> WorldBundleManifest:
    manifest = WorldBundleManifest.from_dict(dict(compiled.get("manifest") or {}))
    if manifest.key_id != verifier.key_id:
        raise WorldOpsSecurityError("bundle key_id does not match verifier")
    bundle = validate_content_bundle(dict(compiled.get("bundle") or {}))
    if content_hash(bundle) != manifest.content_hash:
        raise WorldOpsSecurityError("bundle content hash mismatch")
    if not manifest.signature:
        raise WorldOpsSecurityError("bundle is unsigned")
    verifier.verify(manifest.signing_payload(), manifest.signature)
    return manifest
