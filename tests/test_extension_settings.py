"""Tests for the Drone Setup app's BlueOS Extension settings builder.

No SSH, no filesystem writes to a Pi — this module only computes a JSON blob
a tech copies into BlueOS's control panel. The value tested here is that the
generated settings can never drift from what the image actually ships with,
and that a token always ends up in them.
"""
import json

import extension_settings


def test_build_install_settings_embeds_the_token():
    settings = json.loads(extension_settings.build_install_settings("REALTOKEN"))

    assert "CADDIS_API_TOKEN=REALTOKEN" in settings["Env"]


def test_build_install_settings_keeps_the_prod_api_url():
    # There must be no way to hand-edit a drone onto anything but prod.
    settings = json.loads(extension_settings.build_install_settings("tok"))

    assert "CADDIS_API_URL=https://api.caddistech.com" in settings["Env"]


def test_build_install_settings_carries_the_real_dockerfile_permissions():
    # The whole point: a tech pasting this JSON gets the exact permissions
    # the image ships with, not a hand-copied (and driftable) duplicate.
    settings = json.loads(extension_settings.build_install_settings("tok"))

    assert settings["HostConfig"] == extension_settings.read_docker_permissions()["HostConfig"]


def test_read_docker_permissions_grants_dev_and_persistent_volume_access():
    host = extension_settings.read_docker_permissions()["HostConfig"]

    assert host["Privileged"] is True
    assert any(b.startswith("/dev:") for b in host["Binds"])
    assert any(b.startswith("/usr/blueos/extensions/") for b in host["Binds"])


def test_read_version_matches_the_root_version_file():
    root_version = (extension_settings.REPO_ROOT / "VERSION").read_text().strip()

    assert extension_settings.read_version() == root_version
