from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.project_context import ContextError, ContextRepository


class ContextGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "docs/context/workstreams").mkdir(parents=True)
        (self.root / "docs/context/decisions").mkdir(parents=True)
        (self.root / "docs/PROJECT_CONTEXT.md").write_text("# Context\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        manifest = {
            "schema_version": "ProjectContextManifest.v1",
            "context_revision": 1,
            "snapshot": "docs/PROJECT_CONTEXT.md",
            "agent_instructions": "AGENTS.md",
            "active_workstream_stale_hours": 72,
            "governed_paths": ["src", "apps/desktop", "scripts"],
            "context_update_paths": ["AGENTS.md", "docs/PROJECT_CONTEXT.md", "docs/context/workstreams", "docs/context/decisions"],
            "architecture_sensitive_paths": ["src/contracts.py"],
            "architecture_update_paths": ["docs/PROJECT_CONTEXT.md", "docs/context/decisions"],
        }
        (self.root / "docs/context/context-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.repository = ContextRepository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def begin(self, identifier: str, path: str, allow_overlap: bool = False) -> None:
        self.repository.begin(identifier, identifier, f"codex:{identifier}", [path], "started", allow_overlap)

    def finish(self, identifier: str, changed: str) -> None:
        self.repository.finish(identifier, "completed", [changed], [], ["tests pass"])

    def enable_ledger(self) -> None:
        (self.root / "docs/context/catalog").mkdir(parents=True)
        (self.root / "docs/context/PROJECT_LEDGER.md").write_text("# Ledger\n", encoding="utf-8")
        (self.root / "docs/PROJECT_CONTEXT.md").write_text("# Context\n\nContext revision: 1\n", encoding="utf-8")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "src/system.py").write_text("", encoding="utf-8")
        (self.root / "tests/test_system.py").write_text("", encoding="utf-8")
        catalogs = {
            "artifacts.json": {
                "schema_version": "ProjectSoraArtifactCatalog.v1",
                "artifacts": [{"id": "design-system", "path": "docs/PROJECT_CONTEXT.md"}],
            },
            "milestones.json": {
                "schema_version": "ProjectSoraMilestoneCatalog.v1",
                "milestones": [{"commit": "a77ea7a", "title": "baseline"}],
            },
            "open-items.json": {
                "schema_version": "ProjectSoraOpenItems.v1",
                "items": [{"id": "open-system", "status": "open"}],
            },
            "systems.json": {
                "schema_version": "ProjectSoraSystemCatalog.v1",
                "systems": [{
                    "id": "system-one",
                    "status": "implemented_baseline",
                    "code_paths": ["src/system.py"],
                    "test_paths": ["tests/test_system.py"],
                    "design_artifact_ids": ["design-system"],
                    "audit_artifact_ids": [],
                    "remaining": [],
                }] * 15,
            },
        }
        for name, payload in catalogs.items():
            (self.root / "docs/context/catalog" / name).write_text(json.dumps(payload), encoding="utf-8")
        manifest_path = self.root / "docs/context/context-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "ledger": "docs/context/PROJECT_LEDGER.md",
            "catalogs": {
                "artifacts": "docs/context/catalog/artifacts.json",
                "milestones": "docs/context/catalog/milestones.json",
                "open_items": "docs/context/catalog/open-items.json",
                "systems": "docs/context/catalog/systems.json",
            },
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_begin_rejects_active_path_overlap(self) -> None:
        self.begin("ws-first", "apps/desktop")
        with self.assertRaises(ContextError):
            self.begin("ws-second", "apps/desktop/src")

    def test_allow_overlap_is_reported_as_warning(self) -> None:
        self.begin("ws-first", "apps/desktop")
        self.begin("ws-second", "apps/desktop/src", allow_overlap=True)
        errors, warnings = self.repository.validate()
        self.assertEqual(errors, [])
        self.assertTrue(any("overlap" in warning for warning in warnings))

    def test_finish_requires_verification_and_seals_record(self) -> None:
        self.begin("ws-finish", "src")
        with self.assertRaises(ContextError):
            self.repository.finish("ws-finish", "done", ["src/module.py"], [], [])
        self.finish("ws-finish", "src/module.py")
        with self.assertRaises(ContextError):
            self.repository.update("ws-finish", "mutate", "active", [], [], [])

    def test_decision_is_create_only(self) -> None:
        arguments = ("adr-context", "Context", "accepted", "Tasks are isolated.", "Use repository records.", [], [], None)
        self.repository.decide(*arguments)
        with self.assertRaises(ContextError):
            self.repository.decide(*arguments)

    def test_impact_requires_completed_workstream(self) -> None:
        errors, _ = self.repository.check_impact(["src/module.py", "docs/PROJECT_CONTEXT.md"])
        self.assertTrue(any("completed workstream" in error for error in errors))
        self.begin("ws-impact", "src")
        self.finish("ws-impact", "src/module.py")
        errors, _ = self.repository.check_impact([
            "src/module.py",
            "docs/PROJECT_CONTEXT.md",
            "docs/context/workstreams/ws-impact.json",
        ])
        self.assertEqual(errors, [])
        errors, _ = self.repository.check_impact([
            "apps/desktop/src/App.tsx",
            "docs/context/workstreams/ws-impact.json",
        ])
        self.assertTrue(any("not covered" in error for error in errors))

    def test_architecture_change_requires_snapshot_or_adr(self) -> None:
        self.begin("ws-architecture", "src/contracts.py")
        self.finish("ws-architecture", "src/contracts.py")
        changes = ["src/contracts.py", "docs/context/workstreams/ws-architecture.json"]
        errors, _ = self.repository.check_impact(changes)
        self.assertTrue(any("architecture-sensitive" in error for error in errors))
        errors, _ = self.repository.check_impact([*changes, "docs/context/decisions/adr-contract.json"])
        self.assertEqual(errors, [])

    def test_ledger_catalogs_are_referentially_validated(self) -> None:
        self.enable_ledger()
        systems_path = self.root / "docs/context/catalog/systems.json"
        systems = json.loads(systems_path.read_text(encoding="utf-8"))
        systems["systems"] = [dict(system, id=f"system-{index}") for index, system in enumerate(systems["systems"])]
        systems_path.write_text(json.dumps(systems), encoding="utf-8")
        errors, warnings = self.repository.validate()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

        systems["systems"][0]["design_artifact_ids"] = ["missing-artifact"]
        systems_path.write_text(json.dumps(systems), encoding="utf-8")
        errors, _ = self.repository.validate()
        self.assertTrue(any("unknown artifact" in error for error in errors))

    def test_ledger_snapshot_revision_must_match_manifest(self) -> None:
        self.enable_ledger()
        (self.root / "docs/PROJECT_CONTEXT.md").write_text("Context revision: 2\n", encoding="utf-8")
        errors, _ = self.repository.validate()
        self.assertTrue(any("revision differs" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
