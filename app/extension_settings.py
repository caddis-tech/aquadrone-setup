"""Build BlueOS Extension install settings for MANTA Link.

MANTA Link ships as a BlueOS Extension: a Docker image that BlueOS's Extensions
Manager (Kraken) pulls, runs, restarts, and updates. Provisioning it, including
its API token, happens entirely through BlueOS's own control panel. A tech pastes
the Extension Identifier, image, and tag into the Create Extension dialog, along
with a settings JSON carrying the container's permissions and its Env. Kraken
persists that Env across restarts and updates, so nothing here needs SSH, sudo,
or any write to the Pi's filesystem.

This module's only job is building that settings JSON with the right tag and a
real token filled in, because **Kraken does not fall back to the image's own
`permissions` LABEL**. Installing with Custom settings left empty stores `{}`,
and the container then starts with no `/dev` bind, no host networking, and no
persistent volume. MANTA Link comes up reporting "no API token configured",
"connection refused" to mavlink2rest, and "no Pico present": three symptoms that
look like unrelated bugs and name nothing. Generating the block is what stops a
tech ever leaving it empty.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fields a tech pastes into BlueOS -> Extensions -> INSTALLED -> +,
# alongside the generated settings JSON.
EXTENSION_IDENTIFIER = "caddis.manta-link"
EXTENSION_NAME = "MANTA Link"
DOCKER_IMAGE = "ghcr.io/caddis-tech/manta-link"

# Production. MANTA Link defaults to this when unset, but an unset value is also
# how a bench run silently uploads to production, so it is stated rather than
# assumed.
API_URL = "https://api.caddistech.com"


def _resource_path(*parts: str) -> Path:
    """Resolve a bundled resource, whether running from source or as a frozen PyInstaller exe.

    PyInstaller's onefile mode extracts data files to a temp dir at sys._MEIPASS
    at runtime, so __file__-relative paths no longer point at the real repo then.
    """
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return base.joinpath(*parts)


def _joined_dockerfile() -> str:
    """manta-link/Dockerfile with its backslash-continued LABEL lines folded back
    into one line each, so the regexes below can match them whole."""
    text = _resource_path("manta-link", "Dockerfile").read_text()
    return re.sub(r"\\\s*\n\s*", "", text)


def read_extension_version() -> str:
    """The Docker tag to install, read from the image's own `LABEL version`.

    Deliberately not the repo's VERSION file, which is the exe's version and
    moves independently: this tool released at 1.2.0 while MANTA Link was at
    0.9.0, and pinning the tag to the exe would send a tech to install a tag that
    does not exist. The Dockerfile is the same single source already used for
    permissions below.
    """
    # \r? because git checks this out CRLF on Windows unless .gitattributes says
    # otherwise, and an anchored $ then matches after the \r rather than after
    # the quote. The failure is a bare "has no LABEL version" that says nothing
    # about line endings.
    match = re.search(r'^LABEL version="([^"]+)"\r?$', _joined_dockerfile(), re.M)
    if not match:
        raise RuntimeError("manta-link/Dockerfile has no LABEL version")
    return match.group(1)


def read_docker_permissions() -> dict:
    """The exact container permissions Kraken applies on install, parsed straight
    from the Dockerfile's own LABEL rather than duplicated as a constant here, so
    this can never drift from what actually ships.
    """
    match = re.search(r"^LABEL permissions='(.*)'\r?$", _joined_dockerfile(), re.M)
    if not match:
        raise RuntimeError("manta-link/Dockerfile has no LABEL permissions")
    permissions: dict = json.loads(match.group(1))
    return permissions


def build_install_settings(token: str) -> str:
    """The ready-to-paste JSON for BlueOS's Create/Edit Extension dialog.

    The real permissions block plus an Env array carrying the token. MANTA Link
    resolves its token from `$AQUADRONE_DATA_DIR/.env` first and the environment
    second, so Env works without touching the filesystem. Env is listed first so
    a tech skimming the pasted JSON sees the one boat-specific field before the
    boilerplate.
    """
    permissions = read_docker_permissions()
    settings = {
        "Env": [
            f"CADDIS_API_TOKEN={token}",
            f"CADDIS_API_URL={API_URL}",
        ],
        **permissions,
    }
    return json.dumps(settings, separators=(",", ":"))
