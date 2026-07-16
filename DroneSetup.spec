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

# Everything pi_setup.py ships to the Pi. The systemd unit is deliberately absent:
# it's generated per-install by _service_unit() so its paths can never drift from
# INSTALL_DIR.
datas = [
    (os.path.join(ROOT, "bridge", ".env.example"), "bridge"),
    (os.path.join(ROOT, "bridge", "aquadrone_bridge.py"), "bridge"),
    (os.path.join(ROOT, "bridge", "requirements.txt"), "bridge"),
    # VERSION lives at the root, not under bridge/. pi_setup ships it next to the
    # bridge script on the Pi so the bridge can report firmware_version; without
    # it bundled here, _resource_path("VERSION") fails in the frozen exe.
    (os.path.join(ROOT, "VERSION"), "."),
]

a = Analysis(
    [os.path.join(ROOT, "app", "main.py")],
    pathex=[os.path.join(ROOT, "app")],  # main.py imports flash/pi_setup as siblings
    binaries=[],
    datas=datas,
    # paramiko is imported lazily inside provision_ssh_key(), and its crypto
    # backend loads dynamically — name both so the frozen exe can still do the
    # one-time SSH key bootstrap on a fresh Pi.
    hiddenimports=["paramiko", "cryptography"],
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
