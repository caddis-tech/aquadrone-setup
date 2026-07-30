"""Tests for the published firmware list the app fetches and the file it downloads.

The thing being defended here is the trust chain: an embedded public key vouches for
the manifest, the manifest vouches for the bytes, and the bytes end up on a Pico that
goes in a lake. Every test below is one link in that, or one way a link can be faked.

No network anywhere. The signing half runs on a throwaway keypair generated per test
run -- a committed private key would make the whole scheme decorative.
"""
from __future__ import annotations

import io
import json

import firmware_manifest
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VALID_URL = f"{firmware_manifest.RELEASE_URL_PREFIX}v1.2.0/AquaD_Pico_v1.2.0.uf2"
DIGEST_OF_NOTHING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
def signer(monkeypatch):
    """A throwaway keypair, with its public half swapped in as the app's built-in key.

    Returns a function that signs bytes with the matching private half, so a test can
    produce a manifest the app under test will accept.
    """
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    monkeypatch.setattr(firmware_manifest, "SIGNING_PUBLIC_KEY_HEX", public_hex)
    return private.sign


def _entry(version="1.2.0", channel="stable", **overrides):
    entry = {
        "version": version,
        "channel": channel,
        "url": f"{firmware_manifest.RELEASE_URL_PREFIX}v{version}/AquaD_Pico_v{version}.uf2",
        "sha256": DIGEST_OF_NOTHING,
        "size": 205312,
        "released": "2026-07-30T12:00:00Z",
    }
    entry.update(overrides)
    return entry


def _manifest(*entries, schema=firmware_manifest.SCHEMA_VERSION) -> bytes:
    return json.dumps({"schema": schema, "builds": list(entries)}).encode("utf-8")


def _build(**overrides) -> firmware_manifest.Build:
    fields = {
        "version": "1.2.0",
        "channel": "stable",
        "url": VALID_URL,
        "sha256": DIGEST_OF_NOTHING,
        "size": 0,
        "released": "2026-07-30T12:00:00Z",
    }
    fields.update(overrides)
    return firmware_manifest.Build(**fields)


# -- The signature ------------------------------------------------------------
#
# This is the only thing standing between "someone served us a JSON file" and "Caddis
# published this firmware". HTTPS proves who answered the request, not who wrote it.

def test_accepts_a_manifest_we_signed(signer):
    raw = _manifest(_entry())

    firmware_manifest.verify_manifest(raw, signer(raw))  # must not raise


def test_rejects_a_manifest_altered_after_signing(signer):
    raw = _manifest(_entry())
    signature = signer(raw)
    tampered = raw.replace(b"1.2.0", b"9.9.9")

    with pytest.raises(firmware_manifest.ManifestError, match="signature check"):
        firmware_manifest.verify_manifest(tampered, signature)


def test_rejects_a_manifest_signed_by_someone_else(signer):
    """The case that matters if the public repo is ever taken over.

    An attacker with push access can replace both the manifest and its signature file.
    They cannot produce a signature that verifies against the key inside the exe.
    """
    raw = _manifest(_entry())
    impostor = Ed25519PrivateKey.generate()

    with pytest.raises(firmware_manifest.ManifestError, match="signature check"):
        firmware_manifest.verify_manifest(raw, impostor.sign(raw))


def test_rejects_a_truncated_signature(signer):
    raw = _manifest(_entry())

    with pytest.raises(firmware_manifest.ManifestError, match="expected 64"):
        firmware_manifest.verify_manifest(raw, signer(raw)[:32])


def test_signature_covers_the_exact_bytes_not_the_parsed_document(signer):
    """Re-encoding between verify and parse would silently break the guarantee.

    Same JSON, different bytes: whitespace changes nothing semantically and everything
    cryptographically. If this ever passes, someone has started verifying a
    re-serialised copy instead of what actually came off the wire.
    """
    raw = _manifest(_entry())
    reindented = json.dumps(json.loads(raw), indent=4).encode("utf-8")

    with pytest.raises(firmware_manifest.ManifestError):
        firmware_manifest.verify_manifest(reindented, signer(raw))


# -- Parsing ------------------------------------------------------------------
#
# A good signature proves the bytes are ours. It does not prove whatever produced
# them was working, and this is the last check before a URL gets fetched.

def test_parses_a_well_formed_manifest():
    builds = firmware_manifest.parse_manifest(_manifest(_entry()), "stable")

    assert len(builds) == 1
    assert builds[0].version == "1.2.0"
    assert builds[0].size == 205312


def test_returns_only_the_requested_channel():
    raw = _manifest(_entry(version="1.2.0"), _entry(version="1.3.0", channel="experimental"))

    assert [b.version for b in firmware_manifest.parse_manifest(raw, "stable")] == ["1.2.0"]
    assert [b.version for b in firmware_manifest.parse_manifest(raw, "experimental")] == ["1.3.0"]


def test_orders_newest_version_first():
    # Numerically, not lexically: "1.10.0" sorts below "1.9.0" as a string, and a tech
    # picking the pre-selected top entry would silently get an older build.
    raw = _manifest(_entry(version="1.9.0"), _entry(version="1.10.0"), _entry(version="1.2.0"))

    versions = [b.version for b in firmware_manifest.parse_manifest(raw, "stable")]

    assert versions == ["1.10.0", "1.9.0", "1.2.0"]


def test_rejects_a_version_carrying_the_hil_marker():
    """The publishing side should never emit this. The app refuses it anyway.

    Requiring a plain x.y.z version means the test build cannot even be listed, let
    alone selected, no matter what goes wrong upstream of here.
    """
    raw = _manifest(_entry(version="1.2.0+HIL"))

    with pytest.raises(firmware_manifest.ManifestError, match="not a plain"):
        firmware_manifest.parse_manifest(raw, "stable")


