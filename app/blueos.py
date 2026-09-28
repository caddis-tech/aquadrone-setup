"""Reach one of a BlueOS vehicle's HTTP services.

BlueOS runs several APIs on one board, each on its own port: Kraken, the
Extensions Manager, on 9134, and the autopilot manager that owns the MAVLink
endpoints on 8000. What a tech types to reach the boat is the same either way,
and so is what happens when the boat does not answer, so both live here rather
than in whichever client happened to need them first.

Deliberately knows nothing about extensions or endpoints. Each service client
adds its own vocabulary on top and turns BlueOsError into its own error type, so
a caller's `except` clause names the thing it was actually doing.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# Where to look for a vehicle when the tech has not typed an address.
#
# blueos.local is BlueOS's mDNS name, resolved by Windows' own resolver, so this
# needs no zeroconf dependency in an exe that PyInstaller has to bundle.
# 192.168.2.2 is BlueOS's fixed address on the tether interface and is what
# answers when a laptop is plugged straight into the boat.
DEFAULT_HOSTS = ("blueos.local", "192.168.2.2")

# A probe has to fail fast: discovery walks the list above and a tech is waiting.
DISCOVERY_TIMEOUT = 4.0
REQUEST_TIMEOUT = 15.0

# Enough of any response to quote in an error. Kraken's install streams Docker
# pull progress, which is unbounded and worth none of the tech's memory.
MAX_RESPONSE_BYTES = 64 * 1024
_CHUNK_BYTES = 16 * 1024

# A hostname, an IPv4 address, or either with an explicit :port. Deliberately not
# a URL: accepting a path would turn a typo into a request against something
# other than the boat, and there is nothing a tech needs to reach here but a host.
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(:\d{1,5})?")


class BlueOsError(Exception):
    """Anything that stopped us reaching a service on a vehicle."""


def _cleaned(address: str) -> str:
    """A typed-in address with the noise removed, or an error saying why not.

    A scheme and a trailing slash are forgiven because techs paste them, but a
    path is refused rather than silently dropped: an address with a path in it
    means the tech is looking at something other than the boat's API, and a
    request built from it would go somewhere we cannot vouch for.
    """
    cleaned = address.strip()
    for scheme in ("http://", "https://"):
        if cleaned.lower().startswith(scheme):
            cleaned = cleaned[len(scheme):]
    cleaned = cleaned.rstrip("/")

    if not cleaned:
        raise BlueOsError("No vehicle address given. Try blueos.local, or the boat's IP.")
    if not _HOST.fullmatch(cleaned):
        raise BlueOsError(
            f"{address!r} is not a plain address. Enter just the hostname or IP, with an "
            "optional :port."
        )
    return cleaned


def vehicle_host(address: str, default_port: int) -> str:
    """An address with a port on it: "blueos.local" -> "blueos.local:9134".

    A port the tech typed always wins. Each service has its own default, so the
    same box is reached at :9134 for Kraken and :8000 for the autopilot manager.
    """
    cleaned = _cleaned(address)
    return cleaned if ":" in cleaned else f"{cleaned}:{default_port}"


def host_only(address: str) -> str:
    """Just the host, with any scheme and port stripped off.

    For telling a tech where to point something that is not one of these APIs:
    QGroundControl dials the boat on its own port, not on whichever one this tool
    happened to be talking to.
    """
    return _cleaned(address).partition(":")[0]


def read_bounded(response: Any) -> str:
    """Drain a response, keeping only the first MAX_RESPONSE_BYTES of it.

    Kraken's install endpoint streams Docker pull progress for as long as the pull
    takes. Closing the socket once we had enough would abort the pull rather than
    just truncating our copy of it, so the rest is read and thrown away.
    """
    kept = bytearray()
    while True:
        chunk = response.read(_CHUNK_BYTES)
        if not chunk:
            break
        if len(kept) < MAX_RESPONSE_BYTES:
            kept.extend(chunk[: MAX_RESPONSE_BYTES - len(kept)])
    return kept.decode("utf-8", errors="replace")


def _unchanged(text: str) -> str:
    return text


def request(
    url: str,
    method: str = "GET",
    body: Any = None,
    timeout: float = REQUEST_TIMEOUT,
    scrub: Callable[[str], str] = _unchanged,
) -> str:
    """One call against a vehicle. Returns the response text.

    `scrub` is applied to the server's own words before they reach an exception
    message, not after. Kraken echoes rejected request bodies back, and one of
    those can carry a boat's API token; scrubbing at the point the message is
    built means the unredacted string never exists to be chained into a traceback.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is None and method == "POST":
        # A POST with no body still needs one, or urllib sends a GET.
        data = b""
    headers = {"User-Agent": "DroneSetup", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    prepared = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(prepared, timeout=timeout) as response:  # noqa: S310
            return read_bounded(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        raise BlueOsError(
            scrub(f"{method} {url} answered {exc.code} {exc.reason}. {detail}".strip())
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BlueOsError(
            scrub(
                f"Could not reach BlueOS at {urllib.parse.urlsplit(url).netloc}: {exc}. Check "
                "that the laptop is on the boat's network and that BlueOS is up."
            )
        ) from exc
