# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ZENITH.app.

Bundles the tiny launcher (zenith_app.py) + the stdlib server (server.py) and
ships the static/ dir inside the bundle (read-only at runtime, resolved via
sys._MEIPASS by server.py's frozen-mode path handling).

REPO_ROOT is resolved from an env var set by build.sh so the spec works no matter
where pyinstaller is invoked from (PyInstaller does not define __file__ for specs).
"""
import os
from pathlib import Path

REPO_ROOT = Path(os.environ.get("ZENITH_REPO_ROOT", os.getcwd())).resolve()
try:
    _VER = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
except Exception:
    _VER = "0.0.0"

a = Analysis(
    [str(REPO_ROOT / "packaging" / "macos" / "zenith_app.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    # Ship static/ + the VERSION file inside the bundle. server.py reads them from
    # sys._MEIPASS when frozen (VERSION → the update check's "current" version).
    datas=[(str(REPO_ROOT / "static"), "static"),
           (str(REPO_ROOT / "defaults"), "defaults"),   # shipped config defaults
           (str(REPO_ROOT / "VERSION"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pywinpty / pywhispercpp are lazy optional imports guarded in server.py;
    # excluding them keeps them out of the frozen dep graph on macOS.
    excludes=["pywinpty", "winpty", "pywhispercpp"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZENITH",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed → produces a .app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ZENITH",
)
app = BUNDLE(
    coll,
    name="ZENITH.app",
    icon=None,
    bundle_identifier="os.zenith.app",
    info_plist={
        "CFBundleName": "ZENITH",
        "CFBundleDisplayName": "ZENITH",
        "CFBundleShortVersionString": _VER,
        # No dock/menu UI of its own — it just launches the server + browser.
        "LSBackgroundOnly": False,
        "NSHighResolutionCapable": True,
    },
)
