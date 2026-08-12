"""Talk to Kraken, BlueOS's Extensions Manager, over its HTTP API.

Everything here is shaped by one fact about Kraken: it stores **two** permission
blocks per extension and starts the container from the second one. `permissions`
is what the extension's manifest entry declared; `user_permissions` is what the
operator typed into the Custom settings box. MANTA Link is a private image with
no BlueOS manifest entry at all, so on a working boat `permissions` is literally
`"{}"` and the entire configuration lives in `user_permissions`. A boat installed
with that box left empty stores `"{}"` there too, and the container comes up with
no /dev bind, no host networking and no persistent volume.

Split the way firmware_manifest.py is: parsing, host handling and redaction are
pure and unit-tested with no network at all, and the urllib wrappers on top stay
thin.

No credential is ever written into a URL. The only values that travel as query
parameters are the extension identifier and a version tag, both public strings.
A token that comes *back* from a boat inside an Env array is redacted before it
can reach a log line, an error message, or the screen.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# Kraken's own port. BlueOS proxies it on 80 as well, but the direct port is what
# the issue's API table was verified against and it skips the proxy entirely.
KRAKEN_PORT = 9134
API_PREFIX = "/v1.0"

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
# Install pulls an arm/v7 image onto the boat, sometimes over cellular, and the
# response does not come back until that finishes.
INSTALL_TIMEOUT = 600.0

# Enough of any response to quote in an error. Kraken's install streams Docker
# pull progress, which is unbounded and worth none of the tech's memory.
MAX_RESPONSE_BYTES = 64 * 1024
_CHUNK_BYTES = 16 * 1024

# A hostname, an IPv4 address, or either with an explicit :port. Deliberately not
# a URL: accepting a path would turn a typo into a request against something
# other than the boat, and there is nothing a tech needs to reach here but a host.
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(:\d{1,5})?")

# Environment variable names whose values must never be displayed. Matched as a
# substring so CADDIS_API_TOKEN and any future FOO_SECRET are both covered;
# over-redacting costs a tech nothing, under-redacting puts a boat's credential
# in a log panel they will screenshot.
_SECRET_NAME = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|KEY", re.I)

# The same idea for free text: Kraken echoes request bodies back in some errors,
# and a reinstall body carries whatever Env the boat already had. Both NAME=value
# and JSON's "name": "value" are covered, including the backslash-escaped form
# FastAPI produces when it quotes a rejected body inside its own JSON error.
_SECRET_ASSIGNMENT = re.compile(
    r"([A-Za-z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|KEY)[A-Za-z0-9_]*"
    r"\\?\"?\s*[=:]\s*\\?\"?)([^\"'\\\s,\]}]+)",
    re.I,
)

REDACTED = "<redacted>"


class KrakenError(Exception):
    """Anything that stopped us reading or changing a boat's extensions.

    One type for unreachable hosts, HTTP errors and malformed payloads, the way
    ManifestError covers the whole firmware path: from the caller's side they all
    mean the same thing, which is that this boat cannot be provisioned right now
    and the message says why.
    """


def is_secret_name(name: str) -> bool:
    """Whether an environment variable's *name* means its value must not be shown."""
    return _SECRET_NAME.search(name) is not None


def scrub_secrets(text: str) -> str:
    """Replace the value of any NAME=VALUE or "NAME": "VALUE" pair whose name looks
    like a credential. For free text: server errors, echoed request bodies, logs."""
    return _SECRET_ASSIGNMENT.sub(rf"\1{REDACTED}", text)


def redact_env(settings: dict) -> dict:
    """A copy of a settings block safe to display, with secret Env values masked.

    Returns a new dict rather than editing the caller's: the un-redacted block is
    what gets sent back to the boat on a reinstall, and quietly blanking it in
    place would send `<redacted>` as the boat's real token.
    """
    env = settings.get("Env")
    if not isinstance(env, list):
        return dict(settings)

    masked = []
    for entry in env:
        name, sep, _ = str(entry).partition("=")
        masked.append(f"{name}={REDACTED}" if sep and is_secret_name(name) else str(entry))
    return {**settings, "Env": masked}


