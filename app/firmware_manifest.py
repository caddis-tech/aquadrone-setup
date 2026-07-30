"""Listing published firmware builds, and downloading one without trusting the network.

The app ships a public Ed25519 *verification* key and nothing else. No token, no
credential, nothing that would be worth extracting from the exe. Everything here is
shaped by that: CI signs the manifest at publish time, the app verifies the signature
over the exact bytes it fetched, and every entry carries the SHA-256 of its .uf2 so
the file that reaches a Pico is provably the file we published. HTTPS on its own
would only prove that whoever answered the request said so.

Split the way flash.py is: parse_manifest() and the verify helpers are pure and get
unit-tested with no network at all, and the urllib wrappers on top stay thin.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import exists for the annotation only
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FIRMWARE_REPO = "caddis-tech/aquadrone-pico-firmware"
MANIFEST_URL = f"https://raw.githubusercontent.com/{FIRMWARE_REPO}/main/manifest.json"
SIGNATURE_URL = f"{MANIFEST_URL}.sig"
RELEASE_URL_PREFIX = f"https://github.com/{FIRMWARE_REPO}/releases/download/"

# The public half of the signing keypair. Public keys are not secrets: this one is
# here to be read, and shipping it inside the exe is the whole point. The private
# half lives only in the FIRMWARE_SIGNING_KEY Actions secret on the private repo.
SIGNING_PUBLIC_KEY_HEX = "b4a3d7eada0680c07211fbc3b84bffb95732d3162aabf95d0eb39da7505d30b4"

# "experimental" is a prerelease *production* build: same firmware, published before
# it is blessed for the fleet. It is NOT the HIL test image, which is never published
# to any channel, ever. Conflating the two is exactly the accident every guardrail in
# docs/firmware-build.md exists to prevent.
CHANNEL_STABLE = "stable"
CHANNEL_EXPERIMENTAL = "experimental"
CHANNELS = (CHANNEL_STABLE, CHANNEL_EXPERIMENTAL)

SCHEMA_VERSION = 1
SIGNATURE_LENGTH = 64  # raw Ed25519

# Bounds on anything we read off the network, so a hostile or broken endpoint cannot
# spend the tech's disk or memory. A real .uf2 is around 200 KB.
MAX_MANIFEST_BYTES = 256 * 1024
MAX_BUILD_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024

_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class ManifestError(Exception):
    """Anything that stopped us producing a trustworthy list or a verified file.

    One type for network, signature, schema and digest failures on purpose: from the
    caller's side they all mean the same thing, which is that this path cannot be
    used and the tech should browse to a local file instead. The message carries the
    detail.
    """


@dataclass(frozen=True)
class Build:
    version: str
    channel: str
    url: str
    sha256: str
    size: int
    released: str

    @property
    def local_filename(self) -> str:
        """The name to save this build under, built from fields we already validated.

        Never taken from the URL. A filename lifted out of a remote string is a path
        traversal waiting to happen, and there is no reason to accept one when the
        version and channel already say everything the name needs to.

        Prerelease builds get the channel in the name so downloading experimental
        1.3.0 and stable 1.3.0 cannot silently overwrite each other. Both still match
        flash.FIRMWARE_GLOB, which is correct: both are production images.
        """
        suffix = "" if self.channel == CHANNEL_STABLE else f"_{self.channel}"
        return f"AquaD_Pico_v{self.version}{suffix}.uf2"

    @property
    def label(self) -> str:
        """What the version dropdown shows."""
        return f"{self.version}  ({self.released[:10]})"


def _load_public_key(hex_key: str) -> Ed25519PublicKey:
    """Import cryptography lazily so a stale venv costs the version picker, not the app.

    main.py imports this module at startup. A top-level cryptography import would mean
    that anyone whose environment predates it gets a window that will not open at all,
    including for the local-file flash path that needs none of this.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ManifestError(
            "The 'cryptography' package is not installed, so the published firmware "
            "list cannot be verified and will not be used. Install it with "
            "'pip install -r app/requirements.txt', or use 'Load .uf2 file...' to "
            "flash a file you already have."
        ) from exc

    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))
    except ValueError as exc:
        raise ManifestError(f"Built-in signing key is not a valid Ed25519 key: {exc}") from exc


