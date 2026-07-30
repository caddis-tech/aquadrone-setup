# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the technician-facing Drone Setup app.

Build with:  pyinstaller DroneSetup.spec   (output lands in dist/DroneSetup.exe)

This lives in version control on purpose. The previous spec was only ever
generated into the gitignored build/ dir with absolute paths baked in, so the
exe a tech runs could not be reproduced from a clean checkout — and it still
referenced bridge/aquadrone-bridge.service, which af523a1 deleted (the systemd
unit is generated per-install now), so a rebuild failed outright.
"""
import os

ROOT = SPECPATH  # noqa: F821 — injected by PyInstaller

# The bridge itself ships as a BlueOS Extension (Docker image via GHCR) —
# this app never touches a Pi's filesystem. extension_settings.py only reads
# two local files to build the install settings a tech pastes into BlueOS's
# control panel: the root VERSION (the Docker tag) and bridge/Dockerfile
# (the source of truth for the container's permissions LABEL).
datas = [
    (os.path.join(ROOT, "VERSION"), "."),
    (os.path.join(ROOT, "bridge", "Dockerfile"), "bridge"),
]

# hiddenimports stays empty on purpose, including for cryptography, which the version
# picker uses to check the signature on the published firmware list. PyInstaller ships
# no hook for it, so it looked like it would need one -- it doesn't. Verified against
# 6.17 / cryptography 47: the analysis picks up the 33 pure-Python modules (ed25519
# among them) and cryptography\hazmat\bindings\_rust.pyd, which is where the actual
# signature verification happens.
a = Analysis(
    [os.path.join(ROOT, "app", "main.py")],
    pathex=[os.path.join(ROOT, "app")],  # main.py imports flash/extension_settings as siblings
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DroneSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
