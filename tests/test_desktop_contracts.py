from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from pydantic import TypeAdapter


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.conversation_gateway.contracts import GatewayEvent, TurnSubmit
from sora_sandbox.desktop_contracts import ShellError
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


if __name__ == "__main__":
    unittest.main()
