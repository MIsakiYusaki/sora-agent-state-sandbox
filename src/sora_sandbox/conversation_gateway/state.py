from __future__ import annotations

import contextlib
import hashlib
import json
import ntpath
import os
import threading
import uuid
from pathlib import Path
from typing import Iterator

from ..state import JsonDict, ensure_state_migrations, seed_state
from .contracts import validate_identifier


def _windows_extended_path(path: str) -> str:
    """Keep the same file identity without requiring global long-path policy."""
    if path.startswith("\\\\?\\"):
        return path
    absolute = ntpath.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


class ProfileStateRepository:
    """Atomic per-profile compatibility state with serialized mutation."""

    def __init__(self, root: Path, *, profile_scope: str) -> None:
        # The hashed identity directories plus atomic temporary filename can
        # exceed MAX_PATH even under the default AppData root. Use extended
        # paths for all I/O, not just the write, while preserving legacy names.
        self.root = Path(_windows_extended_path(str(root))) if os.name == "nt" else Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_scope = validate_identifier(profile_scope)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    @contextlib.contextmanager
    def turn_state(
        self,
        profile_id: str,
        conversation_id: str,
        *,
        user_id: str,
        agent_id: str,
        pair_id: str,
    ) -> Iterator[JsonDict]:
        self._require_scope(profile_id)
        lock = self._lock_for(profile_id)
        with lock:
            state = self.load(profile_id)
            session = state.setdefault("session", {})
            session["session_id"] = conversation_id
            session["user_id"] = user_id
            session["sora_id"] = agent_id
            session["profile_id"] = profile_id
            session["pair_id"] = pair_id
            yield state
            self.save(profile_id, state)

    def load(self, profile_id: str) -> JsonDict:
        self._require_scope(profile_id)
        path = self._path(profile_id)
        if not path.exists():
            return seed_state()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("profile state must be a JSON object")
        if ensure_state_migrations(payload):
            self.save(profile_id, payload)
        return payload

    def save(self, profile_id: str, state: JsonDict) -> None:
        self._require_scope(profile_id)
        path = self._path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        serialized = json.dumps(state, ensure_ascii=False, indent=2)
        try:
            temp.write_text(serialized, encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _lock_for(self, profile_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(profile_id, threading.RLock())

    def _path(self, profile_id: str) -> Path:
        self._require_scope(profile_id)
        digest = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
        return self.root / f"profile-{digest}.json"

    def _require_scope(self, profile_id: str) -> None:
        if profile_id != self.profile_scope:
            raise PermissionError("profile state access is outside the repository scope")
