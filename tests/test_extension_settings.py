"""Tests for the Drone Setup app's BlueOS Extension settings builder.

No SSH, no filesystem writes to a Pi: this module only computes a JSON blob a
tech copies into BlueOS's control panel. The value tested here is that the
generated settings can never drift from what the image actually ships with, and
that a token always ends up in them.
"""
import json
import re

import extension_settings
import pytest


def test_build_install_settings_embeds_the_token():
    settings = json.loads(extension_settings.build_install_settings("REALTOKEN"))

    assert "CADDIS_API_TOKEN=REALTOKEN" in settings["Env"]


def test_build_install_settings_keeps_the_prod_api_url():
    # There must be no way to hand-edit a drone onto anything but prod.
    settings = json.loads(extension_settings.build_install_settings("tok"))

    assert "CADDIS_API_URL=https://api.caddistech.com" in settings["Env"]


def test_build_install_settings_carries_the_real_dockerfile_permissions():
    # The whole point: a tech pasting this JSON gets the exact permissions the
    # image ships with, not a hand-copied (and driftable) duplicate.
    settings = json.loads(extension_settings.build_install_settings("tok"))

    assert settings["HostConfig"] == extension_settings.read_docker_permissions()["HostConfig"]


def test_read_docker_permissions_grants_dev_and_persistent_volume_access():
    host = extension_settings.read_docker_permissions()["HostConfig"]

    assert host["Privileged"] is True
    assert any(b.startswith("/dev:") for b in host["Binds"])
    assert any(b.startswith("/usr/blueos/extensions/manta-link:") for b in host["Binds"])


def test_read_docker_permissions_uses_host_networking():
    # MANTA Link reads mavlink2rest on 127.0.0.1 and uploads over the host's
    # cellular default route; without this it gets connection refused and looks
    # like the autopilot is down.
    assert extension_settings.read_docker_permissions()["HostConfig"]["NetworkMode"] == "host"


def test_extension_version_is_a_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", extension_settings.read_extension_version())


def test_extension_version_comes_from_the_image_label():
    """The tag must track the extension, not this tool.

    The two versions move independently, so deriving the tag from the repo's
    VERSION file sends a tech to install a tag that was never published. Reading
    the LABEL is what keeps them from being confused for each other.
    """
    dockerfile = (extension_settings.REPO_ROOT / "manta-link" / "Dockerfile").read_text()

    assert f'LABEL version="{extension_settings.read_extension_version()}"' in dockerfile


def test_labels_still_parse_when_the_dockerfile_is_checked_out_crlf(monkeypatch):
    """A CRLF checkout must not look like a missing LABEL.

    `core.autocrlf` is true by default on Windows, including on CI's windows
    runner, so this is what a fresh clone produces without the .gitattributes
    rule. An end-of-line anchored regex then matches after the \\r instead of
    after the quote, and the tool dies saying the LABEL is absent.
    """
    crlf = extension_settings._joined_dockerfile().replace("\n", "\r\n")
    monkeypatch.setattr(extension_settings, "_joined_dockerfile", lambda: crlf)

    assert re.fullmatch(r"\d+\.\d+\.\d+", extension_settings.read_extension_version())
    assert extension_settings.read_docker_permissions()["HostConfig"]["Privileged"] is True


def test_identifier_and_image_target_manta_link():
    # The bridge this replaced is a dead design; pointing at it installs nothing.
    assert extension_settings.EXTENSION_IDENTIFIER == "caddis.manta-link"
    assert extension_settings.DOCKER_IMAGE == "ghcr.io/caddis-tech/manta-link"


# -- The body Kraken is sent directly ----------------------------------------


def test_the_install_body_carries_every_field_kraken_requires():
    # Kraken's ExtensionSource requires all six. A body missing one is a 422 that
    # a tech reads as "the boat refused", with nothing to act on.
    source = extension_settings.build_extension_source()

    assert set(source) >= {"identifier", "tag", "name", "docker", "enabled", "permissions"}
    assert source["identifier"] == "caddis.manta-link"
    assert source["tag"] == extension_settings.read_extension_version()
    assert source["enabled"] is True


