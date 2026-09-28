"""Tests for the Kraken client: what a boat says, and what we say back to it.

No network. Every payload below is shaped like the real one: the fixtures came
from a live `GET /v1.0/installed_extensions` on DB Cooper, including the detail
that matters most: Kraken keeps the *manifest's* permissions in `permissions` and
the operator's in `user_permissions`, and for a privately-hosted image like MANTA
Link the first of those is literally the string "{}".

The other thing defended here is that a boat's API token, which lives in the Env
array Kraken hands back, never reaches a log line or an error message.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import extension_settings
import kraken
import pytest

REAL_PERMISSIONS = json.dumps(extension_settings.read_docker_permissions())


def _entry(identifier="caddis.manta-link", **overrides):
    """One installed_extensions entry, with the field set the live boat has set."""
    entry = {
        "identifier": identifier,
        "name": "MANTA Link",
        "docker": "ghcr.io/caddis-tech/manta-link",
        "tag": "0.9.0",
        "enabled": True,
        "auth": None,
        # A manually created extension has no manifest entry, so Kraken has
        # nothing to put here. This is what DB Cooper actually returns.
        "permissions": "{}",
        "user_permissions": REAL_PERMISSIONS,
    }
    entry.update(overrides)
    return entry


class _FakeResponse:
    """Enough of an http.client.HTTPResponse for _read_bounded()."""

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
    """Swap in a urlopen that records the Request it was handed."""
    seen = {}

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["headers"] = dict(request.headers)
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


# -- Addresses ---------------------------------------------------------------


def test_a_bare_hostname_gets_krakens_port_and_api_prefix():
    assert kraken.base_url("blueos.local") == "http://blueos.local:9134/v1.0"


def test_an_explicit_port_is_kept():
    # The bench rig answers over ZeroTier, which is not the tether address.
    assert kraken.base_url("10.198.95.122:9134").startswith("http://10.198.95.122:9134")


@pytest.mark.parametrize("typed", ["http://blueos.local", "https://blueos.local/", "blueos.local/"])
def test_a_pasted_url_is_forgiven(typed):
    assert kraken.base_url(typed) == "http://blueos.local:9134/v1.0"


@pytest.mark.parametrize("typed", ["blueos.local/extensions", "192.168.2.2 8000", ""])
def test_an_address_with_a_path_or_junk_in_it_is_refused(typed):
    # Silently dropping the path would send the request somewhere we cannot
    # vouch for while telling the tech it went to the boat.
    with pytest.raises(kraken.KrakenError):
        kraken.base_url(typed)


# -- Reading what a boat reports ---------------------------------------------


def test_parses_the_shape_a_real_boat_returns():
    extensions = kraken.parse_installed_extensions([_entry(), _entry("bluerobotics.cockpit")])

    assert [e.identifier for e in extensions] == ["caddis.manta-link", "bluerobotics.cockpit"]
    assert extensions[0].tag == "0.9.0"
    assert extensions[0].enabled is True


def test_refuses_a_payload_that_is_not_a_list():
    with pytest.raises(kraken.KrakenError):
        kraken.parse_installed_extensions({"identifier": "caddis.manta-link"})


@pytest.mark.parametrize("missing", ["identifier", "tag", "name", "docker", "enabled"])
def test_refuses_an_entry_missing_a_field_rather_than_defaulting_it(missing):
    # A boat running an unexpected BlueOS must not audit as healthy because a
    # field we needed was quietly assumed.
    entry = _entry()
    del entry[missing]

    with pytest.raises(kraken.KrakenError):
        kraken.parse_installed_extensions([entry])


def test_finds_the_one_extension_we_care_about():
    extensions = kraken.parse_installed_extensions([_entry("bluerobotics.cockpit"), _entry()])

    assert kraken.find_extension(extensions, "caddis.manta-link").tag == "0.9.0"
    assert kraken.find_extension(extensions, "caddis.nothing") is None


# -- Which permissions block actually applies --------------------------------


def test_user_permissions_are_what_the_container_runs_with():
    (extension,) = kraken.parse_installed_extensions([_entry()])

    assert extension.effective_settings() == extension_settings.read_docker_permissions()


def test_user_permissions_set_to_empty_is_a_real_empty_not_a_fallback():
    """The whole failure class. `"{}"` is a non-empty string, so Kraken uses it and
    the container gets nothing, and it does not fall back to the other field."""
    (extension,) = kraken.parse_installed_extensions(
        [_entry(permissions=REAL_PERMISSIONS, user_permissions="{}")]
    )

    assert extension.effective_settings() == {}


def test_an_unset_user_permissions_falls_back_to_the_manifest_block():
    # How every stock BlueOS extension is configured: "" really is unset.
    (extension,) = kraken.parse_installed_extensions(
        [_entry(permissions=REAL_PERMISSIONS, user_permissions="")]
    )

    assert extension.effective_settings()["HostConfig"]["NetworkMode"] == "host"


def test_a_null_permissions_field_reads_as_unset():
    entry = _entry(permissions=None, user_permissions="")
    (extension,) = kraken.parse_installed_extensions([entry])

    assert extension.effective_settings() == {}


@pytest.mark.parametrize("stored", ["not json at all", '"a string"', "[1, 2]"])
def test_permissions_that_cannot_be_read_are_an_error_not_an_empty_dict(stored):
    # Guessing {} here would report the boat as having empty permissions, which
    # is a different fault with a different fix.
    (extension,) = kraken.parse_installed_extensions([_entry(user_permissions=stored)])

    with pytest.raises(kraken.KrakenError):
        extension.effective_settings()


def test_one_broken_extension_does_not_stop_us_reading_the_others():
    # Parsing is lazy for exactly this: a neighbour's malformed block is not our
    # boat's problem.
    extensions = kraken.parse_installed_extensions(
        [_entry("bluerobotics.cockpit", user_permissions="{{{"), _entry()]
    )

    assert kraken.find_extension(extensions, "caddis.manta-link").effective_settings()


# -- Never showing a token ---------------------------------------------------


def test_a_token_in_a_boats_env_is_masked_for_display():
    settings = {"Env": ["CADDIS_API_TOKEN=s3cret", "CADDIS_API_URL=https://api.caddistech.com"]}

    redacted = kraken.redact_env(settings)

    assert "s3cret" not in json.dumps(redacted)
    assert "CADDIS_API_URL=https://api.caddistech.com" in redacted["Env"]


def test_redacting_leaves_the_original_intact():
    # The un-redacted block is what gets sent back on a reinstall. Masking it in
    # place would write "<redacted>" to the boat as its real token.
    settings = {"Env": ["CADDIS_API_TOKEN=s3cret"]}

    kraken.redact_env(settings)

    assert settings["Env"] == ["CADDIS_API_TOKEN=s3cret"]


@pytest.mark.parametrize(
    "text",
    [
        "CADDIS_API_TOKEN=s3cret",
        '{"Env": ["CADDIS_API_TOKEN=s3cret"]}',
        'body: {\\"CADDIS_API_TOKEN\\": \\"s3cret\\"}',
    ],
)
def test_a_token_echoed_back_in_an_error_is_scrubbed(text):
    assert "s3cret" not in kraken.scrub_secrets(text)


def test_scrubbing_leaves_the_api_url_readable():
    # Over-redaction would hide the one field worth checking by eye.
    assert kraken.scrub_secrets("CADDIS_API_URL=https://api.caddistech.com").endswith(".com")


# -- The wire ----------------------------------------------------------------


def test_fetches_and_parses_a_listing(monkeypatch):
    seen = _fake_urlopen(monkeypatch, json.dumps([_entry()]).encode())

    extensions = kraken.fetch_installed_extensions("blueos.local")

    assert seen["url"] == "http://blueos.local:9134/v1.0/installed_extensions"
    assert seen["method"] == "GET"
    assert extensions[0].identifier == "caddis.manta-link"


def test_a_reply_that_is_not_json_says_so_rather_than_crashing(monkeypatch):
    _fake_urlopen(monkeypatch, b"<html>BlueOS</html>")

    with pytest.raises(kraken.KrakenError, match="not JSON"):
        kraken.fetch_installed_extensions("blueos.local")


def test_an_http_error_carries_the_servers_reason(monkeypatch):
    _fake_urlopen(
        monkeypatch,
        error=urllib.error.HTTPError(
            "http://blueos.local", 500, "Internal Server Error", {}, io.BytesIO(b"kraken exploded")
        ),
    )

    with pytest.raises(kraken.KrakenError, match="500"):
        kraken.fetch_installed_extensions("blueos.local")


def test_an_error_body_echoing_our_request_does_not_leak_the_token(monkeypatch):
    # Kraken quotes the body it rejected, and a reinstall body carries whatever
    # Env the boat already had.
    _fake_urlopen(
        monkeypatch,
        error=urllib.error.HTTPError(
            "http://blueos.local",
            422,
            "Unprocessable",
            {},
            io.BytesIO(b'{"detail": "bad body: CADDIS_API_TOKEN=s3cret"}'),
        ),
    )

    with pytest.raises(kraken.KrakenError) as caught:
        kraken.fetch_installed_extensions("blueos.local")
    assert "s3cret" not in str(caught.value)


def test_an_unreachable_boat_says_what_to_check(monkeypatch):
    _fake_urlopen(monkeypatch, error=urllib.error.URLError("timed out"))

    with pytest.raises(kraken.KrakenError, match="network"):
        kraken.fetch_installed_extensions("blueos.local")


# Bounded reading and the urllib plumbing moved to blueos.py when the autopilot
# manager client needed the same thing; they are tested in test_blueos.py.


# -- Discovery ---------------------------------------------------------------


def test_discovery_returns_the_first_address_that_answers(monkeypatch):
    answered = json.dumps([_entry()]).encode()

    def urlopen(request, timeout=None):
        if "192.168.2.2" not in request.full_url:
            raise urllib.error.URLError("no route to host")
        return _FakeResponse(answered)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    host, extensions = kraken.discover_vehicle(("blueos.local", "192.168.2.2"))

    assert host == "192.168.2.2"
    assert extensions[0].identifier == "caddis.manta-link"


def test_discovery_failing_names_every_address_it_tried(monkeypatch):
    # "No vehicle found" on its own gives a tech nothing to check.
    _fake_urlopen(monkeypatch, error=urllib.error.URLError("no route to host"))

    with pytest.raises(kraken.KrakenError) as caught:
        kraken.discover_vehicle(("blueos.local", "192.168.2.2"))

    assert "blueos.local" in str(caught.value)
    assert "192.168.2.2" in str(caught.value)


# -- Writing -----------------------------------------------------------------


def test_installing_posts_the_whole_body_as_json(monkeypatch):
    seen = _fake_urlopen(monkeypatch, b"ok")
    source = extension_settings.build_extension_source()

    kraken.install_extension("blueos.local", source)

    assert seen["method"] == "POST"
    assert seen["url"] == "http://blueos.local:9134/v1.0/extension/install"
    assert json.loads(seen["body"]) == source


def test_updating_passes_the_identifier_and_version_as_query_parameters(monkeypatch):
    seen = _fake_urlopen(monkeypatch, b"ok")

    kraken.update_extension_to_version("blueos.local", "caddis.manta-link", "0.9.0")

    assert seen["method"] == "POST"
    assert "extension_identifier=caddis.manta-link" in seen["url"]
    assert "new_version=0.9.0" in seen["url"]


def test_nothing_secret_is_ever_put_in_a_url(monkeypatch):
    """Query parameters land in request logs. Only public strings go there.

    The install body is the one thing that can carry a token, and it is a POST
    body precisely so it does not become part of a URL.
    """
    seen = _fake_urlopen(monkeypatch, b"ok")
    source = extension_settings.build_extension_source(["CADDIS_API_TOKEN=s3cret"])

    kraken.install_extension("blueos.local", source)

    assert "s3cret" not in seen["url"]
    assert "?" not in seen["url"]


def test_enabling_posts_with_an_empty_body(monkeypatch):
    # urllib sends a GET when data is None, whatever method it was told to use.
    seen = _fake_urlopen(monkeypatch, b"ok")

    kraken.enable_extension("blueos.local", "caddis.manta-link")

    assert seen["method"] == "POST"
    assert seen["body"] == b""
