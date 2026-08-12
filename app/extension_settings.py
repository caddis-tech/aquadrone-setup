"""What a correctly installed MANTA Link looks like, read from the image itself.

MANTA Link ships as a BlueOS Extension: a Docker image that BlueOS's Extensions
Manager (Kraken) pulls, runs, restarts, and updates. Everything a correct install
needs (the identifier, the image, the tag, the container's permissions) is read
out of the vendored Dockerfile here rather than duplicated as constants, so it
cannot drift from what actually ships.

That matters because **Kraken does not fall back to the image's own `permissions`
LABEL**. Installing with Custom settings left empty stores `{}`, and the container
then starts with no `/dev` bind, no host networking, and no persistent volume.
MANTA Link comes up reporting "no API token configured", "connection refused" to
mavlink2rest, and "no Pico present": three symptoms that look like unrelated bugs
and name nothing.

provisioning.py consumes this twice over: it compares a boat against these values
to decide whether it is current and correctly configured, and it sends
build_extension_source() to Kraken to make it so. build_install_settings() remains
for the manual path, a tech pasting into BlueOS's own dialog, which is still how a
token reaches a boat.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
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

# Named rather than spelled out at each use: the audit has to recognise these in
# a boat's stored Env, and a typo there reads as "the boat has no token".
API_TOKEN_VAR = "CADDIS_API_TOKEN"
API_URL_VAR = "CADDIS_API_URL"


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


def merge_env(existing: Iterable[str] = ()) -> list[str]:
    """The Env array to install with: everything the boat already had, plus prod.

    Reinstalling is how a boat's permissions get repaired, and Kraken has no
    partial update: the body sent becomes the extension's whole configuration.
    So an Env variable this does not carry forward is one the container loses,
    and on a boat provisioned by hand that includes its API token. Nothing here
    reads, logs, or displays those values; they are moved from one field of the
    boat's own record back into the next one.

    `CADDIS_API_URL` is the exception: it is forced to production rather than
    preserved. A boat left pointed at a bench endpoint uploads nowhere anyone is
    looking, and it reports perfect health while doing it.
    """
    kept = []
    for entry in existing:
        if not isinstance(entry, str) or "=" not in entry:
            raise ValueError(
                f"The boat's Env holds {entry!r}, which is not a NAME=VALUE string. "
                "Fix it in BlueOS before reinstalling, or the container gets it back."
            )
        if entry.split("=", 1)[0] != API_URL_VAR:
            kept.append(entry)
    return [*kept, f"{API_URL_VAR}={API_URL}"]


def _settings_block(env: list[str]) -> dict:
    """The permissions the image declares, plus an Env array. Env is listed first
    so a tech skimming the JSON sees the boat-specific fields before boilerplate."""
    return {"Env": env, **read_docker_permissions()}


def build_install_settings(token: str) -> str:
    """The ready-to-paste JSON for BlueOS's Create/Edit Extension dialog.

    Still the only way a token reaches a boat: delivering it over the API is a
    separate piece of work with a security constraint this path does not carry.
    MANTA Link resolves its token from `$AQUADRONE_DATA_DIR/.env` first and the
    environment second, so Env works without touching the filesystem.
    """
    return json.dumps(
        _settings_block(merge_env([f"{API_TOKEN_VAR}={token}"])), separators=(",", ":")
    )


def build_extension_source(existing_env: Iterable[str] = ()) -> dict:
    """The body for Kraken's POST /v1.0/extension/install. No token in it.

    Both permission fields are filled, deliberately. Kraken keeps the manifest's
    block in `permissions` and the operator's in `user_permissions`, and starts
    the container from `user_permissions` whenever that is set to anything at all,
    `{}` included. Writing the real HostConfig into both means the empty-permissions
    failure cannot happen here even if someone later clears the custom settings by
    hand.

    They are not identical: only `user_permissions` carries Env. The boat's token
    can end up there, and there is no reason to write a credential into a second
    field that nothing reads.
    """
    return {
        "identifier": EXTENSION_IDENTIFIER,
        "name": EXTENSION_NAME,
        "docker": DOCKER_IMAGE,
        "tag": read_extension_version(),
        "enabled": True,
        "permissions": json.dumps(read_docker_permissions(), separators=(",", ":")),
        "user_permissions": json.dumps(
            _settings_block(merge_env(existing_env)), separators=(",", ":")
        ),
    }
