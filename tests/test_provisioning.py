"""Tests for the audit: present, absent, and wrong as three separate answers.

The failure this suite exists for is a boat that looks fine. Installed, enabled,
running the right tag, and stored with empty permissions, so the container has
no /dev bind, no host networking and no persistent volume, and MANTA Link reports
three unrelated-looking faults that never name the cause. Every "an empty block
is not a pass" test below is one boat that does not go out broken.

Pure throughout: audit() takes what a boat said and never touches the network, so
these run with no hardware and the audit itself is safe on a boat in service.
"""
from __future__ import annotations

import json

import extension_settings
import kraken
import provisioning
import pytest


def _boat(**overrides) -> kraken.InstalledExtension:
    """One installed MANTA Link, correct in every respect unless overridden."""
    fields = {
        "identifier": extension_settings.EXTENSION_IDENTIFIER,
        "name": extension_settings.EXTENSION_NAME,
        "docker": extension_settings.DOCKER_IMAGE,
        "tag": extension_settings.read_extension_version(),
        "enabled": True,
        "permissions_raw": "{}",
        "user_permissions_raw": json.dumps(extension_settings.read_docker_permissions()),
    }
    fields.update(overrides)
    return kraken.InstalledExtension(**fields)


def _with_host_config(**changes) -> str:
    permissions = extension_settings.read_docker_permissions()
    return json.dumps({"HostConfig": {**permissions["HostConfig"], **changes}})


def _finding(report: provisioning.AuditReport, check: str) -> provisioning.Finding:
    return next(f for f in report.findings if f.check == check)


# -- The three answers -------------------------------------------------------


def test_a_correctly_configured_boat_passes_with_nothing_to_do():
    report = provisioning.audit(_boat())

    assert report.ok
    assert report.actions == ()


def test_an_absent_extension_is_reported_as_absent_not_as_a_failure_to_read():
    report = provisioning.audit(None)

    assert not report.ok
    assert "not installed" in _finding(report, provisioning.CHECK_PRESENT).message
    assert report.actions == (provisioning.ACTION_INSTALL,)


def test_empty_permissions_are_a_distinct_answer_from_absent():
    # Both are "this boat is not working", and they need different fixes and
    # different words. Collapsing them is how a tech reinstalls a boat that was
    # already installed and wonders why nothing changed.
    absent = provisioning.audit(None)
    empty = provisioning.audit(_boat(user_permissions_raw="{}"))

    assert _finding(absent, provisioning.CHECK_PRESENT).ok is False
    assert _finding(empty, provisioning.CHECK_PRESENT).ok is True
    assert _finding(empty, provisioning.CHECK_PERMISSIONS).ok is False


# -- Empty permissions, the failure this exists to kill -----------------------


@pytest.mark.parametrize("stored", ["{}", "", "   "])
def test_a_boat_with_no_effective_permissions_never_passes(stored):
    report = provisioning.audit(_boat(permissions_raw="{}", user_permissions_raw=stored))

    assert not report.ok
    assert not _finding(report, provisioning.CHECK_PERMISSIONS).ok


def test_a_boat_with_empty_permissions_passes_every_other_check():
    """Which is exactly why it needs its own check: nothing else catches it."""
    report = provisioning.audit(_boat(user_permissions_raw="{}"))

    passed = {f.check for f in report.findings if f.ok}
    assert passed == {
        provisioning.CHECK_PRESENT,
        provisioning.CHECK_IMAGE,
        provisioning.CHECK_VERSION,
        provisioning.CHECK_ENABLED,
    }


def test_the_empty_permissions_message_names_all_three_symptoms():
    # A tech reading the boat's own logs sees three faults that name nothing.
    # This is the line that connects them to one cause.
    report = provisioning.audit(_boat(user_permissions_raw="{}"))

    message = _finding(report, provisioning.CHECK_PERMISSIONS).message
    assert "no Pico present" in message
    assert "Connection refused" in message
    assert "no API token configured" in message


def test_settings_holding_only_an_env_are_not_called_empty():
    # A different mistake with the same consequences: someone pasted the token
    # half of the settings JSON and nothing else. Telling them it is "empty"
    # sends them looking for something that is not what went wrong.
    report = provisioning.audit(
        _boat(user_permissions_raw=json.dumps({"Env": ["CADDIS_API_URL=x"]}))
    )

    message = _finding(report, provisioning.CHECK_PERMISSIONS).message
    assert not _finding(report, provisioning.CHECK_PERMISSIONS).ok
    assert "no HostConfig" in message
    assert "no Pico present" in message