def verify_manifest(raw: bytes, signature: bytes) -> None:
    """Raise ManifestError unless `signature` is our signature over exactly `raw`.

    Pure, and deliberately takes bytes rather than a URL: the bytes that get verified
    have to be the same object the caller goes on to parse. Re-fetching or re-encoding
    between the two would make the signature cover something other than what we use.
    """
    if len(signature) != SIGNATURE_LENGTH:
        raise ManifestError(
            f"Firmware list signature is {len(signature)} bytes, expected "
            f"{SIGNATURE_LENGTH}. The download was truncated or the file is not a "
            "signature."
        )

    from cryptography.exceptions import InvalidSignature

    key = _load_public_key(SIGNING_PUBLIC_KEY_HEX)
    try:
        key.verify(signature, raw)
    except InvalidSignature as exc:
        raise ManifestError(
            "The published firmware list failed its signature check, so it was not "
            "used. Either it was altered in transit or it was not signed by Caddis. "
            "Do not work around this: use 'Load .uf2 file...' with an image you "
            "already trust, and report it."
        ) from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(f"Published firmware list is malformed: {message}")


def _build_from_entry(entry: Any) -> Build:
    _require(isinstance(entry, dict), f"expected an object per build, got {type(entry).__name__}")

    version = entry.get("version")
    channel = entry.get("channel")
    url = entry.get("url")
    sha256 = entry.get("sha256")
    size = entry.get("size")
    released = entry.get("released")

    # A plain three-number version is what the root VERSION file is pinned to
    # (tests/test_release_consistency.py), and refusing anything else here means a
    # version carrying the +HIL marker cannot even be listed, let alone selected.
    _require(
        isinstance(version, str) and _SEMVER.fullmatch(version) is not None,
        f"version {version!r} is not a plain x.y.z version",
    )
    _require(channel in CHANNELS, f"unknown channel {channel!r}")
    # Constrain what the app is willing to fetch to our own release storage. The
    # signature already covers this field, so this guards the case where the thing
    # generating manifests is what went wrong.
    _require(
        isinstance(url, str) and url.startswith(RELEASE_URL_PREFIX),
        f"download URL {url!r} is not under {RELEASE_URL_PREFIX}",
    )
    _require(
        isinstance(sha256, str) and _SHA256_HEX.fullmatch(sha256) is not None,
        f"sha256 {sha256!r} is not 64 lowercase hex characters",
    )
    _require(
        isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_BUILD_BYTES,
        f"size {size!r} is not a sensible byte count",
    )
    _require(
        isinstance(released, str) and _TIMESTAMP.fullmatch(released) is not None,
        f"released {released!r} is not an ISO 8601 UTC timestamp",
    )

    assert isinstance(version, str) and isinstance(channel, str)  # noqa: S101 - narrows for mypy
    assert isinstance(url, str) and isinstance(sha256, str) and isinstance(released, str)
    assert isinstance(size, int)
    return Build(
        version=version, channel=channel, url=url, sha256=sha256, size=size, released=released
    )


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def parse_manifest(raw: bytes, channel: str) -> tuple[Build, ...]:
    """Validated builds for one channel, newest version first. Pure.

    Every field is checked rather than trusted. The signature proves the bytes came
    from us, not that whatever produced them was working correctly, and this is the
    last point before a URL gets fetched and a file gets flashed.
    """
    _require(channel in CHANNELS, f"asked for unknown channel {channel!r}")

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Published firmware list is not valid JSON: {exc}") from exc

    _require(isinstance(document, dict), "top level is not an object")
    schema = document.get("schema")
    if schema != SCHEMA_VERSION:
        raise ManifestError(
            f"The published firmware list uses format {schema!r} and this app "
            f"understands {SCHEMA_VERSION}. Download a newer DroneSetup.exe."
        )

    entries = document.get("builds")
    _require(isinstance(entries, list), "'builds' is not a list")

    builds = [_build_from_entry(entry) for entry in entries]
    matching = [b for b in builds if b.channel == channel]
    matching.sort(key=lambda b: (_version_key(b.version), b.released), reverse=True)
    return tuple(matching)