def test_neither_permissions_field_is_ever_installed_empty():
    """The failure class, closed at the source.

    Kraken starts the container from `user_permissions` whenever that is set to
    anything at all, `{}` included, and falls back to `permissions` only when it
    is genuinely unset. Filling both means no boat this tool installs can come up
    with no /dev bind, no host networking and no persistent volume.
    """
    source = extension_settings.build_extension_source()

    for field in ("permissions", "user_permissions"):
        assert json.loads(source[field])["HostConfig"] == (
            extension_settings.read_docker_permissions()["HostConfig"]
        )


def test_the_manifest_permissions_field_carries_no_credential():
    # Only user_permissions needs Env. Writing a boat's token into a second field
    # that nothing reads is one more place it can leak from.
    source = extension_settings.build_extension_source(["CADDIS_API_TOKEN=s3cret"])

    assert "Env" not in json.loads(source["permissions"])
    assert "s3cret" not in source["permissions"]


def test_installing_states_the_production_api_url():
    env = json.loads(extension_settings.build_extension_source()["user_permissions"])["Env"]

    assert "CADDIS_API_URL=https://api.caddistech.com" in env


def test_a_reinstall_keeps_the_token_the_boat_already_had():
    # Kraken has no partial update: an Env variable this body omits is one the
    # container loses. Repairing a boat's permissions must not cost it its token.
    source = extension_settings.build_extension_source(["CADDIS_API_TOKEN=s3cret"])

    assert "CADDIS_API_TOKEN=s3cret" in json.loads(source["user_permissions"])["Env"]


def test_a_boat_left_pointed_at_a_bench_endpoint_is_put_back_on_production():
    # It would otherwise upload nowhere anyone is looking, while reporting
    # perfect health.
    env = extension_settings.merge_env(["CADDIS_API_URL=http://bench.local:8000"])

    assert env == ["CADDIS_API_URL=https://api.caddistech.com"]


def test_env_that_is_not_a_name_value_pair_is_refused_rather_than_sent_back():
    with pytest.raises(ValueError):
        extension_settings.merge_env(["JUST_A_NAME"])


# -- The token a tech pastes -------------------------------------------------

# Made up, and shaped like a caddis-api device token: 40 hex characters.
PASTED_TOKEN = "0123456789abcdef0123456789abcdef01234567"
PROD_URL = "CADDIS_API_URL=https://api.caddistech.com"


def test_a_pasted_token_replaces_the_boats_and_keeps_every_other_env_entry():
    env = extension_settings.merge_env(
        ["TZ=America/New_York", "CADDIS_API_TOKEN=s3cret", "MANTA_LOG_LEVEL=debug"],
        token=PASTED_TOKEN,
    )

    assert env == [
        "TZ=America/New_York",
        "MANTA_LOG_LEVEL=debug",
        f"CADDIS_API_TOKEN={PASTED_TOKEN}",
        PROD_URL,
    ]


def test_a_pasted_token_does_not_stop_the_boat_being_put_back_on_production():
    env = extension_settings.merge_env(
        ["CADDIS_API_URL=http://bench.local:8000"], token=PASTED_TOKEN
    )

    assert env == [f"CADDIS_API_TOKEN={PASTED_TOKEN}", PROD_URL]


def test_with_no_pasted_token_the_boats_env_is_carried_over_unchanged():
    existing = ["TZ=America/New_York", "CADDIS_API_TOKEN=s3cret"]

    assert extension_settings.merge_env(existing) == [*existing, PROD_URL]


