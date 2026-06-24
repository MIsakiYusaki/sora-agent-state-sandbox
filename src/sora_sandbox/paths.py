from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
SEEDS_DIR = PACKAGE_ROOT / "seeds"
PROMPTS_DIR = PACKAGE_ROOT / "prompts"


def user_config_dir() -> Path:
    override = os.environ.get("SORA_SANDBOX_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ProjectSora" / "agent-state-sandbox"

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "project-sora" / "agent-state-sandbox"

    return Path.home() / ".config" / "project-sora" / "agent-state-sandbox"


def workspace_state_dir() -> Path:
    override = os.environ.get("SORA_SANDBOX_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return PACKAGE_ROOT / ".sandbox"
