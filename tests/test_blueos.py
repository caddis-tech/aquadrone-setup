"""Tests for the addressing and HTTP plumbing both service clients sit on.

Two BlueOS APIs live on one board at different ports, so the thing a tech types
has to resolve to whichever one the caller wanted. The rest of this file is
about the two ways a response can hurt us: one that never stops arriving, and
one that quotes our own request body back at us with a token in it.
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request

import blueos
import pytest


class _FakeResponse:
    """Enough of an http.client.HTTPResponse for read_bounded()."""

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


# -- Addresses ---------------------------------------------------------------


def test_the_default_port_depends_on_which_service_is_asking():
    # One board, two APIs. The same typed address has to reach either.
    assert blueos.vehicle_host("blueos.local", 9134) == "blueos.local:9134"
    assert blueos.vehicle_host("blueos.local", 8000) == "blueos.local:8000"


def test_a_port_the_tech_typed_beats_the_default():
    assert blueos.vehicle_host("10.198.95.122:8000", 9134) == "10.198.95.122:8000"


@pytest.mark.parametrize("typed", ["http://blueos.local", "https://blueos.local/", "blueos.local/"])
def test_a_pasted_url_is_forgiven(typed):
    assert blueos.vehicle_host(typed, 8000) == "blueos.local:8000"


@pytest.mark.parametrize("typed", ["blueos.local/endpoints", "192.168.2.2 8000", "", "   "])
def test_an_address_with_a_path_or_junk_in_it_is_refused(typed):
    # Silently dropping the path would send the request somewhere we cannot
    # vouch for while telling the tech it went to the boat.
    with pytest.raises(blueos.BlueOsError):
        blueos.vehicle_host(typed, 8000)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("10.198.95.122", "10.198.95.122"),
        ("10.198.95.122:8000", "10.198.95.122"),
        ("http://blueos.local:9134/", "blueos.local"),
    ],
)
def test_the_bare_host_drops_whichever_api_port_was_typed_with_it(typed, expected):
    # QGroundControl dials 14550. Telling a tech to point it at the port this
    # tool happened to be talking to would send them somewhere with no MAVLink.
    assert blueos.host_only(typed) == expected


# -- Responses ---------------------------------------------------------------


def test_a_long_response_is_drained_rather_than_cut_off():
    """Kraken's install streams Docker pull progress. Closing the socket early
    aborts the pull instead of just truncating our copy of it."""
    response = _FakeResponse(b"x" * (blueos.MAX_RESPONSE_BYTES + 5000))

    kept = blueos.read_bounded(response)

    assert len(kept) == blueos.MAX_RESPONSE_BYTES
    assert response.read(1) == b""


def test_an_http_error_carries_the_servers_own_words(monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", {}, io.BytesIO(b"it exploded")
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(blueos.BlueOsError, match="it exploded"):
        blueos.request("http://blueos.local:8000/v1.0/endpoints/")


def test_an_unreachable_boat_says_what_to_check(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("timed out")),
    )

    with pytest.raises(blueos.BlueOsError, match="network"):
        blueos.request("http://blueos.local:8000/v1.0/endpoints/")


def test_the_scrubber_runs_before_the_message_is_built_not_after(monkeypatch):
    """A caller's scrubber has to reach the string at the moment it is created.

    Redacting afterwards would leave the unredacted message hanging off the
    exception as its __cause__, where a traceback would still print it.
    """
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 422, "Unprocessable", {}, io.BytesIO(b"rejected: TOKEN=s3cret")
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(blueos.BlueOsError) as caught:
        blueos.request(
            "http://blueos.local:9134/v1.0/x", scrub=lambda text: text.replace("s3cret", "***")
        )

    assert "s3cret" not in str(caught.value)
    assert "s3cret" not in str(caught.value.__cause__ or "")


# -- Requests ----------------------------------------------------------------


def test_a_body_travels_as_json_with_a_content_type(monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["headers"] = dict(request.headers)
        return _FakeResponse(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    blueos.request("http://blueos.local:8000/v1.0/endpoints/", method="PUT", body=[{"a": 1}])

    assert seen["method"] == "PUT"
    assert seen["body"] == b'[{"a": 1}]'
    assert seen["headers"]["Content-type"] == "application/json"


def test_a_bodyless_post_still_sends_a_body(monkeypatch):
    # urllib sends a GET when data is None, whatever method it was told to use.
    seen = {}

    def urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["body"] = request.data
        return _FakeResponse(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    blueos.request("http://blueos.local:9134/v1.0/x", method="POST")

    assert seen["method"] == "POST"
    assert seen["body"] == b""
