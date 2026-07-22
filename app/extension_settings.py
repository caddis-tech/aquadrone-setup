"""Build BlueOS Extension install settings for the Aquadrone Bridge.

The bridge ships as a BlueOS Extension — a Docker image that BlueOS's
Extensions Manager (Kraken) pulls, runs, restarts, and updates (see
bridge/BLUEOS_EXTENSION.md). Provisioning it, including its API token, happens
entirely through BlueOS's own control panel: paste the Extension Identifier,
image, and tag into the Create Extension dialog, along with a settings JSON
that carries the container's permissions and its Env. Kraken persists that
Env across restarts and updates — no SSH, no sudo, nothing touches the Pi's
filesystem directly.

This module's only job is building that settings JSON with the current
VERSION and a real token filled in, so a tech never hand-types (and can't
typo) the permissions block that decides whether the bridge can see the Pico
at all — see bridge/BLUEOS_EXTENSION.md's own warning that a malformed
permissions block installs cleanly and then just looks like a hardware fault.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fields a tech pastes into BlueOS -> Extensions -> INSTALLED -> +,
# alongside the generated settings JSON. See bridge/BLUEOS_EXTENSION.md.
EXTENSION_IDENTIFIER = "caddis.aquadrone-bridge"
EXTENSION_NAME = "Aquadrone Bridge"
DOCKER_IMAGE = "ghcr.io/caddis-tech/aquadrone-bridge"


def _resource_path(*parts: str) -> Path:
    """Resolve a bundled resource, whether running from source or as a frozen PyInstaller exe.

    PyInstaller's onefile mode extracts data files to a temp dir at sys._MEIPASS
    at runtime — __file__-relative paths no longer point at the real repo then.
    """
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return base.joinpath(*parts)


def read_version() -> str:
    """The current firmware/bridge/image version — also the Docker tag to install.

    BLUEOS_EXTENSION.md pins the tag to a real version rather than `latest`:
    `latest` makes it impossible to tell from the UI which build a boat runs.
    """
    return _resource_path("VERSION").read_text().strip()


def _joined_dockerfile() -> str:
    """bridge/Dockerfile with its backslash-continued LABEL lines folded back
    into one line each, so the permissions regex below can match it whole."""
    text = _resource_path("bridge", "Dockerfile").read_text()
    return re.sub(r"\\\s*\n\s*", "", text)


def read_docker_permissions() -> dict:
    """The exact container permissions Kraken applies on install, parsed
    straight from the Dockerfile's own LABEL rather than duplicated as a
    separate constant here — so this can never drift from what actually
    ships. tests/test_release_consistency.py asserts the same LABEL is valid
    and grants the access the bridge needs; this reuses that single source.
    """
    m = re.search(r"^LABEL permissions='(.*)'$", _joined_dockerfile(), re.M)
    if not m:
        raise RuntimeError("bridge/Dockerfile has no LABEL permissions")
    permissions: dict = json.loads(m.group(1))
    return permissions


def build_install_settings(token: str) -> str:
    """The ready-to-paste JSON for BlueOS's Create/Edit Extension dialog.

    This is 'Option A' from bridge/BLUEOS_EXTENSION.md: the real permissions
    block plus an Env array carrying the token. Providing it this way needs
    no SSH, and Kraken persists it across restarts and updates. Env is listed
    first (matching BLUEOS_EXTENSION.md's own example) so a tech skimming the
    pasted JSON sees the one boat-specific field before the boilerplate.
    """
    permissions = read_docker_permissions()
    settings = {
        "Env": [
            f"CADDIS_API_TOKEN={token}",
            "CADDIS_API_URL=https://api.caddistech.com",
        ],
        **permissions,
    }
    return json.dumps(settings, separators=(",", ":"))
