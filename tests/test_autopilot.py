"""Tests for the MAVLink endpoint client: what a boat says, and what we say back.

No network. Every payload below is shaped like the real one: the fixtures are the
six endpoints a live `GET /v1.0/endpoints/` returned from DB Cooper, including
two details that matter. The autopilot manager leaks pydantic's own bookkeeping
key into its responses, which must never be posted back as though it were
configuration; and its schema defaults `persistent` to false, so an endpoint
created without saying otherwise is gone at the next router reload.

The property defended hardest here is that no write this module makes can ever
carry a protected endpoint. Those three loopback links carry GPS into every
telemetry record, and a boat that loses them keeps uploading and reports healthy
while every reading arrives with no position.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import autopilot
import pytest

# The live endpoint list from DB Cooper, 2026-08-12, verbatim in shape.
DB_COOPER = [
    {
        "name": "MAVLink2Rest", "owner": "ardupilot-manager", "connection_type": "udpout",
        "place": "127.0.0.1", "argument": 14000, "persistent": True, "protected": True,
        "enabled": True, "overwrite_settings": True, "__pydantic_initialised__": True,
    },
    {
        "name": "GCS Client Link", "owner": "ardupilot-manager", "connection_type": "udpout",
        "place": "192.168.2.1", "argument": 14550, "persistent": True, "protected": False,
        "enabled": True, "overwrite_settings": False, "__pydantic_initialised__": True,
    },
    {
        "name": "GCS Server Link", "owner": "ardupilot-manager", "connection_type": "udpin",
        "place": "0.0.0.0", "argument": 14550, "persistent": True, "protected": False,
        "enabled": True, "overwrite_settings": False, "__pydantic_initialised__": True,
    },
    {
        "name": "MAVLink2RestServer", "owner": "ardupilot-manager", "connection_type": "udpin",
        "place": "127.0.0.1", "argument": 14001, "persistent": True, "protected": True,
        "enabled": True, "overwrite_settings": False, "__pydantic_initialised__": True,
    },
    {
        "name": "Ping360 Heading", "owner": "ardupilot-manager", "connection_type": "udpin",
        "place": "0.0.0.0", "argument": 14660, "persistent": True, "protected": True,
        "enabled": True, "overwrite_settings": False, "__pydantic_initialised__": True,
    },
    {
        "name": "Internal Link", "owner": "ardupilot-manager", "connection_type": "tcpin",
        "place": "127.0.0.1", "argument": 5777, "persistent": True, "protected": True,
        "enabled": True, "overwrite_settings": True, "__pydantic_initialised__": True,
    },
]


def _entry(**overrides):
    """One endpoint entry, the stock GCS Server Link unless overridden."""
    entry = dict(DB_COOPER[2])
    entry.update(overrides)
    return entry


def _endpoint(**overrides) -> autopilot.Endpoint:
    (parsed,) = autopilot.parse_endpoints([_entry(**overrides)])
    return parsed


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self._body) if size < 0 else min(len(self._body), self._read + size)
        chunk = self._body[self._read:end]
        self._read = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _fake_urlopen(monkeypatch, body=b"[]", error=None):
    seen = {}

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = request.data
        if error is not None:
            raise error
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


# -- Reading what a boat reports ---------------------------------------------


def test_parses_the_six_endpoints_a_real_boat_returns():
    endpoints = autopilot.parse_endpoints(DB_COOPER)

    assert [e.name for e in endpoints] == [
        "MAVLink2Rest", "GCS Client Link", "GCS Server Link",
        "MAVLink2RestServer", "Ping360 Heading", "Internal Link",
    ]
    assert endpoints[2].route == "udpin 0.0.0.0:14550"
    assert [e.name for e in endpoints if e.protected] == [
        "MAVLink2Rest", "MAVLink2RestServer", "Ping360 Heading", "Internal Link",
    ]


def test_refuses_a_payload_that_is_not_a_list():
    with pytest.raises(autopilot.AutopilotError):
        autopilot.parse_endpoints({"name": "GCS Server Link"})


@pytest.mark.parametrize(
    "missing", ["name", "owner", "connection_type", "place", "argument", "persistent", "enabled"]
)
def test_refuses_an_entry_missing_a_field_rather_than_defaulting_it(missing):
    entry = _entry()
    del entry[missing]

    with pytest.raises(autopilot.AutopilotError):
        autopilot.parse_endpoints([entry])


def test_a_missing_protected_flag_is_refused_rather_than_read_as_false():
    """The schema defaults it to false, and we deliberately do not.

    `protected` is the flag that decides whether this tool may write an endpoint
    at all. A boat running an unexpected BlueOS that omitted it would have its
    loopback endpoints silently treated as fair game.
    """
    entry = _entry()
    del entry["protected"]

    with pytest.raises(autopilot.AutopilotError):
        autopilot.parse_endpoints([entry])


def test_overwrite_settings_is_the_one_flag_allowed_to_default():
    # Presentation for BlueOS's own page, not access, and older builds omit it.
    entry = _entry()
    del entry["overwrite_settings"]

    (endpoint,) = autopilot.parse_endpoints([entry])

    assert endpoint.overwrite_settings is False


def test_finds_the_one_endpoint_we_care_about():
    endpoints = autopilot.parse_endpoints(DB_COOPER)

    assert autopilot.find_endpoint(endpoints, "GCS Server Link").argument == 14550
    assert autopilot.find_endpoint(endpoints, "QGC Cellular ZT") is None


# -- What we send back -------------------------------------------------------


def test_a_payload_carries_the_schema_fields_and_nothing_else():
    """The pydantic key the boat leaks is bookkeeping, not configuration."""
    payload = _endpoint().as_payload()

    assert set(payload) == {
        "name", "owner", "connection_type", "place", "argument",
        "persistent", "protected", "enabled", "overwrite_settings",
    }
    assert "__pydantic_initialised__" not in payload


def test_changing_state_leaves_the_address_alone():
    # Keeping name, type, place and port identical is what makes an update match
    # the endpoint already on the boat, however the manager identifies it.
    original = _endpoint(enabled=False, persistent=False)

    updated = original.with_state(enabled=True, persistent=True)

    assert (updated.name, updated.connection_type, updated.place, updated.argument) == (
        original.name, original.connection_type, original.place, original.argument
    )
    assert (updated.enabled, updated.persistent) == (True, True)
    assert (original.enabled, original.persistent) == (False, False)


# -- What we refuse to send --------------------------------------------------


@pytest.mark.parametrize("write", [autopilot.create_endpoints, autopilot.update_endpoints])
def test_no_write_can_carry_a_protected_endpoint(write, monkeypatch):
    """The single most damaging thing this code could do, refused at the wire.

    Removing or disabling the loopback endpoints costs every telemetry reading
    its GPS position while the boat keeps uploading and reports healthy.
    """
    seen = _fake_urlopen(monkeypatch, b"ok")
    protected = _endpoint(name="MAVLink2Rest", protected=True)

    with pytest.raises(autopilot.AutopilotError, match="protected"):
        write("blueos.local", (protected,))

    assert seen == {}, "nothing should have reached the network"


@pytest.mark.parametrize("write", [autopilot.create_endpoints, autopilot.update_endpoints])
def test_one_protected_endpoint_poisons_the_whole_write(write, monkeypatch):
    # A batch is all-or-nothing: sending the safe half would still restart the
    # router while leaving the caller believing the whole batch went.
    seen = _fake_urlopen(monkeypatch, b"ok")

    with pytest.raises(autopilot.AutopilotError, match="protected"):
        write("blueos.local", (_endpoint(), _endpoint(name="Internal Link", protected=True)))

    assert seen == {}


@pytest.mark.parametrize("write", [autopilot.create_endpoints, autopilot.update_endpoints])
def test_an_empty_write_is_refused_rather_than_sent(write, monkeypatch):
    # Sending nothing still restarts the router and drops every live connection.
    seen = _fake_urlopen(monkeypatch, b"ok")

    with pytest.raises(autopilot.AutopilotError):
        write("blueos.local", ())

    assert seen == {}


def test_there_is_no_delete():
    """The API offers DELETE. Wrapping it buys nothing and risks the boat."""
    assert not [name for name in dir(autopilot) if "delete" in name.lower()]


# -- The wire ----------------------------------------------------------------


def test_reading_hits_the_autopilot_managers_port_not_krakens(monkeypatch):
    seen = _fake_urlopen(monkeypatch, json.dumps(DB_COOPER).encode())

    endpoints = autopilot.fetch_endpoints("blueos.local")

    assert seen["url"] == "http://blueos.local:8000/v1.0/endpoints/"
    assert seen["method"] == "GET"
    assert len(endpoints) == 6


def test_every_route_keeps_the_trailing_slash(monkeypatch):
    """FastAPI mounts the route at /endpoints/ and 307s anything without it.

    urllib follows that silently for a GET, but a redirected POST is not
    dependable, so a write to the unslashed path can arrive as something the
    boat does nothing with while looking like it worked.
    """
    for call in (
        lambda: autopilot.fetch_endpoints("blueos.local"),
        lambda: autopilot.create_endpoints("blueos.local", (_endpoint(),)),
        lambda: autopilot.update_endpoints("blueos.local", (_endpoint(),)),
    ):
        seen = _fake_urlopen(monkeypatch, b"[]")
        call()
        assert seen["url"].endswith("/v1.0/endpoints/")


def test_creating_posts_a_json_array_even_for_one_endpoint(monkeypatch):
    seen = _fake_urlopen(monkeypatch, b"ok")

    autopilot.create_endpoints("blueos.local", (_endpoint(),))

    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == [_endpoint().as_payload()]


def test_updating_puts_a_json_array(monkeypatch):
    seen = _fake_urlopen(monkeypatch, b"ok")

    autopilot.update_endpoints("blueos.local", (_endpoint(enabled=False),))

    assert seen["method"] == "PUT"
    assert json.loads(seen["body"])[0]["enabled"] is False


def test_a_reply_that_is_not_json_says_which_port_to_check(monkeypatch):
    _fake_urlopen(monkeypatch, b"<html>BlueOS</html>")

    with pytest.raises(autopilot.AutopilotError, match="8000"):
        autopilot.fetch_endpoints("blueos.local")


def test_an_unreachable_boat_is_an_autopilot_error_not_a_blueos_one(monkeypatch):
    # The caller's `except` should name the thing it was actually doing.
    _fake_urlopen(monkeypatch, error=urllib.error.URLError("no route to host"))

    with pytest.raises(autopilot.AutopilotError):
        autopilot.fetch_endpoints("blueos.local")


def test_a_bad_address_is_refused_before_anything_is_sent(monkeypatch):
    seen = _fake_urlopen(monkeypatch, b"[]")

    with pytest.raises(autopilot.AutopilotError):
        autopilot.fetch_endpoints("blueos.local/endpoints")

    assert seen == {}


# -- Discovery ---------------------------------------------------------------


def test_discovery_returns_the_first_address_that_answers(monkeypatch):
    def urlopen(request, timeout=None):
        if "192.168.2.2" not in request.full_url:
            raise urllib.error.URLError("no route to host")
        return _FakeResponse(json.dumps(DB_COOPER).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    host, endpoints = autopilot.discover_vehicle(("blueos.local", "192.168.2.2"))

    assert host == "192.168.2.2"
    assert len(endpoints) == 6


def test_discovery_failing_names_every_address_it_tried(monkeypatch):
    _fake_urlopen(monkeypatch, error=urllib.error.URLError("no route to host"))

    with pytest.raises(autopilot.AutopilotError) as caught:
        autopilot.discover_vehicle(("blueos.local", "192.168.2.2"))

    assert "blueos.local" in str(caught.value)
    assert "192.168.2.2" in str(caught.value)
