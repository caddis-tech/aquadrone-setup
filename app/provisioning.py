"""Decide whether a boat's MANTA Link is right, and make it right.

Two halves. `audit()` is pure: it takes what a boat reports and what
extension_settings reads out of the image, and answers with findings. Nothing in
it touches the network, so every state below is testable without hardware, and a
tech can run it against a boat in service knowing it cannot change anything.
`apply_plan()` is the thin half that acts on those findings.

The audit reports present, absent, and wrong as three separate answers, and says
which. A boat installed with BlueOS's Custom settings box left empty is *present*
and *enabled* and running the *right tag*: everything a glance would check looks
correct. What is wrong is that Kraken stored `{}` for its permissions, so the
container has no /dev bind, no host networking, and no persistent volume. MANTA
Link then reports "no Pico present", "Connection refused", and "no API token
configured" all at once, and none of those three names the cause. Naming it is
the point of this module, so every finding below is stated out loud even when it
passes: an audit that prints nothing on a broken boat is worse than no audit.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import extension_settings
import kraken
from kraken import InstalledExtension

# What to do about what the audit found. A reinstall sends the whole
# ExtensionSource body, so it fixes permissions, image and tag together and
# leaves the extension enabled; an update only moves the tag.
ACTION_INSTALL = "install"
ACTION_REINSTALL = "reinstall"
ACTION_UPDATE = "update"
ACTION_ENABLE = "enable"

# Stable names for what was checked, so tests and logs refer to the same thing.
CHECK_PRESENT = "installed"
CHECK_IMAGE = "image"
CHECK_PERMISSIONS = "permissions"
CHECK_VERSION = "version"
CHECK_ENABLED = "enabled"

_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Finding:
    """One checked thing, and what a tech should be told about it."""

    check: str
    ok: bool
    message: str

    @property
    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} - {self.check}: {self.message}"


@dataclass(frozen=True)
class AuditReport:
    extension: InstalledExtension | None
    findings: tuple[Finding, ...]
    actions: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(finding.line for finding in self.findings)

    @property
    def summary(self) -> str:
        if self.ok:
            return "MANTA Link is installed, current, and correctly configured."
        failed = [finding.check for finding in self.findings if not finding.ok]
        return f"MANTA Link needs attention: {', '.join(failed)}."


def audit(extension: InstalledExtension | None) -> AuditReport:
    """Compare one boat's MANTA Link against the image. Pure.

    Takes None for "not installed" rather than a separate call, because absent is
    one of the three answers this exists to distinguish, not an error.
    """
    if extension is None:
        return AuditReport(
            None,
            (
                Finding(
                    CHECK_PRESENT,
                    False,
                    f"{extension_settings.EXTENSION_NAME} is not installed on this vehicle.",
                ),
            ),
            (ACTION_INSTALL,),
        )

    findings = (
        Finding(CHECK_PRESENT, True, f"installed as {extension.identifier}."),
        _image_finding(extension),
        _permissions_finding(extension),
        _version_finding(extension),
        _enabled_finding(extension),
    )
    return AuditReport(extension, findings, _actions_for(findings))


def _image_finding(extension: InstalledExtension) -> Finding:
    """Which image the boat is actually running.

    Worth its own check because the extension this replaced used the same slot:
    a boat still pointed at the retired bridge is installed, enabled, and running
    a real tag, and every other check here would pass on it.
    """
    expected = extension_settings.DOCKER_IMAGE
    if extension.docker == expected:
        return Finding(CHECK_IMAGE, True, expected)
    return Finding(
        CHECK_IMAGE,
        False,
        f"the boat runs {extension.docker}, not {expected}. Reinstalling replaces it.",
    )


def _permissions_finding(extension: InstalledExtension) -> Finding:
    try:
        actual = extension.effective_settings()
    except kraken.KrakenError as exc:
        return Finding(CHECK_PERMISSIONS, False, str(exc))

    expected = extension_settings.read_docker_permissions()

    # Both branches below grant the container nothing and produce the same three
    # symptoms, but they are different mistakes and get different words. Calling
    # a settings block that holds only an Env array "empty" sends a tech looking
    # for something that is not there.
    with_nothing = "; ".join(_consequences({})) + ". Reinstalling writes the real block."
    if not actual:
        # The empty-permissions failure. Everything else about the boat can look
        # right, so this message has to carry the symptoms with it.
        return Finding(
            CHECK_PERMISSIONS,
            False,
            "stored empty. Kraken does not fall back to the image's own permissions "
            f"LABEL, so the container starts with {with_nothing}",
        )

    actual_host = actual.get("HostConfig")
    if not isinstance(actual_host, dict) or not actual_host:
        return Finding(
            CHECK_PERMISSIONS,
            False,
            "stored with no HostConfig in them, which grants the container nothing: it "
            f"starts with {with_nothing}",
        )

    differences = _differences(actual_host, expected["HostConfig"])
    if not differences:
        return Finding(CHECK_PERMISSIONS, True, "match the block the image declares, exactly.")

    broken = _consequences(actual_host)
    message = f"differ from the block the image declares. {'; '.join(differences)}."
    if broken:
        message += f" On this boat that means {'; '.join(broken)}."
    return Finding(CHECK_PERMISSIONS, False, message)


def _normalized(host_config: dict) -> dict:
    """A HostConfig comparable by value. Bind order carries no meaning to Docker,
    and a boat whose binds were stored in another order is not misconfigured."""
    binds = host_config.get("Binds")
    if isinstance(binds, list):
        return {**host_config, "Binds": sorted(str(bind) for bind in binds)}
    return dict(host_config)


def _differences(actual: dict, expected: dict) -> tuple[str, ...]:
    """Every key where the boat and the image disagree, named individually.

    Only the keys the image declares are compared, plus any extra the boat has
    under HostConfig. Env lives a level up and is deliberately never compared or
    quoted: it is where a boat's API token sits.
    """
    left, right = _normalized(actual), _normalized(expected)
    return tuple(
        f"{key}: boat has {json.dumps(left.get(key))}, image declares "
        f"{json.dumps(right.get(key))}"
        for key in sorted(set(left) | set(right))
        if left.get(key) != right.get(key)
    )


def _consequences(host_config: dict) -> tuple[str, ...]:
    """What a tech will see on the boat, given the access this HostConfig grants.

    The three symptoms MANTA Link reports are each caused by a different missing
    piece, and none of them names the piece. Saying which is missing next to what
    it breaks is what turns "three unrelated bugs" back into one cause.
    """
    binds = [str(bind) for bind in host_config.get("Binds") or []]
    broken = []
    if not any(bind.startswith("/dev:") for bind in binds):
        broken.append("no /dev bind, so MANTA Link reports 'no Pico present'")
    if host_config.get("NetworkMode") != "host":
        broken.append("no host networking, so it reports 'Connection refused' to mavlink2rest")
    if not any(":/app/data" in bind for bind in binds):
        broken.append(
            "no persistent volume, so it reports 'no API token configured' even when the "
            "boat has one"
        )
    if not host_config.get("Privileged"):
        broken.append("no privileged access, so it cannot open the Pico's serial device")
    return tuple(broken)


def _semver(tag: str) -> tuple[int, ...] | None:
    match = _SEMVER.fullmatch(tag.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _version_finding(extension: InstalledExtension) -> Finding:
    expected = extension_settings.read_extension_version()
    if extension.tag == expected:
        return Finding(CHECK_VERSION, True, f"running {expected}, the current tag.")

    installed, current = _semver(extension.tag), _semver(expected)
    if installed is None or current is None:
        direction = "which this tool cannot compare to"
    else:
        direction = "which is newer than" if installed > current else "which is older than"
    return Finding(
        CHECK_VERSION,
        False,
        f"running {extension.tag}, {direction} the {expected} this tool installs.",
    )


def _enabled_finding(extension: InstalledExtension) -> Finding:
    if extension.enabled:
        return Finding(CHECK_ENABLED, True, "enabled.")
    return Finding(
        CHECK_ENABLED,
        False,
        "disabled, so the boat uploads nothing. Nothing about the container is wrong; "
        "it is not running.",
    )


def _actions_for(findings: tuple[Finding, ...]) -> tuple[str, ...]:
    """What to do about the findings, in the order to do it.

    Anything wrong with the image or its permissions needs the full body, which
    carries the right tag and leaves the extension enabled, so a reinstall is the
    whole plan. Only when those are already correct is the tag-only update safe:
    that endpoint takes no body and so cannot repair permissions.
    """
    failed = {finding.check for finding in findings if not finding.ok}
    if CHECK_IMAGE in failed or CHECK_PERMISSIONS in failed:
        return (ACTION_REINSTALL,)

    actions = []
    if CHECK_VERSION in failed:
        actions.append(ACTION_UPDATE)
    if CHECK_ENABLED in failed:
        actions.append(ACTION_ENABLE)
    return tuple(actions)


def describe_actions(actions: tuple[str, ...]) -> str:
    """What the tech is about to authorise, in plain words."""
    tag = extension_settings.read_extension_version()
    wording = {
        ACTION_INSTALL: f"Install {extension_settings.EXTENSION_NAME} {tag}",
        ACTION_REINSTALL: (
            f"Reinstall {extension_settings.EXTENSION_NAME} {tag} with the full "
            "permissions block"
        ),
        ACTION_UPDATE: f"Update to {tag}",
        ACTION_ENABLE: "Enable the extension",
    }
    return "; ".join(wording[action] for action in actions) if actions else "Nothing to do"


LogCallback = Callable[[str], None]


def _existing_env(extension: InstalledExtension | None, log: LogCallback) -> list[str]:
    """The Env the boat already has, to be sent back with the reinstall.

    Kraken has no partial update, so anything not carried forward is lost, and on
    a hand-provisioned boat that includes its API token. The count is logged and
    the values are not: this is the one place a credential passes through, and it
    passes through unread.
    """
    if extension is None:
        return []
    try:
        env = extension.effective_settings().get("Env") or []
    except kraken.KrakenError:
        log(
            "  WARNING: the boat's stored settings could not be read, so any "
            "environment variables in them cannot be carried forward. If this boat "
            "had a token in its Env rather than its .env file, set it again after this."
        )
        return []

    if env:
        log(f"  Carrying forward {len(env)} environment variable(s) already on the boat.")
    return [str(entry) for entry in env]


def apply_plan(host: str, report: AuditReport, log: LogCallback) -> None:
    """Run the audit's plan against a boat. Changes the vehicle.

    Deliberately driven by the report rather than by its own checks, so what runs
    is exactly what the tech was shown and agreed to.
    """
    identifier = extension_settings.EXTENSION_IDENTIFIER
    tag = extension_settings.read_extension_version()

    for action in report.actions:
        if action in (ACTION_INSTALL, ACTION_REINSTALL):
            log(f"  {describe_actions((action,))}. Pulling the image can take a few minutes...")
            source = extension_settings.build_extension_source(
                _existing_env(report.extension, log)
            )
            kraken.install_extension(host, source)
        elif action == ACTION_UPDATE:
            log(f"  Updating {identifier} to {tag}...")
            kraken.update_extension_to_version(host, identifier, tag)
        elif action == ACTION_ENABLE:
            log(f"  Enabling {identifier}...")
            kraken.enable_extension(host, identifier)
        else:  # pragma: no cover - unreachable unless a new action is added above
            raise kraken.KrakenError(f"No idea how to carry out {action!r}.")
        log(f"  {action} finished.")
