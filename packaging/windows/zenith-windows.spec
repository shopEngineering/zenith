# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ZENITH.exe (Windows, one-file).

Bundles the shared launcher (packaging/macos/zenith_app.py — OS-agnostic despite
its folder) + the stdlib server + static/, and collects pywinpty so the in-app
terminals work on Windows. server.py's sys.frozen path handling resolves static/
from sys._MEIPASS and redirects writable state to %USERPROFILE%\\.zenith.

REPO_ROOT comes from the ZENITH_REPO_ROOT env var (PyInstaller specs have no
__file__); the CI workflow sets it to the checkout root.
"""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

REPO_ROOT = Path(os.environ.get("ZENITH_REPO_ROOT", os.getcwd())).resolve()

# pywinpty ships a native winpty.dll + agent that must be collected, not just the
# Python module — otherwise spawning a terminal in the frozen app fails.
wp_datas, wp_bins, wp_hidden = collect_all("winpty")

a = Analysis(
    [str(REPO_ROOT / "packaging" / "macos" / "zenith_app.py")],
    pathex=[str(REPO_ROOT)],
    binaries=wp_bins,
    datas=[(str(REPO_ROOT / "static"), "static"),
           (str(REPO_ROOT / "defaults"), "defaults"),   # shipped config defaults
           (str(REPO_ROOT / "VERSION"), ".")] + wp_datas,
    hiddenimports=["winpty"] + wp_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pywhispercpp"],   # lazy optional (local Whisper); browser speech is the fallback
    noarchive=False,
)
pyz = PYZ(a.pure)

# One-file: binaries + datas fold into the single EXE (no COLLECT/BUNDLE).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ZENITH",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Console app by default so failures are visible (stdout/stderr). Set
    # ZENITH_CONSOLE=0 to build a windowed (no-console) release once it's proven.
    console=(os.environ.get("ZENITH_CONSOLE", "1") == "1"),
    # Windowed build: don't pop an error dialog that would hang headless CI (and look
    # scary to users) — startup failures are written to ~/.zenith/startup-error.log.
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
