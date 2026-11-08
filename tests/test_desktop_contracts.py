from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from pydantic import TypeAdapter, ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.conversation_gateway.contracts import GatewayEvent, TurnSubmit
from sora_sandbox.desktop_contracts import DesktopDebugSnapshot, ShellError
from sora_sandbox.model_provider.admin import CreateProfileRequest


class DesktopContractTests(unittest.TestCase):
    def fixture(self, name: str) -> str:
        path = ROOT / "contracts" / "desktop" / "golden" / name
        return path.read_text(encoding="utf-8")

    def test_golden_payloads_validate_strictly(self) -> None:
        cases = (
            ("turn-submit.json", TypeAdapter(TurnSubmit)),
            ("gateway-event.json", TypeAdapter(GatewayEvent)),
            ("shell-error.json", TypeAdapter(ShellError)),
            ("provider-create.json", TypeAdapter(CreateProfileRequest)),
        )
        for name, adapter in cases:
            with self.subTest(name=name):
                adapter.validate_json(self.fixture(name), strict=True)

    def test_checked_in_contracts_have_no_drift(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_desktop_contracts.py"), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_desktop_debug_snapshot_contract_is_strict_and_closed(self) -> None:
        payload = {
            "schema_version": "DesktopDebugSnapshot.v1",
            "collected_at_ms": 1,
            "profile_id_hash": "0123456789abcdef",
            "conversation_id": "conversation-debug",
            "gateway": {"status": "ready"},
            "timeline": {"status": "empty", "turns": []},
            "routes": {"status": "available", "bindings": []},
            "systems": [
                {
                    "schema_version": "DesktopDebugSubsystem.v1",
                    "subsystem_id": "memory",
                    "label": "记忆系统",
                    "status": "active",
                    "summary": "已运行",
                    "details": {"pending": 0},
                }
            ],
            "memory": {"status": "ok"},
            "async_work": {"pending": 0, "failed": 0, "dead_letter": 0},
            "quality": {"status": "ok"},
            "bridges": [
                {
                    "schema_version": "DesktopDebugBridge.v1",
                    "bridge_id": "debug_snapshot",
                    "label": "脱敏运行快照",
                    "status": "available",
                    "summary": "已接入",
                }
            ],
        }
        adapter = TypeAdapter(DesktopDebugSnapshot)

        snapshot = adapter.validate_python(payload, strict=True)
        self.assertEqual(snapshot.profile_id_hash, "0123456789abcdef")

        with self.assertRaises(ValidationError):
            adapter.validate_python(payload | {"unexpected": True}, strict=True)
        with self.assertRaises(ValidationError):
            adapter.validate_python(payload | {"collected_at_ms": "1"}, strict=True)
        with self.assertRaises(ValidationError):
            adapter.validate_python(
                payload
                | {
                    "systems": [
                        {
                            **payload["systems"][0],
                            "status": "online",
                        }
                    ]
                },
                strict=True,
            )


if __name__ == "__main__":
    unittest.main()
