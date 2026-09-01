from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(os.environ["SORA_REPO_ROOT"]).resolve()
TARGET_TRIPLE = os.environ["SORA_TARGET_TRIPLE"]

datas = [
    (str(ROOT / "seeds"), "seeds"),
    (str(ROOT / "prompts"), "prompts"),
]
datas += collect_data_files("sora_sandbox", include_py_files=False)

hidden_imports = sorted(
    set(
        collect_submodules("sora_sandbox")
        + collect_submodules("keyring.backends")
        + collect_submodules("uvicorn")
    )
)

a = Analysis(
    [str(ROOT / "scripts" / "desktop_gateway_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"sora-gateway-{TARGET_TRIPLE}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