@dataclass(frozen=True)
class InstalledExtension:
    """One entry from GET /v1.0/installed_extensions.

    Both permission blocks are kept as the raw strings Kraken returned. Parsing
    them lazily means a neighbouring extension with a malformed block cannot stop
    us auditing MANTA Link, and it keeps the failure attached to the extension it
    belongs to.
    """

    identifier: str
    name: str
    docker: str
    tag: str
    enabled: bool
    permissions_raw: str
    user_permissions_raw: str

    def effective_settings(self) -> dict:
        """The block Kraken actually hands Docker, parsed. Raises KrakenError.

        `user_permissions` wins whenever it is set *at all*, including when it is
        set to nothing: `"{}"` is a non-empty string, so Kraken uses it and the
        container gets no permissions. That is the whole failure this module
        exists to catch, so an empty object here is deliberately not treated as
        "unset, fall back": it returns {} and the audit calls it a fault.

        An empty *string* is genuinely unset, and falls back to the manifest's
        block the way every stock BlueOS extension relies on.
        """
        raw = self.user_permissions_raw.strip() or self.permissions_raw.strip()
        if not raw:
            return {}
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KrakenError(
                f"{self.identifier} has permissions stored that are not valid JSON: {exc}. "
                "Reinstall to replace them with the block the image ships with."
            ) from exc
        if not isinstance(settings, dict):
            raise KrakenError(
                f"{self.identifier} has permissions stored as "
                f"{type(settings).__name__}, not an object."
            )
        return settings

    def redacted_settings(self) -> dict:
        """effective_settings() with any credential masked, for showing a tech."""
        return redact_env(self.effective_settings())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KrakenError(f"Kraken returned something we do not understand: {message}")


def _extension_from_entry(entry: Any, index: int) -> InstalledExtension:
    _require(isinstance(entry, dict), f"entry {index} is a {type(entry).__name__}, not an object")

    identifier = entry.get("identifier")
    _require(
        isinstance(identifier, str) and identifier != "",
        f"entry {index} has no identifier",
    )
    # Everything below is required by Kraken's own schema. Defaulting rather than
    # demanding would let a boat running an unexpected BlueOS quietly audit as
    # healthy, which is the one outcome this module may not produce.
    for field in ("tag", "name", "docker"):
        _require(
            isinstance(entry.get(field), str),
            f"{identifier} has no {field}",
        )
    _require(isinstance(entry.get("enabled"), bool), f"{identifier} has no enabled flag")

    return InstalledExtension(
        identifier=identifier,
        name=entry["name"],
        docker=entry["docker"],
        tag=entry["tag"],
        enabled=entry["enabled"],
        # Kraken returns "" for an extension whose Custom settings were never
        # touched, and null is what its schema allows; both mean the same thing.
        permissions_raw=str(entry.get("permissions") or ""),
        user_permissions_raw=str(entry.get("user_permissions") or ""),
    )


def parse_installed_extensions(payload: Any) -> tuple[InstalledExtension, ...]:
    """Validated extensions from a decoded /installed_extensions body. Pure."""
    _require(isinstance(payload, list), f"top level is a {type(payload).__name__}, not a list")
    return tuple(_extension_from_entry(entry, i) for i, entry in enumerate(payload))


def find_extension(
    extensions: tuple[InstalledExtension, ...], identifier: str
) -> InstalledExtension | None:
    """The one extension with this identifier, or None. Pure."""
    for extension in extensions:
        if extension.identifier == identifier:
            return extension
    return None


def base_url(host: str) -> str:
    """The API root for a typed-in address, e.g. "blueos.local" -> the v1.0 root.

    A scheme and a trailing slash are forgiven because techs paste them, but a
    path is refused rather than silently dropped: an address with a path in it
    means the tech is looking at something other than the boat's API, and a
    request built from it would go somewhere we cannot vouch for.
    """
    cleaned = host.strip()
    for scheme in ("http://", "https://"):
        if cleaned.lower().startswith(scheme):
            cleaned = cleaned[len(scheme):]
    cleaned = cleaned.rstrip("/")

    if not cleaned:
        raise KrakenError("No vehicle address given. Try blueos.local, or the boat's IP.")
    if not _HOST.fullmatch(cleaned):
        raise KrakenError(
            f"{host!r} is not a plain address. Enter just the hostname or IP, with an "
            "optional :port. For example blueos.local, 192.168.2.2, or "
            f"10.198.95.122:{KRAKEN_PORT}."
        )

    if ":" not in cleaned:
        cleaned = f"{cleaned}:{KRAKEN_PORT}"
    return f"http://{cleaned}{API_PREFIX}"