def test_empty_permissions_are_repaired_by_a_reinstall_not_an_update():
    # update_to_version takes no body, so it cannot write permissions at all.
    report = provisioning.audit(_boat(user_permissions_raw="{}"))

    assert report.actions == (provisioning.ACTION_REINSTALL,)


# -- Wrong permissions -------------------------------------------------------


def test_host_networking_removed_is_caught_and_named():
    report = provisioning.audit(
        _boat(user_permissions_raw=_with_host_config(NetworkMode="bridge"))
    )

    message = _finding(report, provisioning.CHECK_PERMISSIONS).message
    assert not _finding(report, provisioning.CHECK_PERMISSIONS).ok
    assert "NetworkMode" in message
    assert "Connection refused" in message


def test_a_missing_dev_bind_is_caught_and_named():
    without_dev = [
        bind
        for bind in extension_settings.read_docker_permissions()["HostConfig"]["Binds"]
        if not bind.startswith("/dev:")
    ]

    report = provisioning.audit(_boat(user_permissions_raw=_with_host_config(Binds=without_dev)))

    assert "no Pico present" in _finding(report, provisioning.CHECK_PERMISSIONS).message


def test_binds_stored_in_another_order_are_not_a_fault():
    # Order carries no meaning to Docker, and reporting it would train techs to
    # ignore this check.
    reversed_binds = list(
        reversed(extension_settings.read_docker_permissions()["HostConfig"]["Binds"])
    )

    report = provisioning.audit(_boat(user_permissions_raw=_with_host_config(Binds=reversed_binds)))

    assert report.ok


def test_settings_that_cannot_be_read_are_a_finding_rather_than_a_crash():
    report = provisioning.audit(_boat(user_permissions_raw="{not json"))

    assert not _finding(report, provisioning.CHECK_PERMISSIONS).ok
    assert report.actions == (provisioning.ACTION_REINSTALL,)


def test_an_extension_installed_from_the_blueos_manifest_still_audits():
    # The stock arrangement: nothing custom, everything in `permissions`.
    report = provisioning.audit(
        _boat(
            permissions_raw=json.dumps(extension_settings.read_docker_permissions()),
            user_permissions_raw="",
        )
    )

    assert report.ok


# -- Version -----------------------------------------------------------------


def test_an_older_tag_is_drift_and_says_which_direction():
    report = provisioning.audit(_boat(tag="0.1.0"))

    finding = _finding(report, provisioning.CHECK_VERSION)
    assert not finding.ok
    assert "older" in finding.message
    assert report.actions == (provisioning.ACTION_UPDATE,)


def test_a_newer_tag_is_reported_rather_than_quietly_downgraded():
    # A boat ahead of this exe is a real situation, and rolling it back silently
    # is the wrong answer. Say so and let the tech decide.
    report = provisioning.audit(_boat(tag="99.0.0"))

    assert "newer" in _finding(report, provisioning.CHECK_VERSION).message


def test_a_tag_that_is_not_a_version_is_reported_not_assumed_current():
    report = provisioning.audit(_boat(tag="latest"))

    finding = _finding(report, provisioning.CHECK_VERSION)
    assert not finding.ok
    assert "cannot compare" in finding.message


def test_version_drift_alone_does_not_trigger_a_reinstall():
    # The permissions are already right; sending the whole body would risk
    # replacing them for no reason.
    report = provisioning.audit(_boat(tag="0.1.0"))

    assert report.actions == (provisioning.ACTION_UPDATE,)


# -- The rest ----------------------------------------------------------------


def test_a_disabled_extension_is_a_failure_with_its_own_fix():
    report = provisioning.audit(_boat(enabled=False))

    assert not report.ok
    assert report.actions == (provisioning.ACTION_ENABLE,)


def test_a_boat_still_running_the_retired_bridge_image_is_caught():
    # It is installed, enabled and on a real tag: every other check passes.
    report = provisioning.audit(_boat(docker="ghcr.io/caddis-tech/aquadrone-bridge"))

    assert not _finding(report, provisioning.CHECK_IMAGE).ok
    assert report.actions == (provisioning.ACTION_REINSTALL,)


def test_a_broken_boat_is_fixed_in_one_pass():
    # Wrong permissions and an old tag: the reinstall body carries the right tag,
    # so a second action would be redundant work on a boat mid-job.
    report = provisioning.audit(_boat(tag="0.1.0", user_permissions_raw="{}"))

    assert report.actions == (provisioning.ACTION_REINSTALL,)


def test_every_check_is_stated_even_when_it_passes():
    # An audit that prints only failures leaves a tech unsure whether the check
    # ran at all.
    report = provisioning.audit(_boat())

    assert len(report.lines) == 5
    assert all(line.startswith("PASS - ") for line in report.lines)


