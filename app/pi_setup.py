"""Push a drone's token/.env to its Pi and install or update the bridge over SSH.

Runs entirely from the technician's laptop against a Pi reachable over SSH —
mirrors bridge/deploy.sh's install/update steps, plus the .env templating
that script leaves as a manual step.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resource_path(*parts: str) -> Path:
    """Resolve a bundled resource, whether running from source or as a frozen PyInstaller exe.

    PyInstaller's onefile mode extracts data files to a temp dir at sys._MEIPASS
    at runtime — __file__-relative paths no longer point at the real repo then.
    """
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return base.joinpath(*parts)


ENV_EXAMPLE = _resource_path("bridge", ".env.example")
SERVICE_FILE = _resource_path("bridge", "aquadrone-bridge.service")
INSTALL_DIR = "/opt/aquadrone"
VENV_DIR = f"{INSTALL_DIR}/.venv"
SERVICE_NAME = "aquadrone-bridge"
DEFAULT_REPO_URL = "https://github.com/caddis-tech/AquadronePicoFirmwareExperimental.git"

LogFn = Callable[[str], None]


def _run(cmd: list[str], log: LogFn, check: bool = True) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        log(result.stdout.rstrip())
    if result.stderr:
        log(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def _ssh(target: str, remote_cmd: str, log: LogFn, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["ssh", target, remote_cmd], log, check=check)


def _repo_origin_url() -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return result.stdout.strip() or DEFAULT_REPO_URL


def _build_env_file(token: str) -> Path:
    lines = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("CADDIS_API_TOKEN="):
            lines.append(f"CADDIS_API_TOKEN={token}")
        else:
            lines.append(line)
    fd, tmp_name = tempfile.mkstemp(prefix="aquadrone_env_", suffix=".env")
    os.close(fd)  # mkstemp's fd is unused — writing via Path below; close it so unlink() works on Windows
    tmp = Path(tmp_name)
    tmp.write_text("\n".join(lines) + "\n")
    return tmp


def is_fresh_install(target: str, log: LogFn) -> bool:
    result = _ssh(
        target,
        f"test -d {INSTALL_DIR}/.git && echo EXISTS || echo MISSING",
        log,
        check=False,
    )
    return "MISSING" in result.stdout


def deploy(pi_ip: str, pi_user: str, drone_name: str, token: str, log: LogFn) -> None:
    target = f"{pi_user}@{pi_ip}"
    fresh = is_fresh_install(target, log)
    log(f"{'Fresh install' if fresh else 'Update'} detected for {drone_name} at {target}")

    if fresh:
        repo_url = _repo_origin_url()
        _ssh(target, f"sudo mkdir -p {INSTALL_DIR} && sudo chown {pi_user}:{pi_user} {INSTALL_DIR}", log)
        _ssh(target, f"git clone {repo_url} {INSTALL_DIR}", log)
        _ssh(target, f"python3 -m venv {VENV_DIR} && {VENV_DIR}/bin/pip install --quiet --upgrade pip", log)
        _ssh(target, f"{VENV_DIR}/bin/pip install --quiet -r {INSTALL_DIR}/bridge/requirements.txt", log)
        _ssh(target, f"sudo usermod -aG dialout {pi_user}", log)
        _ssh(target, "sudo mkdir -p /media/sensor_data && sudo chown "
                      f"{pi_user}:{pi_user} /media/sensor_data", log)
        _run(["scp", str(SERVICE_FILE), f"{target}:/tmp/aquadrone-bridge.service"], log)
        _ssh(
            target,
            "sudo cp /tmp/aquadrone-bridge.service /etc/systemd/system/ && "
            "sudo systemctl daemon-reload && sudo systemctl enable " + SERVICE_NAME,
            log,
        )
    else:
        _ssh(
            target,
            f"cd {INSTALL_DIR} && git pull && {VENV_DIR}/bin/pip install --quiet -r bridge/requirements.txt",
            log,
        )

    env_file = _build_env_file(token)
    try:
        _run(["scp", str(env_file), f"{target}:{INSTALL_DIR}/bridge/.env"], log)
    finally:
        env_file.unlink(missing_ok=True)

    if fresh:
        log("Fresh install complete — rebooting Pi (needed for the dialout group change)...")
        _ssh(target, "sudo reboot", log, check=False)
        log("Pi is rebooting. The bridge service is enabled and will start automatically.")
    else:
        _ssh(target, f"sudo systemctl restart {SERVICE_NAME}", log)
        log("Bridge restarted.")

    log(f'Done. Watch logs with: ssh {target} "journalctl -u {SERVICE_NAME} -f"')
