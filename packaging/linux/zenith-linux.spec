# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ZENITH/OS Linux binary (used by the AppImage).

Bundles the tiny cross-platform launcher (packaging/macos/zenith_app.py — it is
OS-agnostic: start server thread, wait for the port, open a browser) + the stdlib
server, and ships static/ inside the bundle. server.py's frozen-mode path handling
(sys._MEIPASS for statics, ~/.zenith for writable state) makes it self-locating.

Produces a plain onedir dist (a `console` binary named `zenith`), NOT a macOS
.app BUNDLE. REPO_ROOT comes from an env var set by the build script (PyInstaller
does not define __file__ for specs).
"""
import os
from pathlib import Path

REPO_ROOT = Path(os.environ.get("ZENITH_REPO_ROOT", os.getcwd())).resolve()

a = Analysis(
    [str(REPO_ROOT / "packaging" / "macos" / "zenith_app.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[(str(REPO_ROOT / "static"), "static"),
           (str(REPO_ROOT / "defaults"), "defaults"),   # shipped config defaults
           (str(REPO_ROOT / "VERSION"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Optional native deps guarded by lazy imports in server.py; keep them out of
    # the Linux dep graph.
    excludes=["pywinpty", "winpty", "pywhispercpp"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="zenith",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # Linux CLI/daemon binary (no windowed mode)
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
    name="zenith",
)
