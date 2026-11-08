"""Privacy deletion tests only use temporary directories and in-memory secrets."""
import os
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sora_sandbox.conversation_gateway.privacy_cli import erase_stopped_profile, identity_directory, erasure_status, require_openable_profile
from sora_sandbox.conversation_gateway.profile_lock import acquire_profile_lock
from sora_sandbox.model_provider import ModelProviderSystem, ProviderRegistry, InMemorySecretStore, OwnerScope, ProviderKind, ProviderProtocol, SecretMaterial, EndpointPolicy


class DesktopPrivacyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.env=patch.dict(os.environ,{"SORA_GATEWAY_STATE_ROOT":str(self.root / "runtime")});self.env.start();self.addCleanup(self.env.stop)
        self.secrets=InMemorySecretStore();self.owner=OwnerScope(tenant_id="space-test",user_id="user-test",agent_id="sora")
        self.system=ModelProviderSystem(ProviderRegistry(self.root/"providers.db"),self.secrets,self.owner,endpoint_policy=EndpointPolicy(resolver=lambda *_:("93.184.216.34",)))
        self.addCleanup(self.system.close)
        self.profile=self.system.create_profile(profile_id="pp_own",display_name="private-owner-label",provider_kind=ProviderKind.OPENAI_COMPATIBLE,protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,base_url="https://provider.example/v1",custom_endpoint=True)
        self.system.bind_credential(self.profile.profile_id,SecretMaterial(api_key="fixture-secret-only"))
        _,self.directory=identity_directory("space-test","user-test","sora");self.directory.mkdir(parents=True);(self.directory/"private-state.json").write_text("fixture-private-data")
        _,self.neighbour=identity_directory("other-space","user-test","sora");self.neighbour.mkdir();(self.neighbour/"retained.json").write_text("other-fixture")
        self.confirmation={"confirmed":True,"profile_id":"space-test","confirmation":"erase:space-test"}

    def erase(self, **changes):
        return erase_stopped_profile("space-test","user-test","sora",{**self.confirmation,**changes},provider_system=self.system)

    def test_delete_requires_exact_confirmation_and_exclusive_stopped_identity(self):
        for changes in ({"confirmed":False},{"confirmed":1},{"profile_id":"other-space"},{"confirmation":"erase:other-space"}):
            with self.assertRaises(ValueError): self.erase(**changes)
        with acquire_profile_lock(self.directory):
            with self.assertRaises(ValueError): self.erase()
        self.assertTrue(self.directory.exists())
        self.assertEqual(len(self.system.registry.list_profiles(self.owner)),1)

    def test_erasure_removes_only_owner_records_and_can_resume_missing_vault_secret(self):
        other=ModelProviderSystem(ProviderRegistry(self.root/"providers.db"),self.secrets,OwnerScope(tenant_id="other-space",user_id="user-test",agent_id="sora"),endpoint_policy=EndpointPolicy(resolver=lambda *_:("93.184.216.34",)))
        self.addCleanup(other.close)
        other_profile=other.create_profile(profile_id="pp_other",display_name="other-owner",provider_kind=ProviderKind.OPENAI_COMPATIBLE,protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,base_url="https://provider.example/v1",custom_endpoint=True)
        own=self.system.registry.get_profile(self.profile.profile_id)
        self.secrets.delete_if_present(own.credential_ref,own.profile_id)
        result=self.erase()
        self.assertEqual(result["status"],"erased");self.assertFalse(self.directory.exists());self.assertTrue(self.neighbour.exists())
        self.assertEqual(self.system.registry.list_profiles(self.owner,include_deleted=True),[])
        self.assertEqual(other.registry.get_profile(other_profile.profile_id).display_name,"other-owner")
        self.assertEqual(self.erase(),result)
        with self.assertRaises(ValueError):require_openable_profile(self.directory)

    def test_credential_failure_does_not_report_success_or_delete_runtime_directory(self):
        with patch.object(self.secrets,"delete_if_present",side_effect=RuntimeError("injected-vault-failure")):
            with self.assertRaises(RuntimeError):self.erase()
        self.assertTrue(self.directory.exists());self.assertEqual(len(self.system.registry.list_profiles(self.owner)),1)
        self.assertEqual(erasure_status(self.directory)["status"],"pending")
        with self.assertRaises(ValueError):require_openable_profile(self.directory)
        self.assertEqual(self.erase()["status"],"erased")

    def test_lost_final_receipt_can_resume_without_reopening_partially_erased_profile(self):
        from sora_sandbox.conversation_gateway import privacy_cli
        write = privacy_cli._write_erasure_status
        def fail_final(directory, value):
            if value["status"] == "erased":raise OSError("injected final receipt failure")
            return write(directory, value)
        with patch.object(privacy_cli,"_write_erasure_status",side_effect=fail_final):
            with self.assertRaises(OSError):self.erase()
        self.assertFalse(self.directory.exists())
        self.assertEqual(erasure_status(self.directory)["status"],"pending")
        with self.assertRaises(ValueError):require_openable_profile(self.directory)
        self.assertEqual(self.erase()["status"],"erased")
        self.assertTrue(self.neighbour.exists())

    def test_corrupt_erasure_marker_is_preserved_and_blocks_startup(self):
        marker=self.directory.with_name(self.directory.name+".erasure.json")
        marker.write_text("corrupt",encoding="utf-8")
        with self.assertRaises(ValueError):require_openable_profile(self.directory)
        with self.assertRaises(ValueError):self.erase()
        self.assertEqual(marker.read_text(),"corrupt")
        self.assertTrue(self.directory.exists())

    def test_gateway_entry_point_rejects_pending_or_completed_erasure_before_model_setup(self):
        from sora_sandbox.conversation_gateway.cli import main
        self.erase()
        with patch("sora_sandbox.conversation_gateway.cli.make_llm") as models, patch("sys.stderr",new_callable=io.StringIO):
            result=main(["--profile-id","space-test","--subject-id","user-test","--agent-id","sora","--client-instance-id","client-test","--origin","tauri://localhost"])
        self.assertEqual(result,5)
        models.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_wrong_secret_owner_cannot_erase_another_profiles_key(self):
        reference=self.secrets.put("pp_other",SecretMaterial(api_key="other-owner-secret"))
        with self.assertRaises(Exception):self.secrets.delete_if_present(reference,"pp_own")
        self.assertTrue(self.secrets.exists(reference,"pp_other"))

    def test_erasure_removes_extended_length_profile_files(self):
        from sora_sandbox.conversation_gateway.state import _windows_extended_path
        nested = self.directory / ("long-profile-directory-" * 5) / ("saved-profile-" * 8 + ".json")
        native = Path(_windows_extended_path(str(nested))) if os.name == "nt" else nested
        native.parent.mkdir(parents=True)
        native.write_text("isolated-long-path-fixture", encoding="utf-8")
        self.assertGreater(len(str(nested.resolve())), 260)
        self.assertEqual(self.erase()["status"], "erased")
        self.assertFalse(native.exists())
        self.assertTrue(self.neighbour.exists())

    def test_offline_failure_receipt_excludes_error_messages_and_full_paths(self):
        from sora_sandbox.conversation_gateway.cli import main
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(self.confirmation))), patch("sys.stdout", output), patch(
            "sora_sandbox.conversation_gateway.privacy_cli.erase_stopped_profile",
            side_effect=PermissionError("private-path fixture-secret-only"),
        ):
            result = main(["--erase-stopped-profile", "--profile-id", "space-test", "--subject-id", "user-test"])
        self.assertEqual(result, 3)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "erase_unconfirmed")
        self.assertEqual(receipt["error_type"], "PermissionError")
        self.assertNotIn("private-path", output.getvalue())
        self.assertNotIn("fixture-secret-only", output.getvalue())
