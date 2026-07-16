"""Provision a drone's Pi over SSH — push the bridge files directly, no git.

The Pi runs a stock BlueOS (BlueBoat) image. We own ONE dedicated directory
(INSTALL_DIR) that holds nothing but our files — the bridge script, its
requirements, a .env, and a venv. We never write into any BlueRobotics folder.
A deploy copies only the runtime files the bridge needs straight from this
machine via scp; the boat never talks to GitHub and no repo lives on it, so a
deploy ships exactly the code the tech is running.

The systemd unit is *generated* from INSTALL_DIR (see _service_unit) rather than
shipped as a static file, so ExecStart / WorkingDirectory / EnvironmentFile can
never drift from where we actually put the files — the whole point being that
the service reliably runs from our directory.

Design guardrails (all about not damaging a live drone's state over SSH):
  * files land in a staging dir and are mv'd into place, so a dropped
    connection can never leave a half-written script for the service to load.
  * a token update edits ONLY the token line in the Pi's live .env (see
    _replace_token_line); it does not regenerate the file from the template,
    so any Pi-side .env customization survives a token rotation.
  * ssh/scp use accept-new host-key handling + a connect timeout so a new
    drone doesn't hang the deploy on an interactive prompt, and an
    unreachable one fails fast instead of blocking.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resource_path(*parts: str) -> Path:
    """Resolve a bundled resource, whether running from source or as a frozen PyInstaller exe.

    PyInstaller's onefile mode extracts data files to a temp dir at sys._MEIPASS
    at runtime — __file__-relative paths no longer point at the real repo then.
    """
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return base.joinpath(*parts)


# The one directory we own on the Pi. Isolated on purpose: nothing but our own
# files ever goes in here, and we never touch anything else on the boat.
INSTALL_DIR = "/opt/aquadrone"
VENV_DIR = f"{INSTALL_DIR}/.venv"
STAGE_DIR = "/tmp/aquadrone_stage"
SERVICE_NAME = "aquadrone-bridge"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
ENV_PATH = f"{INSTALL_DIR}/.env"
BRIDGE_ENTRY = f"{INSTALL_DIR}/aquadrone_bridge.py"
TOKEN_PREFIX = "CADDIS_API_TOKEN="

# The only files from bridge/ that belong on the boat. Deliberately not the
# whole folder: README, deploy.sh, requirements-dev.txt, the .service file
# (we generate that) are dev-box concerns and would just be clutter to keep
# in sync on the Pi.
BRIDGE_FILES = ("aquadrone_bridge.py", "requirements.txt")

# VERSION ships too, and lives at the repo root rather than under bridge/ — the
# bridge reads it beside itself on the Pi to report firmware_version in
# heartbeats. It gets its own source path for exactly that reason: it cannot be
# folded into BRIDGE_FILES, which resolves names under bridge/.
VERSION_FILE = _resource_path("VERSION")

ENV_EXAMPLE = _resource_path("bridge", ".env.example")

# accept-new trusts a drone's SSH host key the first time we connect (TOFU) but
# still refuses if a *known* key later changes (MITM guard). Without it the very
# first connect to a new Pi hangs forever on the interactive yes/no prompt,
# because subprocess gives ssh no TTY to answer it. ConnectTimeout makes an
# unreachable Pi fail in ~10s instead of blocking on a full TCP timeout.
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

LogFn = Callable[[str], None]


def _service_unit(pi_user: str) -> str:
    """Render the systemd unit with paths derived from INSTALL_DIR, so it always
    matches where we actually installed the files."""
    return (
        "[Unit]\n"
        "Description=Aquadrone Sensor Bridge\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"User={pi_user}\n"
        f"ExecStart={VENV_DIR}/bin/python3 {BRIDGE_ENTRY}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        f"EnvironmentFile={ENV_PATH}\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"WorkingDirectory={INSTALL_DIR}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _run(
    cmd: list[str], log: LogFn, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        # A --windowed exe has no console of its own, so Windows pops a visible
        # one for every child process unless we suppress it per call.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # quiet suppresses the stdout echo only — used when the command's output is
    # the .env contents (which carry the token), so a secret never hits the log.
    if result.stdout and not quiet:
        log(result.stdout.rstrip())
    if result.stderr:
        log(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


SSH_TRANSPORT_FAILURE = 255  # ssh's own exit code for "couldn't connect / auth refused"


def _ssh(
    target: str, remote_cmd: str, log: LogFn, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess:
    result = _run(["ssh", *SSH_OPTS, target, remote_cmd], log, check=False, quiet=quiet)
    # 255 means ssh itself failed, as opposed to the remote command exiting nonzero.
    # Always raise on it, even under check=False: the probe helpers below pass
    # check=False because they expect the *command* to fail, and without this an
    # unreachable Pi or a rejected key would be silently reported as "file missing".
    if result.returncode == SSH_TRANSPORT_FAILURE:
        detail = result.stderr.strip() or "host unreachable or authentication refused"
        raise RuntimeError(f"Cannot SSH to {target}: {detail}")
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}) on {target}: {remote_cmd}")
    return result


def _scp(local_files: list[Path], remote: str, log: LogFn) -> None:
    _run(["scp", *SSH_OPTS, *(str(p) for p in local_files), remote], log)


def _find_local_public_key() -> Path | None:
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        candidate = ssh_dir / name
        if candidate.exists():
            return candidate
    return None


def provision_ssh_key(pi_ip: str, pi_user: str, password: str, log: LogFn) -> None:
    """One-time bootstrap: install this laptop's SSH public key on the Pi via password auth.

    Uses paramiko (not the system ssh/scp binaries) because that's the only way to
    authenticate with a password non-interactively. Every other operation in this
    module uses key-based auth via the system ssh/scp afterward — this just gets
    that key onto the Pi the first time, same as a manual ssh-copy-id would.
    """
    import paramiko  # local import: only needed for this one-time bootstrap path

    pubkey_path = _find_local_public_key()
    if pubkey_path is None:
        raise RuntimeError(
            "No local SSH public key found in ~/.ssh "
            "(looked for id_ed25519.pub, id_rsa.pub, id_ecdsa.pub)"
        )
    pubkey = pubkey_path.read_text().strip()

    log(f"Connecting to {pi_user}@{pi_ip} with password to install {pubkey_path.name}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(pi_ip, username=pi_user, password=password, timeout=10)
        remote_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"grep -qF {shlex.quote(pubkey)} ~/.ssh/authorized_keys || "
            f"echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        _stdin, stdout, stderr = client.exec_command(remote_cmd)
        exit_status = stdout.channel.recv_exit_status()
        out, err = stdout.read().decode(), stderr.read().decode()
        if out:
            log(out.rstrip())
        if err:
            log(err.rstrip())
        if exit_status != 0:
            raise RuntimeError(f"Key install command failed (exit {exit_status})")
        log("SSH key installed — future runs against this Pi won't need the password.")
    finally:
        client.close()


def _replace_token_line(env_text: str, token: str) -> str:
    """Return env_text with the token line set to token, every other line kept
    verbatim. Appends the line if it's somehow absent.

    This is the heart of the non-destructive update: we read the Pi's *live*
    .env and change exactly one line, rather than regenerating the whole file
    from the template (which would reset every other value to its default and
    wipe any Pi-side customization).
    """
    out = []
    replaced = False
    for line in env_text.splitlines():
        if line.startswith(TOKEN_PREFIX):
            out.append(f"{TOKEN_PREFIX}{token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{TOKEN_PREFIX}{token}")
    return "\n".join(out) + "\n"


def _write_temp(text: str, suffix: str) -> Path:
    fd, tmp_name = tempfile.mkstemp(prefix="aquadrone_", suffix=suffix)
    os.close(fd)  # fd unused (we write via Path); close it so unlink() works on Windows
    tmp = Path(tmp_name)
    tmp.write_text(text)
    return tmp


def _build_env_file(token: str) -> Path:
    """Generate a fresh .env from the checked-in template with the token filled
    in. Used only when the Pi has no existing .env to preserve."""
    return _write_temp(_replace_token_line(ENV_EXAMPLE.read_text(), token), ".env")


def is_fresh_install(target: str, log: LogFn) -> bool:
    """A Pi counts as provisioned once the bridge entry script is in place."""
    result = _ssh(
        target,
        f"test -f {BRIDGE_ENTRY} && echo EXISTS || echo MISSING",
        log,
        check=False,
    )
    return "MISSING" in result.stdout


def _remote_env_exists(target: str, log: LogFn) -> bool:
    result = _ssh(
        target,
        f"test -f {ENV_PATH} && echo EXISTS || echo MISSING",
        log,
        check=False,
    )
    return "EXISTS" in result.stdout


def _push_bridge_files(target: str, log: LogFn) -> None:
    """scp the runtime files to a staging dir, then mv into place.

    The mv is the safety property: a connection drop mid-scp leaves junk in
    /tmp, never a truncated aquadrone_bridge.py that systemd would happily
    restart into a crash loop."""
    # VERSION sits beside the bridge script on the Pi, so the bridge can read it
    # as a sibling; its source is the repo root, not bridge/ (see VERSION_FILE).
    names = (*BRIDGE_FILES, "VERSION")
    local_files = [_resource_path("bridge", name) for name in BRIDGE_FILES] + [VERSION_FILE]
    _ssh(target, f"mkdir -p {STAGE_DIR}", log)
    _scp(local_files, f"{target}:{STAGE_DIR}/", log)
    moves = " && ".join(f"mv {STAGE_DIR}/{n} {INSTALL_DIR}/{n}" for n in names)
    _ssh(target, moves, log)


def _install_service(target: str, pi_user: str, log: LogFn) -> None:
    """Generate the systemd unit for this install path, drop it in, enable it."""
    unit = _write_temp(_service_unit(pi_user), ".service")
    try:
        _scp([unit], f"{target}:/tmp/{SERVICE_NAME}.service", log)
    finally:
        unit.unlink(missing_ok=True)
    _ssh(
        target,
        f"sudo cp /tmp/{SERVICE_NAME}.service {SERVICE_PATH} && "
        f"sudo systemctl daemon-reload && sudo systemctl enable {SERVICE_NAME}",
        log,
    )


def _push_token(target: str, token: str, log: LogFn) -> None:
    """Read the Pi's live .env, replace only the token line, write it back
    atomically. We scp to a temp path then mv, so a dropped connection can never
    leave a half-written .env that would crash the bridge on its next restart."""
    current = _ssh(target, f"cat {ENV_PATH}", log, quiet=True).stdout
    tmp = _write_temp(_replace_token_line(current, token), ".env")
    try:
        _scp([tmp], f"{target}:/tmp/aquadrone_env_update", log)
    finally:
        tmp.unlink(missing_ok=True)
    _ssh(target, f"mv /tmp/aquadrone_env_update {ENV_PATH}", log)


def _seed_env(target: str, token: str, log: LogFn) -> None:
    """Place a template-derived .env on a Pi that doesn't have one yet."""
    env_file = _build_env_file(token)
    try:
        _scp([env_file], f"{target}:{ENV_PATH}", log)
    finally:
        env_file.unlink(missing_ok=True)


