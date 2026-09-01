"""Repository-native context coordination for independent Project Sora tasks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


MANIFEST_SCHEMA = "ProjectContextManifest.v1"
WORKSTREAM_SCHEMA = "ProjectContextWorkstream.v1"
DECISION_SCHEMA = "ProjectContextDecision.v1"
SYSTEM_CATALOG_SCHEMA = "ProjectSoraSystemCatalog.v1"
ARTIFACT_CATALOG_SCHEMA = "ProjectSoraArtifactCatalog.v1"
MILESTONE_CATALOG_SCHEMA = "ProjectSoraMilestoneCatalog.v1"
OPEN_ITEMS_SCHEMA = "ProjectSoraOpenItems.v1"
SYSTEM_STATUSES = {"implemented_baseline", "integration_in_progress", "designed", "deferred", "needs_verification"}
WORKSTREAM_STATUSES = {"active", "blocked", "completed", "superseded"}
DECISION_STATUSES = {"proposed", "accepted", "superseded", "rejected"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")


class ContextError(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContextError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContextError(f"timestamp lacks timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ContextError(f"path must be repository-relative: {value!r}")
    return path.as_posix()


def path_contains(path: str, prefix: str) -> bool:
    path = relative_path(path)
    prefix = relative_path(prefix)
    return path == prefix or path.startswith(prefix + "/")


def paths_overlap(left: str, right: str) -> bool:
    return path_contains(left, right) or path_contains(right, left)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContextError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContextError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContextError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any], *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            return
        except FileExistsError as exc:
            raise ContextError(f"record already exists: {path.name}") from exc
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ContextRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.context = self.root / "docs" / "context"
        self.manifest_file = self.context / "context-manifest.json"
        self.workstreams = self.context / "workstreams"
        self.decisions = self.context / "decisions"

    @staticmethod
    def validate_id(value: str, kind: str) -> None:
        if not ID_PATTERN.fullmatch(value):
            raise ContextError(f"invalid {kind} id: {value!r}")

    def manifest(self) -> dict[str, Any]:
        return read_json(self.manifest_file)

    def records(self, directory: Path) -> list[tuple[Path, dict[str, Any]]]:
        return [(path, read_json(path)) for path in sorted(directory.glob("*.json"))] if directory.exists() else []

    def workstream_file(self, workstream_id: str) -> Path:
        self.validate_id(workstream_id, "workstream")
        return self.workstreams / f"{workstream_id}.json"

    def decision_file(self, decision_id: str) -> Path:
        self.validate_id(decision_id, "decision")
        return self.decisions / f"{decision_id}.json"

    def git_head(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip() or None

    def validate_ledger(self, manifest: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
        ledger_value = manifest.get("ledger")
        catalogs_value = manifest.get("catalogs")
        if ledger_value is None and catalogs_value is None:
            return
        if not isinstance(ledger_value, str):
            errors.append("manifest ledger must be a repository-relative path")
            return
        try:
            ledger_path = self.root / relative_path(ledger_value)
        except ContextError as exc:
            errors.append(f"manifest ledger is invalid: {exc}")
            return
        if not ledger_path.is_file():
            errors.append("manifest ledger is missing or unresolved")
        snapshot_value = manifest.get("snapshot")
        if isinstance(snapshot_value, str):
            snapshot_path = self.root / relative_path(snapshot_value)
            if snapshot_path.is_file():
                match = re.search(r"(?im)^Context revision:\s*(\d+)\s*$", snapshot_path.read_text(encoding="utf-8"))
                if not match:
                    errors.append("snapshot does not declare Context revision")
                elif int(match.group(1)) != manifest.get("context_revision"):
                    errors.append("snapshot Context revision differs from manifest context_revision")

        required_catalogs = {"artifacts", "milestones", "open_items", "systems"}
        if not isinstance(catalogs_value, dict) or set(catalogs_value) != required_catalogs:
            errors.append("manifest catalogs must define artifacts, milestones, open_items, and systems")
            return
        payloads: dict[str, dict[str, Any]] = {}
        for name in sorted(required_catalogs):
            value = catalogs_value.get(name)
            if not isinstance(value, str):
                errors.append(f"manifest catalog {name} must be a repository-relative path")
                continue
            try:
                path = self.root / relative_path(value)
                payloads[name] = read_json(path)
            except ContextError as exc:
                errors.append(f"manifest catalog {name}: {exc}")

        expected_schemas = {
            "artifacts": ARTIFACT_CATALOG_SCHEMA,
            "milestones": MILESTONE_CATALOG_SCHEMA,
            "open_items": OPEN_ITEMS_SCHEMA,
            "systems": SYSTEM_CATALOG_SCHEMA,
        }
        for name, expected in expected_schemas.items():
            payload = payloads.get(name)
            if payload is not None and payload.get("schema_version") != expected:
                errors.append(f"catalog {name} schema must be {expected}")

        artifact_ids: set[str] = set()
        artifacts = payloads.get("artifacts", {}).get("artifacts")
        if not isinstance(artifacts, list):
            if "artifacts" in payloads:
                errors.append("artifact catalog artifacts must be an array")
        else:
            for index, artifact in enumerate(artifacts):
                if not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str):
                    errors.append(f"artifact catalog entry {index} lacks an id")
                    continue
                artifact_id = artifact["id"]
                if artifact_id in artifact_ids:
                    errors.append(f"duplicate artifact id: {artifact_id}")
                artifact_ids.add(artifact_id)
                local_path = artifact.get("path")
                if local_path is not None:
                    try:
                        if not isinstance(local_path, str) or not (self.root / relative_path(local_path)).exists():
                            errors.append(f"artifact {artifact_id} has an unresolved local path")
                    except ContextError as exc:
                        errors.append(f"artifact {artifact_id} has an invalid local path: {exc}")

        system_ids: set[str] = set()
        systems = payloads.get("systems", {}).get("systems")
        if not isinstance(systems, list):
            if "systems" in payloads:
                errors.append("system catalog systems must be an array")
        else:
            for index, system in enumerate(systems):
                if not isinstance(system, dict) or not isinstance(system.get("id"), str):
                    errors.append(f"system catalog entry {index} lacks an id")
                    continue
                system_id = system["id"]
                if system_id in system_ids:
                    errors.append(f"duplicate system id: {system_id}")
                system_ids.add(system_id)
                if system.get("status") not in SYSTEM_STATUSES:
                    errors.append(f"system {system_id} has an invalid status")
                for field in ("code_paths", "test_paths", "design_artifact_ids", "remaining"):
                    if not isinstance(system.get(field), list):
                        errors.append(f"system {system_id} field {field} must be an array")
                for artifact_id in [*system.get("design_artifact_ids", []), *system.get("audit_artifact_ids", [])]:
                    if artifact_id not in artifact_ids:
                        errors.append(f"system {system_id} references unknown artifact {artifact_id}")
                for path_value in [*system.get("code_paths", []), *system.get("test_paths", [])]:
                    try:
                        if not isinstance(path_value, str) or not (self.root / relative_path(path_value)).exists():
                            errors.append(f"system {system_id} has an unresolved repository path: {path_value}")
                    except ContextError as exc:
                        errors.append(f"system {system_id} has an invalid repository path: {exc}")

        milestones = payloads.get("milestones", {}).get("milestones")
        if isinstance(milestones, list):
            commits: set[str] = set()
            for milestone in milestones:
                commit = milestone.get("commit") if isinstance(milestone, dict) else None
                if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
                    errors.append(f"milestone has an invalid commit: {commit}")
                elif commit in commits:
                    errors.append(f"duplicate milestone commit: {commit}")
                else:
                    commits.add(commit)
        elif "milestones" in payloads:
            errors.append("milestone catalog milestones must be an array")

        open_items = payloads.get("open_items", {}).get("items")
        if isinstance(open_items, list):
            item_ids: set[str] = set()
            for item in open_items:
                item_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(item_id, str):
                    errors.append("open item lacks an id")
                elif item_id in item_ids:
                    errors.append(f"duplicate open item id: {item_id}")
                else:
                    item_ids.add(item_id)
        elif "open_items" in payloads:
            errors.append("open item catalog items must be an array")

        if systems and len(systems) < 15:
            warnings.append("system catalog appears incomplete")

    def validate(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            manifest = self.manifest()
        except ContextError as exc:
            return [str(exc)], []
        if manifest.get("schema_version") != MANIFEST_SCHEMA:
            errors.append(f"manifest schema must be {MANIFEST_SCHEMA}")
        if not isinstance(manifest.get("context_revision"), int) or manifest["context_revision"] < 1:
            errors.append("context_revision must be a positive integer")
        for field in ("snapshot", "agent_instructions"):
            value = manifest.get(field)
            if not isinstance(value, str) or not (self.root / relative_path(value)).exists():
                errors.append(f"manifest {field} is missing or unresolved")
        list_fields = (
            "governed_paths",
            "context_update_paths",
            "architecture_sensitive_paths",
            "architecture_update_paths",
        )
        for field in list_fields:
            values = manifest.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(item, str) for item in values):
                errors.append(f"manifest {field} must be a non-empty string array")
        self.validate_ledger(manifest, errors, warnings)

        active: list[dict[str, Any]] = []
        stale_before = datetime.now(timezone.utc) - timedelta(
            hours=int(manifest.get("active_workstream_stale_hours", 72))
        )
        for path, record in self.records(self.workstreams):
            prefix = path.relative_to(self.root).as_posix()
            required = {
                "schema_version", "workstream_id", "title", "owner", "status", "paths",
                "started_at_utc", "updated_at_utc", "summary", "events", "verification",
            }
            missing = required - record.keys()
            if missing:
                errors.append(f"{prefix}: missing {', '.join(sorted(missing))}")
                continue
            if record["schema_version"] != WORKSTREAM_SCHEMA or record["status"] not in WORKSTREAM_STATUSES:
                errors.append(f"{prefix}: invalid schema or status")
            if path.stem != record["workstream_id"]:
                errors.append(f"{prefix}: filename and workstream_id differ")
            try:
                self.validate_id(record["workstream_id"], "workstream")
                parse_utc(record["started_at_utc"])
                updated = parse_utc(record["updated_at_utc"])
                normalized = [relative_path(item) for item in record["paths"]]
            except (ContextError, TypeError) as exc:
                errors.append(f"{prefix}: {exc}")
                continue
            if not normalized or not record["title"] or not record["owner"] or not record["events"]:
                errors.append(f"{prefix}: title, owner, paths, and events must be non-empty")
            if record["status"] == "completed":
                if not record["summary"] or not record["verification"] or "completed_at_utc" not in record:
                    errors.append(f"{prefix}: completed record lacks summary, verification, or completed_at_utc")
            if record["status"] == "active":
                active.append(record)
                if updated < stale_before:
                    warnings.append(f"stale active workstream: {record['workstream_id']}")
        for index, left in enumerate(active):
            for right in active[index + 1 :]:
                if any(paths_overlap(a, b) for a in left["paths"] for b in right["paths"]):
                    warnings.append(f"active path overlap: {left['workstream_id']} and {right['workstream_id']}")

        for path, record in self.records(self.decisions):
            prefix = path.relative_to(self.root).as_posix()
            required = {
                "schema_version", "decision_id", "title", "status", "decided_at_utc",
                "context", "decision", "consequences", "references",
            }
            missing = required - record.keys()
            if missing:
                errors.append(f"{prefix}: missing {', '.join(sorted(missing))}")
                continue
            if record["schema_version"] != DECISION_SCHEMA or record["status"] not in DECISION_STATUSES:
                errors.append(f"{prefix}: invalid schema or status")
            if path.stem != record["decision_id"]:
                errors.append(f"{prefix}: filename and decision_id differ")
            try:
                self.validate_id(record["decision_id"], "decision")
                parse_utc(record["decided_at_utc"])
            except ContextError as exc:
                errors.append(f"{prefix}: {exc}")
            if not record["title"] or not record["context"] or not record["decision"]:
                errors.append(f"{prefix}: title, context, and decision must be non-empty")
        return errors, warnings

    def begin(self, workstream_id: str, title: str, owner: str, paths: Sequence[str], summary: str,
              allow_overlap: bool = False) -> dict[str, Any]:
        self.validate_id(workstream_id, "workstream")
        claimed = sorted({relative_path(path) for path in paths})
        if not claimed:
            raise ContextError("at least one --path is required")
        collisions: list[str] = []
        for _, record in self.records(self.workstreams):
            if record.get("status") != "active":
                continue
            for left in claimed:
                for right in record.get("paths", []):
                    if paths_overlap(left, right):
                        collisions.append(f"{record['workstream_id']}: {left} <-> {right}")
        if collisions and not allow_overlap:
            raise ContextError("path overlap detected:\n- " + "\n- ".join(sorted(collisions)))
        if not title.strip() or not owner.strip():
            raise ContextError("title and owner are required")
        timestamp = now_utc()
        record: dict[str, Any] = {
            "schema_version": WORKSTREAM_SCHEMA,
            "workstream_id": workstream_id,
            "title": title.strip(),
            "owner": owner.strip(),
            "status": "active",
            "paths": claimed,
            "started_at_utc": timestamp,
            "updated_at_utc": timestamp,
            "base_head": self.git_head(),
            "summary": summary.strip(),
            "changed_paths": [],
            "decision_ids": [],
            "verification": [],
            "events": [{"at_utc": timestamp, "action": "begin", "summary": summary.strip()}],
        }
        write_json(self.workstream_file(workstream_id), record, create_only=True)
        return record

    def update(self, workstream_id: str, summary: str | None, status: str | None,
               changed_paths: Sequence[str], decision_ids: Sequence[str], verification: Sequence[str]) -> dict[str, Any]:
        path = self.workstream_file(workstream_id)
        record = read_json(path)
        if record["status"] in {"completed", "superseded"}:
            raise ContextError(f"sealed workstream cannot be changed: {workstream_id}")
        if status and status not in {"active", "blocked"}:
            raise ContextError("update status must be active or blocked")
        timestamp = now_utc()
        if summary is not None:
            record["summary"] = summary.strip()
        if status:
            record["status"] = status
        record["changed_paths"] = sorted(set(record.get("changed_paths", [])) | {relative_path(p) for p in changed_paths})
        record["decision_ids"] = sorted(set(record.get("decision_ids", [])) | set(decision_ids))
        record["verification"] = list(dict.fromkeys([*record.get("verification", []), *verification]))
        record["updated_at_utc"] = timestamp
        record["events"].append({"at_utc": timestamp, "action": status or "update", "summary": summary or ""})
        write_json(path, record)
        return record

    def finish(self, workstream_id: str, summary: str, changed_paths: Sequence[str],
               decision_ids: Sequence[str], verification: Sequence[str]) -> dict[str, Any]:
        if not summary.strip() or not verification:
            raise ContextError("finish requires a summary and at least one verification entry")
        record = self.update(workstream_id, summary, "active", changed_paths, decision_ids, verification)
        timestamp = now_utc()
        record.update(status="completed", completed_at_utc=timestamp, updated_at_utc=timestamp, result_head=self.git_head())
        record["events"].append({"at_utc": timestamp, "action": "finish", "summary": summary.strip()})
        write_json(self.workstream_file(workstream_id), record)
        return record

    def decide(self, decision_id: str, title: str, status: str, context: str, decision: str,
               consequences: Sequence[str], references: Sequence[str], workstream_id: str | None) -> dict[str, Any]:
        self.validate_id(decision_id, "decision")
        if status not in DECISION_STATUSES or not title.strip() or not context.strip() or not decision.strip():
            raise ContextError("decision fields are invalid or empty")
        record = {
            "schema_version": DECISION_SCHEMA,
            "decision_id": decision_id,
            "title": title.strip(),
            "status": status,
            "decided_at_utc": now_utc(),
            "context": context.strip(),
            "decision": decision.strip(),
            "consequences": list(consequences),
            "references": list(references),
            "workstream_id": workstream_id,
        }
        write_json(self.decision_file(decision_id), record, create_only=True)
        return record

    def changed_files(self, base_ref: str, include_worktree: bool) -> list[str]:
        commands = [["git", "diff", "--name-only", f"{base_ref}...HEAD"]]
        if include_worktree:
            commands += [["git", "diff", "--name-only"], ["git", "diff", "--name-only", "--cached"]]
        changed: set[str] = set()
        for command in commands:
            try:
                result = subprocess.run(command, cwd=self.root, check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ContextError(f"Git change inspection failed: {' '.join(command)}") from exc
            changed.update(relative_path(line) for line in result.stdout.splitlines() if line.strip())
        return sorted(changed)

    def check_impact(self, changed_files: Sequence[str]) -> tuple[list[str], list[str]]:
        manifest = self.manifest()
        changed = sorted({relative_path(path) for path in changed_files})
        governed = [p for p in changed if any(path_contains(p, prefix) for prefix in manifest["governed_paths"])]
        if not governed:
            return [], []
        errors: list[str] = []
        context_changed = any(
            any(path_contains(path, prefix) for prefix in manifest["context_update_paths"]) for path in changed
        )
        if not context_changed:
            errors.append("governed implementation changed without a context update")
        completed_records: list[dict[str, Any]] = []
        for path in changed:
            if path.startswith("docs/context/workstreams/") and path.endswith(".json"):
                candidate = self.root / path
                if candidate.exists() and read_json(candidate).get("status") == "completed":
                    completed_records.append(read_json(candidate))
        if not completed_records:
            errors.append("governed implementation changes require a completed workstream record")
        else:
            uncovered = []
            for governed_path in governed:
                covered = any(
                    any(path_contains(governed_path, claim) for claim in record.get("paths", []))
                    or governed_path in record.get("changed_paths", [])
                    for record in completed_records
                )
                if not covered:
                    uncovered.append(governed_path)
            if uncovered:
                errors.append(
                    "governed paths are not covered by a completed workstream: " + ", ".join(uncovered)
                )
        architecture_changed = any(
            any(path_contains(path, prefix) for prefix in manifest["architecture_sensitive_paths"]) for path in changed
        )
        architecture_context = any(
            any(path_contains(path, prefix) for prefix in manifest["architecture_update_paths"]) for path in changed
        )
        if architecture_changed and not architecture_context:
            errors.append("architecture-sensitive changes require PROJECT_CONTEXT or an ADR update")
        warnings = ["large governed change set; review workstream scope"] if len(governed) > 100 else []
        return errors, warnings


def print_report(errors: Sequence[str], warnings: Sequence[str]) -> None:
    print("context governance: OK" if not errors else "context governance: FAILED")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Coordinate Project Sora context across tasks.")
    root.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--check", action="store_true")
    status.add_argument("--json", action="store_true")
    commands.add_parser("validate")
    begin = commands.add_parser("begin")
    begin.add_argument("--id", required=True)
    begin.add_argument("--title", required=True)
    begin.add_argument("--owner", required=True)
    begin.add_argument("--path", action="append", required=True)
    begin.add_argument("--summary", default="")
    begin.add_argument("--allow-overlap", action="store_true")
    update = commands.add_parser("update")
    update.add_argument("--id", required=True)
    update.add_argument("--summary")
    update.add_argument("--status", choices=("active", "blocked"))
    update.add_argument("--changed-path", action="append", default=[])
    update.add_argument("--decision-id", action="append", default=[])
    update.add_argument("--verification", action="append", default=[])
    finish = commands.add_parser("finish")
    finish.add_argument("--id", required=True)
    finish.add_argument("--summary", required=True)
    finish.add_argument("--changed-path", action="append", default=[])
    finish.add_argument("--decision-id", action="append", default=[])
    finish.add_argument("--verification", action="append", required=True)
    decision = commands.add_parser("decision")
    decision.add_argument("--id")
    decision.add_argument("--title", required=True)
    decision.add_argument("--status", choices=sorted(DECISION_STATUSES), default="accepted")
    decision.add_argument("--context", required=True)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--consequence", action="append", default=[])
    decision.add_argument("--reference", action="append", default=[])
    decision.add_argument("--workstream-id")
    impact = commands.add_parser("check-impact")
    impact.add_argument("--base-ref")
    impact.add_argument("--include-worktree", action="store_true")
    impact.add_argument("--changed-file", action="append", default=[])
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = ContextRepository(args.root)
    try:
        if args.command in {"status", "validate"}:
            errors, warnings = repository.validate()
            if args.command == "status" and args.json:
                active = [r for _, r in repository.records(repository.workstreams) if r.get("status") == "active"]
                print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings,
                                  "manifest": repository.manifest(), "active_workstreams": active},
                                 ensure_ascii=False, indent=2))
            else:
                print_report(errors, warnings)
                if args.command == "status" and not errors:
                    print(f"context revision: {repository.manifest()['context_revision']}")
                    for record in repository.records(repository.workstreams):
                        if record[1].get("status") == "active":
                            print(f"active: {record[1]['workstream_id']} [{', '.join(record[1]['paths'])}]")
            return 1 if errors and (args.command == "validate" or args.check) else 0
        if args.command == "begin":
            result = repository.begin(args.id, args.title, args.owner, args.path, args.summary, args.allow_overlap)
        elif args.command == "update":
            result = repository.update(args.id, args.summary, args.status, args.changed_path,
                                       args.decision_id, args.verification)
        elif args.command == "finish":
            result = repository.finish(args.id, args.summary, args.changed_path,
                                       args.decision_id, args.verification)
        elif args.command == "decision":
            decision_id = args.id or f"adr-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:10]}"
            result = repository.decide(decision_id, args.title, args.status, args.context, args.decision,
                                       args.consequence, args.reference, args.workstream_id)
        elif args.command == "check-impact":
            changed = list(args.changed_file)
            if args.base_ref:
                changed += repository.changed_files(args.base_ref, args.include_worktree)
            elif args.include_worktree:
                changed += repository.changed_files("HEAD", True)
            if not changed:
                raise ContextError("provide --base-ref, --include-worktree, or --changed-file")
            errors, warnings = repository.check_impact(changed)
            print_report(errors, warnings)
            return 1 if errors else 0
        else:
            raise ContextError(f"unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ContextError as exc:
        print(f"context governance error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
