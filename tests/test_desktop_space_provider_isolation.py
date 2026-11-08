"""Explicit isolated replacements only; no OS credentials or model requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sora_sandbox.app import make_llm
from sora_sandbox.model_provider import OwnerScope
from sora_sandbox.conversation_gateway.cli import build_parser

class DesktopSpaceProviderIsolationTests(unittest.TestCase):
    def test_managed_empty_space_does_not_read_or_copy_global_legacy_credentials(self):
        system = SimpleNamespace(configured_client=Mock(return_value=None), llm=object(), close=Mock(), bootstrap_legacy_deepseek=Mock())
        owner = OwnerScope(tenant_id="space_fixture", user_id="user_fixture", agent_id="sora")
        with patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system) as build, patch("sora_sandbox.app.CredentialManager") as manager:
            self.assertIs(make_llm("deepseek", "test-model", True, owner=owner, allow_unconfigured=True, migrate_legacy=False), system.llm)
        build.assert_called_once_with(owner)
        manager.assert_not_called()
        system.bootstrap_legacy_deepseek.assert_not_called()

    def test_explicit_managed_space_keeps_its_own_existing_configuration(self):
        configured = object()
        system = SimpleNamespace(configured_client=Mock(return_value=configured), llm=object(), close=Mock())
        with patch("sora_sandbox.app.ModelProviderSystem.default", return_value=system), patch("sora_sandbox.app.CredentialManager") as manager:
            self.assertIs(make_llm("deepseek", "test-model", True, allow_unconfigured=True, migrate_legacy=False), configured)
        manager.assert_not_called()
        self.assertTrue(build_parser().parse_args(["--no-legacy-migration"]).no_legacy_migration)