def test_rejects_a_download_url_pointing_off_our_release_storage():
    # Constrains what the app is willing to fetch even when the signing side is what
    # went wrong.
    raw = _manifest(_entry(url="https://example.invalid/evil.uf2"))

    with pytest.raises(firmware_manifest.ManifestError, match="not under"):
        firmware_manifest.parse_manifest(raw, "stable")


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"sha256": "not a digest"}, "sha256"),
        ({"sha256": DIGEST_OF_NOTHING.upper()}, "sha256"),  # we compare against lowercase hex
        ({"size": 0}, "size"),
        ({"size": -1}, "size"),
        ({"size": firmware_manifest.MAX_BUILD_BYTES + 1}, "size"),
        ({"size": True}, "size"),  # bool is an int in Python; it is not a byte count
        ({"channel": "nightly"}, "channel"),
        ({"released": "yesterday"}, "released"),
        ({"version": "1.2"}, "version"),
    ],
)
def test_rejects_malformed_entries(overrides, expected):
    raw = _manifest(_entry(**overrides))

    with pytest.raises(firmware_manifest.ManifestError, match=expected):
        firmware_manifest.parse_manifest(raw, "stable")


def test_rejects_an_entry_missing_a_field():
    entry = _entry()
    del entry["sha256"]

    with pytest.raises(firmware_manifest.ManifestError):
        firmware_manifest.parse_manifest(_manifest(entry), "stable")


def test_rejects_a_future_schema_and_says_what_to_do():
    with pytest.raises(firmware_manifest.ManifestError, match="newer DroneSetup"):
        firmware_manifest.parse_manifest(_manifest(_entry(), schema=2), "stable")


def test_rejects_non_json():
    with pytest.raises(firmware_manifest.ManifestError, match="not valid JSON"):
        firmware_manifest.parse_manifest(b"<html>404</html>", "stable")


def test_rejects_a_manifest_that_is_not_an_object():
    with pytest.raises(firmware_manifest.ManifestError):
        firmware_manifest.parse_manifest(b"[]", "stable")


def test_rejects_builds_that_is_not_a_list():
    with pytest.raises(firmware_manifest.ManifestError, match="'builds'"):
        firmware_manifest.parse_manifest(b'{"schema": 1, "builds": {}}', "stable")


def test_an_empty_manifest_is_not_an_error():
    # A brand new firmware repo before the first publish. Nothing to offer is a normal
    # state, not a failure, and the UI says so rather than showing an error.
    assert firmware_manifest.parse_manifest(_manifest(), "stable") == ()


# -- Downloading --------------------------------------------------------------

def test_streams_and_accepts_a_matching_file(tmp_path):
    payload = b"pretend uf2 bytes"
    build = _build(size=len(payload), sha256=__import__("hashlib").sha256(payload).hexdigest())
    dest = tmp_path / "out.uf2"

    firmware_manifest.stream_verified(io.BytesIO(payload), build, dest)

    assert dest.read_bytes() == payload


def test_refuses_a_file_whose_digest_does_not_match(tmp_path):
    # A corrupted or swapped .uf2. This is the check that stops it reaching a Pico.
    payload = b"pretend uf2 bytes"
    build = _build(size=len(payload), sha256="0" * 64)

    with pytest.raises(firmware_manifest.ManifestError, match="signed checksum"):
        firmware_manifest.stream_verified(io.BytesIO(payload), build, tmp_path / "out.uf2")


def test_refuses_a_response_longer_than_the_signed_size(tmp_path):
    # Enforced while streaming rather than after, so an endless response cannot fill
    # the disk before we get to compare digests.
    build = _build(size=4)

    with pytest.raises(firmware_manifest.ManifestError, match="longer than"):
        firmware_manifest.stream_verified(io.BytesIO(b"x" * 4096), build, tmp_path / "out.uf2")


def test_refuses_a_response_that_ends_early(tmp_path):
    build = _build(size=4096)

    with pytest.raises(firmware_manifest.ManifestError, match="ended early"):
        firmware_manifest.stream_verified(io.BytesIO(b"xx"), build, tmp_path / "out.uf2")


def test_reports_progress_as_it_goes(tmp_path):
    payload = b"z" * 4096
    build = _build(size=len(payload), sha256=__import__("hashlib").sha256(payload).hexdigest())
    seen = []

    firmware_manifest.stream_verified(
        io.BytesIO(payload), build, tmp_path / "out.uf2", progress_cb=lambda n, t: seen.append(n)
    )

    assert seen and seen[-1] == len(payload)


# -- Local filenames ----------------------------------------------------------

def test_local_filename_comes_from_the_version_not_the_url():
    """A filename taken from a remote string is a path traversal waiting to happen."""
    build = _build(url=f"{firmware_manifest.RELEASE_URL_PREFIX}v1.2.0/../../../evil.exe")

    assert build.local_filename == "AquaD_Pico_v1.2.0.uf2"


def test_prerelease_downloads_do_not_overwrite_the_stable_build_of_the_same_version():
    stable = _build(version="1.3.0", channel="stable")
    experimental = _build(version="1.3.0", channel="experimental")

    assert stable.local_filename != experimental.local_filename


def test_a_downloaded_name_is_still_one_the_app_will_auto_select():
    # Both channels carry production images, so both should be offered by
    # flash.find_firmware() next time the app opens.
    import fnmatch

    import flash

    for channel in firmware_manifest.CHANNELS:
        name = _build(channel=channel).local_filename
        assert fnmatch.fnmatch(name, flash.FIRMWARE_GLOB), name
