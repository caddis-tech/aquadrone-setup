"""Talk to BlueOS's autopilot manager, which owns a boat's MAVLink endpoints.

The endpoint list is how a ground station reaches a vehicle, and it is also how
GPS reaches every telemetry record, so this module is written to keep the
destructive half out of reach. It reads the list, creates endpoints and updates
them. It cannot delete one. The API offers DELETE, but three of the six endpoints
on a working boat are loopback links feeding mavlink2rest, and a boat that loses
them keeps capturing, keeps uploading and reports healthy while every reading
arrives with no position. Nothing this tool does needs DELETE, so following
kraken.py's rule about untested calls that take a boat down, it is not wrapped.

Writes refuse any endpoint flagged `protected` outright. That check belongs here,
at the wire, rather than only in the planner: it is the last place a mistake
further up can still be caught.

Every write of any kind restarts MAVLink Router and drops every live GCS
connection. Callers are expected to have asked the operator first.

Split the way kraken.py is: parsing and payload building are pure and unit-tested
with no network at all, and the urllib wrappers on top stay thin.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import blueos

# The autopilot manager's own port, not Kraken's 9134.
AUTOPILOT_PORT = 8000
API_PREFIX = "/v1.0"

# The trailing slash is load-bearing. FastAPI mounts the route at "/endpoints/"
# and 307s anything without it. urllib follows that silently for a GET, but a
# redirected POST is not dependable, so a write aimed at the unslashed path can
# arrive as a request the boat does nothing with while looking like it worked.
ENDPOINTS_PATH = "/endpoints/"

DISCOVERY_TIMEOUT = blueos.DISCOVERY_TIMEOUT
REQUEST_TIMEOUT = blueos.REQUEST_TIMEOUT

# Fields the autopilot manager's Endpoint schema defines. A GET also returns
# pydantic's own bookkeeping (`__pydantic_initialised__`), which is not
# configuration and is never sent back.
_WIRE_FIELDS = (
    "name",
    "owner",
    "connection_type",
    "place",
    "argument",
    "persistent",
    "protected",
    "enabled",
    "overwrite_settings",
)


class AutopilotError(Exception):
    """Anything that stopped us reading or changing a boat's MAVLink endpoints.

    One type for unreachable hosts, HTTP errors, malformed payloads and writes
    this module refuses to make, the way KrakenError covers the extension path.
    """


@dataclass(frozen=True)
class Endpoint:
    """One entry from GET /v1.0/endpoints/.

    `argument` is the port for the udp and tcp connection types, which is all a
    boat uses. It is named for the wire field rather than renamed to `port`
    because the same field carries a baud rate on a serial endpoint, and calling
    it a port there would be a lie waiting for someone to trip over.
    """

    name: str
    owner: str
    connection_type: str
    place: str
    argument: int
    persistent: bool
    protected: bool
    enabled: bool
    overwrite_settings: bool = False

    @property
    def route(self) -> str:
        """How this endpoint reads in a sentence: "udpin 0.0.0.0:14550"."""
        return f"{self.connection_type} {self.place}:{self.argument}"

    def as_payload(self) -> dict[str, Any]:
        """The endpoint as the API's schema wants it back.

        Built from named fields rather than by echoing what the boat sent, so
        pydantic's leaked bookkeeping never gets posted back as configuration,
        and so every flag we send is one this module chose on purpose.
        """
        return {field: getattr(self, field) for field in _WIRE_FIELDS}

    def with_state(self, *, enabled: bool, persistent: bool) -> Endpoint:
        """A copy with only those two flags set, everything else carried through.

        Keeping name, type, place and port identical is what makes an update
        match the endpoint already on the boat whichever of those the autopilot
        manager identifies it by.
        """
        return replace(self, enabled=enabled, persistent=persistent)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AutopilotError(
            f"The autopilot manager returned something we do not understand: {message}"
        )


def _endpoint_from_entry(entry: Any, index: int) -> Endpoint:
    _require(isinstance(entry, dict), f"entry {index} is a {type(entry).__name__}, not an object")

    name = entry.get("name")
    _require(isinstance(name, str) and name != "", f"entry {index} has no name")

    for field in ("owner", "connection_type", "place"):
        _require(isinstance(entry.get(field), str), f"{name} has no {field}")
    _require(isinstance(entry.get("argument"), int), f"{name} has no argument")

    # Demanded rather than defaulted, unlike the schema's own defaults. `protected`
    # is the flag that decides whether this tool may write an endpoint at all, and
    # a missing one silently reading as False is how a boat running an unexpected
    # BlueOS would get its loopback endpoints treated as fair game.
    for field in ("persistent", "protected", "enabled"):
        _require(isinstance(entry.get(field), bool), f"{name} has no {field} flag")

    return Endpoint(
        name=name,
        owner=entry["owner"],
        connection_type=entry["connection_type"],
        place=entry["place"],
        argument=entry["argument"],
        persistent=entry["persistent"],
        protected=entry["protected"],
        enabled=entry["enabled"],
        # The one genuinely optional flag: it is presentation for BlueOS's own
        # page, not access, and older builds may not send it.
        overwrite_settings=bool(entry.get("overwrite_settings", False)),
    )


def parse_endpoints(payload: Any) -> tuple[Endpoint, ...]:
    """Validated endpoints from a decoded /endpoints/ body. Pure."""
    _require(isinstance(payload, list), f"top level is a {type(payload).__name__}, not a list")
    return tuple(_endpoint_from_entry(entry, i) for i, entry in enumerate(payload))


def find_endpoint(endpoints: tuple[Endpoint, ...], name: str) -> Endpoint | None:
    """The one endpoint with this name, or None. Pure."""
    for endpoint in endpoints:
        if endpoint.name == name:
            return endpoint
    return None


def base_url(host: str) -> str:
    """The API root for a typed-in address, e.g. "blueos.local" -> the v1.0 root."""
    try:
        return f"http://{blueos.vehicle_host(host, AUTOPILOT_PORT)}{API_PREFIX}"
    except blueos.BlueOsError as exc:
        raise AutopilotError(
            f"{exc} For example blueos.local, 192.168.2.2, or 10.198.95.122:{AUTOPILOT_PORT}."
        ) from exc


def _request(
    host: str, method: str = "GET", body: Any = None, timeout: float = REQUEST_TIMEOUT
) -> str:
    """One call against a boat's autopilot manager. Returns the response text.

    Every route this module uses is /endpoints/, so there is no path to pass and
    no query string to build: nothing about a request here is caller-controlled
    except the verb and the body.
    """
    try:
        return blueos.request(
            f"{base_url(host)}{ENDPOINTS_PATH}", method=method, body=body, timeout=timeout
        )
    except blueos.BlueOsError as exc:
        raise AutopilotError(str(exc)) from exc


def fetch_endpoints(host: str, timeout: float = REQUEST_TIMEOUT) -> tuple[Endpoint, ...]:
    """Every MAVLink endpoint on a boat. Read-only, and safe on a boat in service."""
    text = _request(host, timeout=timeout)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutopilotError(
            f"{host} answered something that is not JSON. Is port {AUTOPILOT_PORT} really the "
            f"autopilot manager on this address? ({exc})"
        ) from exc
    return parse_endpoints(payload)


def discover_vehicle(
    hosts: tuple[str, ...] = blueos.DEFAULT_HOSTS, timeout: float = DISCOVERY_TIMEOUT
) -> tuple[str, tuple[Endpoint, ...]]:
    """The first address in `hosts` whose autopilot manager answers, and its endpoints.

    Read-only. Returns the listing alongside the address so a caller never has to
    ask the same boat twice, and reports every address it tried on failure.
    """
    attempts = []
    for host in hosts:
        try:
            return host, fetch_endpoints(host, timeout=timeout)
        except AutopilotError as exc:
            attempts.append(f"  {host}: {exc}")

    raise AutopilotError(
        f"No BlueOS vehicle answered on port {AUTOPILOT_PORT}. Tried:\n"
        + "\n".join(attempts)
        + "\nEnter the boat's address directly if it is on a different network."
    )


def _writable(endpoints: tuple[Endpoint, ...]) -> list[dict[str, Any]]:
    """The bodies to send, or an error naming what this module will not write.

    An empty list is refused as well as a protected one. Sending nothing still
    restarts the router, so an empty write means the logic upstream produced no
    change and should not have called at all.
    """
    if not endpoints:
        raise AutopilotError(
            "Refusing to write an empty endpoint list. Every write restarts MAVLink Router "
            "and drops live GCS connections, and this one would change nothing."
        )

    protected = [endpoint.name for endpoint in endpoints if endpoint.protected]
    if protected:
        raise AutopilotError(
            f"Refusing to write the protected endpoint(s) {', '.join(protected)}. Protected "
            "endpoints carry GPS into every telemetry record, and a boat that loses them "
            "keeps uploading and reports healthy while every reading arrives with no "
            "position. Change them in BlueOS by hand if that is really what you want."
        )
    return [endpoint.as_payload() for endpoint in endpoints]


def create_endpoints(
    host: str, endpoints: tuple[Endpoint, ...], timeout: float = REQUEST_TIMEOUT
) -> str:
    """Add endpoints that are not on the boat yet. Restarts MAVLink Router.

    The body is a JSON array even for one endpoint; the API takes a set, not an
    object. Creating one whose port is already taken is refused by the boat, so
    the caller checks for that first and reports it rather than sending a write
    that restarts the router only to fail.
    """
    return _request(host, method="POST", body=_writable(endpoints), timeout=timeout)


def update_endpoints(
    host: str, endpoints: tuple[Endpoint, ...], timeout: float = REQUEST_TIMEOUT
) -> str:
    """Replace endpoints already on the boat with these. Restarts MAVLink Router.

    Each entry carries the endpoint's whole state, so anything left out of the
    body is a setting the endpoint loses.
    """
    return _request(host, method="PUT", body=_writable(endpoints), timeout=timeout)
