"""Offline erasure helper for the trusted Host. Never opens a model or runtime.

The Host first drains and stops its Gateway. Confirmation arrives on stdin,
not command arguments. Only the identity-derived directory can be removed.
"""
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from ..paths import user_config_dir, workspace_state_dir
from ..model_provider import ModelProviderSystem, OwnerScope
from .contracts import validate_identifier
from .profile_lock import acquire_profile_lock
from .state import _windows_extended_path


def identity_directory(profile: str, subject: str, agent: str) -> tuple[Path, Path]:
    for value in (profile, subject, agent):
        validate_identifier(value)
    configured = os.environ.get("SORA_GATEWAY_STATE_ROOT")
    root = Path(configured).expanduser().resolve() if configured else workspace_state_dir().resolve() if os.environ.get("SORA_SANDBOX_STATE_DIR") else (user_config_dir() / "runtime").resolve()
    digest = hashlib.sha256("\x1f".join((profile, subject, agent)).encode()).hexdigest()
    target = root / "gateway-identities" / digest
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)() or root not in target.resolve().parents:
        raise ValueError("identity directory is not contained in its configured root")
    return root, target


def erase_stopped_profile(profile: str, subject: str, agent: str, confirmation: dict, *, provider_system=None) -> dict:
    if confirmation.get("confirmed") is not True or confirmation.get("profile_id") != profile or confirmation.get("confirmation") != "erase:" + profile:
        raise ValueError("explicit current-profile confirmation required")
    _, directory = identity_directory(profile, subject, agent)
    with acquire_profile_lock(directory):
        previous = erasure_status(directory)
        if previous and previous.get("profile_id") != profile:
            raise ValueError("erasure receipt belongs to another profile")
        if previous and previous["status"] == "erased" and not directory.exists():
            return previous
        _write_erasure_status(directory, {"schema_version": "ProfileErasure.v1", "status": "pending", "profile_id": profile})
        result = _erase(profile, subject, agent, directory, provider_system)
        result["schema_version"] = "ProfileErasure.v1"
        _write_erasure_status(directory, result)
        return result


def erasure_status(directory: Path) -> dict | None:
    path = directory.with_name(directory.name + ".erasure.json")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("erasure receipt cannot be a link")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != "ProfileErasure.v1"
            or value.get("status") not in {"pending", "erased"}):
        raise ValueError("erasure receipt is invalid; profile remains blocked")
    return value


def require_openable_profile(directory: Path) -> None:
    if erasure_status(directory) is not None:
        raise ValueError("profile erasure is pending or complete; resume it from the desktop privacy settings")


def _write_erasure_status(directory: Path, value: dict) -> None:
    path = directory.with_name(directory.name + ".erasure.json")
    # Validate a previous receipt before replacing it. A corrupt receipt must
    # remain available for recovery instead of silently reopening the profile.
    erasure_status(directory)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _erase(profile, subject, agent, directory, provider_system):
    # Reject junctions/reparse links before removing a tree. No supplied path,
    # export file, backup directory or neighbouring identity is traversed.
    native_directory = Path(_windows_extended_path(str(directory))) if os.name == "nt" else directory
    if native_directory.exists():
        for current, directories, files in os.walk(native_directory, followlinks=False):
            for name in directories + files:
                path = Path(current) / name
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise ValueError("identity directory contains an external link")
    owner = OwnerScope(tenant_id=profile, user_id=subject, agent_id=agent)
    own_provider = provider_system is None
    provider = provider_system or ModelProviderSystem.default(owner)
    if provider.owner != owner:
        raise ValueError("provider owner does not match erased profile")
    try:
        counts = provider.erase_owner_data()
    finally:
        if own_provider:
            provider.close()
    if native_directory.exists():
        shutil.rmtree(native_directory)
    return {"status": "erased", "profile_id": profile, "provider_profiles_erased": counts["provider_profiles"]}