def test_a_failing_line_is_marked_so_it_can_be_found_by_eye():
    report = provisioning.audit(_boat(enabled=False))

    assert any(line.startswith("FAIL - ") for line in report.lines)


# -- Never showing a token ---------------------------------------------------


def test_no_report_line_can_contain_a_boats_token():
    """Env is where a hand-provisioned boat keeps its API token, and the audit
    quotes stored settings back at the tech. It compares HostConfig only."""
    with_token = json.dumps(
        {
            "Env": ["CADDIS_API_TOKEN=s3cret"],
            "HostConfig": {"NetworkMode": "bridge"},
        }
    )

    report = provisioning.audit(_boat(user_permissions_raw=with_token))

    assert not report.ok
    assert "s3cret" not in " ".join(report.lines)


# -- Carrying out the plan ---------------------------------------------------


class _RecordingKraken:
    """Stands in for the real client so the plan can be run without a boat."""

    def __init__(self):
        self.calls = []

    def install_extension(self, host, source, timeout=None):
        self.calls.append(("install", host, source))

    def update_extension_to_version(self, host, identifier, version, timeout=None):
        self.calls.append(("update", host, identifier, version))

    def enable_extension(self, host, identifier, timeout=None):
        self.calls.append(("enable", host, identifier))


@pytest.fixture
def fake_kraken(monkeypatch):
    recorder = _RecordingKraken()
    for name in ("install_extension", "update_extension_to_version", "enable_extension"):
        monkeypatch.setattr(provisioning.kraken, name, getattr(recorder, name))
    return recorder


def test_an_install_sends_the_full_permissions_block(fake_kraken):
    # A partial block is how the empty-permissions failure happens in the first
    # place, so there is no code path here that sends anything less.
    provisioning.apply_plan("blueos.local", provisioning.audit(None), lambda _: None)

    _, _, source = fake_kraken.calls[0]
    assert json.loads(source["user_permissions"])["HostConfig"] == (
        extension_settings.read_docker_permissions()["HostConfig"]
    )


def test_a_reinstall_carries_the_boats_existing_env_back_to_it(fake_kraken):
    """Kraken has no partial update: what the body omits, the container loses.
    On a hand-provisioned boat that includes the API token."""
    broken = _boat(
        user_permissions_raw=json.dumps(
            {"Env": ["CADDIS_API_TOKEN=s3cret"], "HostConfig": {"NetworkMode": "bridge"}}
        )
    )

    provisioning.apply_plan("blueos.local", provisioning.audit(broken), lambda _: None)

    (_, _, source) = fake_kraken.calls[0]
    assert "CADDIS_API_TOKEN=s3cret" in json.loads(source["user_permissions"])["Env"]


def test_carrying_env_forward_never_logs_its_values(fake_kraken):
    broken = _boat(
        user_permissions_raw=json.dumps(
            {"Env": ["CADDIS_API_TOKEN=s3cret"], "HostConfig": {}}
        )
    )
    logged = []

    provisioning.apply_plan("blueos.local", provisioning.audit(broken), logged.append)

    assert "s3cret" not in " ".join(logged)
    assert any("Carrying forward 1" in line for line in logged)


def test_an_unreadable_env_is_flagged_rather_than_silently_dropped(fake_kraken):
    # The reinstall still has to happen, but a token that was in there is gone
    # and the tech has to know to set it again.
    logged = []

    provisioning.apply_plan(
        "blueos.local", provisioning.audit(_boat(user_permissions_raw="{oops")), logged.append
    )

    assert any("WARNING" in line for line in logged)


def test_a_tag_only_update_does_not_touch_permissions(fake_kraken):
    provisioning.apply_plan(
        "blueos.local", provisioning.audit(_boat(tag="0.1.0")), lambda _: None
    )

    assert fake_kraken.calls == [
        (
            "update",
            "blueos.local",
            extension_settings.EXTENSION_IDENTIFIER,
            extension_settings.read_extension_version(),
        )
    ]


def test_a_healthy_boat_is_left_alone(fake_kraken):
    provisioning.apply_plan("blueos.local", provisioning.audit(_boat()), lambda _: None)

    assert fake_kraken.calls == []


def test_the_tech_is_told_what_they_are_authorising():
    described = provisioning.describe_actions((provisioning.ACTION_REINSTALL,))

    assert extension_settings.read_extension_version() in described
    assert "permissions" in described


def test_nothing_to_do_says_so():
    assert provisioning.describe_actions(()) == "Nothing to do"