def _read_bounded(response: Any) -> str:
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


def _request(
    host: str,
    path: str,
    method: str = "GET",
    params: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> str:
    """One call against a boat's Kraken. Returns the response text.

    `params` are urlencoded, so an identifier is escaped rather than concatenated.
    Nothing secret is ever passed here as a parameter; see the module docstring.
    """
    url = f"{base_url(host)}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is None and method == "POST":
        # A POST with no body still needs one, or urllib sends a GET.
        data = b""
    headers = {"User-Agent": "DroneSetup", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return _read_bounded(response)
    except urllib.error.HTTPError as exc:
        detail = scrub_secrets(exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace"))
        raise KrakenError(
            f"{method} {path} on {host} answered {exc.code} {exc.reason}. {detail}".strip()
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise KrakenError(
            f"Could not reach BlueOS at {host}: {exc}. Check that the laptop is on the "
            "boat's network and that BlueOS is up."
        ) from exc


def fetch_installed_extensions(
    host: str, timeout: float = REQUEST_TIMEOUT
) -> tuple[InstalledExtension, ...]:
    """Every extension installed on a boat. Read-only, and safe on a boat in service."""
    text = _request(host, "/installed_extensions", timeout=timeout)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KrakenError(
            f"{host} answered something that is not JSON. Is port {KRAKEN_PORT} really "
            f"Kraken on this address? ({exc})"
        ) from exc
    return parse_installed_extensions(payload)


def discover_vehicle(
    hosts: tuple[str, ...] = DEFAULT_HOSTS, timeout: float = DISCOVERY_TIMEOUT
) -> tuple[str, tuple[InstalledExtension, ...]]:
    """The first address in `hosts` whose Kraken answers, and its extensions.

    Read-only. Returns the listing alongside the address so a caller never has to
    ask the same boat twice, and reports every address it tried on failure:
    "no vehicle found" on its own gives a tech nothing to check.
    """
    attempts = []
    for host in hosts:
        try:
            return host, fetch_installed_extensions(host, timeout=timeout)
        except KrakenError as exc:
            attempts.append(f"  {host}: {exc}")

    raise KrakenError(
        "No BlueOS vehicle answered on port "
        f"{KRAKEN_PORT}. Tried:\n" + "\n".join(attempts) + "\nEnter the boat's address "
        "directly if it is on a different network."
    )


def install_extension(host: str, source: dict, timeout: float = INSTALL_TIMEOUT) -> str:
    """Create or replace an extension from a full ExtensionSource body.

    The body carries the complete permissions block every time. Kraken has no
    partial update: whatever this sends becomes the extension's entire
    configuration, so anything left out is a permission the container loses.
    """
    return _request(host, "/extension/install", method="POST", body=source, timeout=timeout)


def update_extension_to_version(
    host: str, identifier: str, version: str, timeout: float = INSTALL_TIMEOUT
) -> str:
    """Move an installed extension to another tag, leaving its settings alone.

    Only safe when the boat's permissions have already been checked and are
    correct: this endpoint takes no body, so it cannot repair them. Anything that
    needs the permissions changed goes through install_extension() instead.
    """
    return _request(
        host,
        "/extension/update_to_version",
        method="POST",
        params={"extension_identifier": identifier, "new_version": version},
        timeout=timeout,
    )


def enable_extension(host: str, identifier: str, timeout: float = REQUEST_TIMEOUT) -> str:
    """Start a disabled extension.

    Kraken also exposes disable, restart and uninstall on the same shape. They are
    not wrapped here: nothing this tool does needs them, and an untested call that
    takes a boat down is not worth having available.
    """
    return _request(
        host,
        "/extension/enable",
        method="POST",
        params={"extension_identifier": identifier},
        timeout=timeout,
    )