def _fetch(url: str, limit: int, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "DroneSetup"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            # Read one byte past the cap so an over-long body is detectable rather
            # than silently truncated into something that looks well-formed.
            body: bytes = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        # 404 is what a tech sees before the first firmware is ever published, and it
        # is just as much of a dead end for them as being offline. Say the same thing.
        raise ManifestError(
            f"Could not fetch {url}: the server answered {exc.code} {exc.reason}. "
            "Use 'Load .uf2 file...' to flash a file you already have."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ManifestError(
            f"Could not reach {url}: {exc}. Check the network, or use "
            "'Load .uf2 file...' to flash a file you already have."
        ) from exc

    if len(body) > limit:
        raise ManifestError(f"{url} returned more than the {limit} bytes we allow.")
    return body


def fetch_manifest(timeout: float = 15.0) -> tuple[bytes, bytes]:
    """The raw manifest bytes and the raw signature bytes, unverified. Thin I/O."""
    raw = _fetch(MANIFEST_URL, MAX_MANIFEST_BYTES, timeout)
    signature_hex = _fetch(SIGNATURE_URL, SIGNATURE_LENGTH * 4, timeout)
    try:
        signature = bytes.fromhex(signature_hex.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"Signature file at {SIGNATURE_URL} is not hex: {exc}") from exc
    return raw, signature


def load_builds(channel: str, timeout: float = 15.0) -> tuple[Build, ...]:
    """Fetch, verify, parse. The one call the UI makes."""
    raw, signature = fetch_manifest(timeout)
    verify_manifest(raw, signature)
    return parse_manifest(raw, channel)


ProgressCallback = Callable[[int, int], None]


def stream_verified(
    reader: Any, build: Build, dest: Path, progress_cb: ProgressCallback | None = None
) -> None:
    """Copy `reader` into `dest`, refusing anything that is not exactly this build.

    Takes an already-open reader so the checking half is testable with a BytesIO and
    no network, the same way evaluate_telemetry() is testable with no Pico.

    Length is enforced as it streams rather than after: a response that never ends
    would otherwise fill the disk before we got to compare digests.
    """
    digest = hashlib.sha256()
    received = 0

    with dest.open("wb") as out:
        while True:
            chunk = reader.read(_CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if received > build.size:
                raise ManifestError(
                    f"Firmware download for {build.version} is longer than the "
                    f"{build.size} bytes the signed list says it should be. Stopped."
                )
            digest.update(chunk)
            out.write(chunk)
            if progress_cb is not None:
                progress_cb(received, build.size)

    if received != build.size:
        raise ManifestError(
            f"Firmware download for {build.version} ended early: got {received} bytes "
            f"of {build.size}."
        )
    actual = digest.hexdigest()
    if actual != build.sha256:
        raise ManifestError(
            f"Firmware download for {build.version} does not match the signed "
            f"checksum (got {actual}, expected {build.sha256}). The file was "
            "discarded and nothing was flashed."
        )


def download_build(
    build: Build,
    dest_dir: Path,
    progress_cb: ProgressCallback | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download one build and return the path, or raise ManifestError.

    Writes to a .part file and renames only once the digest matches, so the path this
    returns has never held unverified bytes. A tech who comes back to a folder after
    a failed download finds nothing that looks flashable.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / build.local_filename
    partial = final.with_name(final.name + ".part")

    request = urllib.request.Request(build.url, headers={"User-Agent": "DroneSetup"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            stream_verified(response, build, partial, progress_cb)
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ManifestError(
            f"Could not download {build.url}: the server answered {exc.code} "
            f"{exc.reason}."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise ManifestError(f"Could not download {build.url}: {exc}.") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.replace(final)
    return final
