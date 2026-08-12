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