def update_token(
    pi_ip: str, pi_user: str, token: str, log: LogFn, pi_password: str | None = None
) -> None:
    """Rotate just the API token on an already-provisioned Pi, leaving the rest
    of its .env untouched. Cheaper and safer than a full re-deploy when all a
    tech needs is a fresh token — no file pushes, no pip, no regeneration.

    Takes pi_password for the same reason deploy() does: a second technician's
    laptop has never had its key installed on this boat, and rotating a token is
    exactly the errand that laptop gets sent on.
    """
    target = f"{pi_user}@{pi_ip}"

    if pi_password:
        provision_ssh_key(pi_ip, pi_user, pi_password, log)

    if not _remote_env_exists(target, log):
        raise RuntimeError(
            f"No bridge .env found at {ENV_PATH} on {target}. "
            "Run a full Deploy first to install the bridge."
        )
    log(f"Rotating API token on {target} (rest of .env left as-is)...")
    _push_token(target, token, log)
    _ssh(target, f"sudo systemctl restart {SERVICE_NAME}", log)
    log("Token updated and bridge restarted.")


def deploy(
    pi_ip: str, pi_user: str, drone_name: str, token: str, log: LogFn,
    pi_password: str | None = None,
) -> None:
    target = f"{pi_user}@{pi_ip}"

    if pi_password:
        provision_ssh_key(pi_ip, pi_user, pi_password, log)

    fresh = is_fresh_install(target, log)
    log(f"{'Fresh install' if fresh else 'Update'} detected for {drone_name} at {target}")

    if fresh:
        # Our own dedicated directory — created by us, owned by us, nothing
        # else in it. We never write into any BlueRobotics folder.
        _ssh(
            target,
            f"sudo mkdir -p {INSTALL_DIR} && sudo chown -R {pi_user}:{pi_user} {INSTALL_DIR}",
            log,
        )
        _push_bridge_files(target, log)
        # A bare Raspberry Pi OS image doesn't always have python3-venv installed
        # (ensurepip fails otherwise, and _run raises, aborting the deploy on a
        # half-provisioned Pi). apt-get update first: a stale package index 404s
        # on pruned point releases — hit exactly this on a genuinely fresh Pi.
        _ssh(target, "sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv", log)
        _ssh(
            target,
            f"python3 -m venv {VENV_DIR} && {VENV_DIR}/bin/pip install --quiet --upgrade pip",
            log,
        )
        _ssh(target, f"{VENV_DIR}/bin/pip install --quiet -r {INSTALL_DIR}/requirements.txt", log)
        # Serial port access — the service user must be in dialout for /dev/ttyACM0.
        _ssh(target, f"sudo usermod -aG dialout {pi_user}", log)
        _ssh(target, "sudo mkdir -p /media/sensor_data && sudo chown "
                      f"{pi_user}:{pi_user} /media/sensor_data", log)
        _install_service(target, pi_user, log)
        _seed_env(target, token, log)
        log("Fresh install complete — rebooting Pi (needed for the dialout group change)...")
        _ssh(target, "sudo reboot", log, check=False)
        log("Pi is rebooting. The bridge service is enabled and will start automatically.")
    else:
        # Update path — deliberately non-destructive to Pi-side state: only the
        # runtime files are replaced (staged, then mv'd), the venv is reused,
        # the service unit is refreshed (paths may have changed), and the token
        # is edited in place so the rest of the live .env survives.
        _push_bridge_files(target, log)
        _ssh(target, f"{VENV_DIR}/bin/pip install --quiet -r {INSTALL_DIR}/requirements.txt", log)
        _install_service(target, pi_user, log)
        if _remote_env_exists(target, log):
            _push_token(target, token, log)
        else:
            # Files present but no .env — an earlier install that stopped
            # partway. Seed from the template so the service can start.
            _seed_env(target, token, log)
        _ssh(target, f"sudo systemctl restart {SERVICE_NAME}", log)
        log("Bridge updated and restarted.")

    log(f'Done. Watch logs with: ssh {target} "journalctl -u {SERVICE_NAME} -f"')