def test_the_install_body_carries_a_pasted_token_in_user_permissions_only():
    source = extension_settings.build_extension_source(["CADDIS_API_TOKEN=s3cret"], PASTED_TOKEN)

    env = json.loads(source["user_permissions"])["Env"]
    assert f"CADDIS_API_TOKEN={PASTED_TOKEN}" in env
    assert "CADDIS_API_TOKEN=s3cret" not in env
    assert PASTED_TOKEN not in source["permissions"]


def test_generate_settings_json_still_holds_just_the_token_and_production():
    # The manual path is unchanged by Install / Repair taking the same box.
    settings = json.loads(extension_settings.build_install_settings(PASTED_TOKEN))

    assert settings["Env"] == [f"CADDIS_API_TOKEN={PASTED_TOKEN}", PROD_URL]


def test_a_pasted_token_is_stripped_of_surrounding_whitespace():
    assert extension_settings.validate_token(f"  {PASTED_TOKEN}\r\n") == PASTED_TOKEN


def test_every_character_a_token_may_hold_is_accepted():
    # Hex, base64 and base64url between them. Matched by
    # test_provisioning's redaction test, which proves each one is scrubbed.
    token = "Made.Up_Token~With+Every/Allowed-Char0123456789"

    assert extension_settings.validate_token(token) == token


@pytest.mark.parametrize("pasted", ["", "   ", "\t\r\n"])
def test_an_empty_token_is_refused(pasted):
    with pytest.raises(ValueError, match="empty"):
        extension_settings.validate_token(pasted)


@pytest.mark.parametrize(
    ("pasted", "named"),
    [
        (f"{PASTED_TOKEN[:20]} {PASTED_TOKEN[20:]}", "space"),
        (f"{PASTED_TOKEN[:20]}\t{PASTED_TOKEN[20:]}", "tab"),
        (f"{PASTED_TOKEN[:20]}\n{PASTED_TOKEN[20:]}", "line break"),
        # A no-break space, which copying from a web page can bring along.
        (f"{PASTED_TOKEN[:20]} {PASTED_TOKEN[20:]}", "space"),
        (f"{PASTED_TOKEN[:20]}\x00{PASTED_TOKEN[20:]}", "control character"),
        # A zero-width space survives strip(), being neither whitespace nor visible.
        (f"​{PASTED_TOKEN}", "invisible"),
        (f"CADDIS_API_TOKEN={PASTED_TOKEN}", "="),
        (f"{PASTED_TOKEN}==", "="),
        (f'"{PASTED_TOKEN}"', "quote"),
        (f"{PASTED_TOKEN},", "comma"),
        (f"é{PASTED_TOKEN}", "accented"),
    ],
    ids=[
        "space",
        "tab",
        "line-break",
        "no-break-space",
        "control-character",
        "zero-width-space",
        "whole-env-line",
        "equals-padding",
        "quoted",
        "comma",
        "accented",
    ],
)
def test_a_malformed_token_is_refused_by_name_without_echoing_it(pasted, named):
    with pytest.raises(ValueError) as caught:
        extension_settings.validate_token(pasted)

    message = str(caught.value)
    assert named in message
    assert PASTED_TOKEN[:20] not in message
    assert PASTED_TOKEN[20:] not in message


@pytest.mark.parametrize("box", ["", "   ", "\r\n"])
def test_an_empty_token_box_sends_no_token(box):
    assert extension_settings.pasted_token(box) is None


def test_a_filled_token_box_is_validated_before_it_is_sent():
    assert extension_settings.pasted_token(f" {PASTED_TOKEN} ") == PASTED_TOKEN
    with pytest.raises(ValueError):
        extension_settings.pasted_token(f"CADDIS_API_TOKEN={PASTED_TOKEN}")


def test_the_fingerprint_is_the_first_8_hex_characters_of_the_sha256():
    # SHA-256 of "abc" is the published test vector ba7816bf..., so this pins the
    # definition MANTA Link logs rather than restating the code.
    assert extension_settings.token_fingerprint("abc") == "ba7816bf"
