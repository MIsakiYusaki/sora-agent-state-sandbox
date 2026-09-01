from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sora_sandbox.app import make_llm
from sora_sandbox.credentials import CredentialError


class AppLlmBootstrapTests(unittest.TestCase):
    def test_unconfigured_desktop_control_plane_stays_ready_without_a_legacy_key(self) -> None:
        system = Mock()
        unconfigured_client = object()
        system.configured_client.return_value = None
        system.llm = unconfigured_client
        manager = Mock()
        manager.get_api_key.side_effect = CredentialError("missing")

        with (
            patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system),
            patch("sora_sandbox.app.CredentialManager", return_value=manager),
        ):
            result = make_llm(
                "deepseek",
                "deepseek-v4-flash",
                True,
                allow_unconfigured=True,
            )

        self.assertIs(result, unconfigured_client)
        manager.get_api_key.assert_called_once_with(prompt_if_missing=False)
        system.bootstrap_legacy_deepseek.assert_not_called()
        system.close.assert_not_called()

    def test_desktop_silently_migrates_an_existing_legacy_credential(self) -> None:
        system = Mock()
        configured_client = object()
        system.configured_client.return_value = None
        system.bootstrap_legacy_deepseek.return_value = configured_client
        manager = Mock()
        manager.get_api_key.return_value = "existing-key"

        with (
            patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system),
            patch("sora_sandbox.app.CredentialManager", return_value=manager),
            patch.dict("os.environ", {"OPENAI_BASE_URL": "https://api.deepseek.com"}),
        ):
            result = make_llm(
                "deepseek",
                "deepseek-v4-flash",
                True,
                allow_unconfigured=True,
            )

        self.assertIs(result, configured_client)
        manager.get_api_key.assert_called_once_with(prompt_if_missing=False)
        system.bootstrap_legacy_deepseek.assert_called_once_with(
            api_key="existing-key",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
        )

    def test_interactive_bootstrap_keeps_the_legacy_credential_path(self) -> None:
        system = Mock()
        configured_client = object()
        system.configured_client.return_value = None
        system.bootstrap_legacy_deepseek.return_value = configured_client
        manager = Mock()
        manager.get_api_key.return_value = "test-key"

        with (
            patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system),
            patch("sora_sandbox.app.CredentialManager", return_value=manager),
            patch.dict("os.environ", {"OPENAI_BASE_URL": "https://api.deepseek.com"}),
        ):
            result = make_llm("deepseek", "deepseek-chat", True)

        self.assertIs(result, configured_client)
        manager.get_api_key.assert_called_once_with(prompt_if_missing=False)
        system.bootstrap_legacy_deepseek.assert_called_once_with(
            api_key="test-key",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
        )

    def test_bootstrap_failure_closes_the_provider_system(self) -> None:
        system = Mock()
        system.configured_client.side_effect = RuntimeError("broken registry")

        with patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system):
            with self.assertRaisesRegex(RuntimeError, "broken registry"):
                make_llm("deepseek", "deepseek-chat", True, allow_unconfigured=True)

        system.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
