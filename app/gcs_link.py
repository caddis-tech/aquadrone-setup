"""Decide whether QGroundControl can reach a boat, and make it so.

The boat listens and the ground station dials it, rather than the boat streaming
at an address baked into it. `GCS Server Link`, udpin on 0.0.0.0:14550, ships
with every BlueOS install and ships disabled; enabling it is the whole of this.
0.0.0.0 binds every interface at once, so that single endpoint serves the
cellular path over ZeroTier, the base station, and anything added later, and the
boat stores no operator's address anywhere. An operator picks a drone by dialling
that drone's address.

Same shape as provisioning.py: audit() is pure and safe against a boat in
service, apply_plan() is the thin half that writes. Three answers, each said out
loud: absent, present and correct, present and pointing somewhere else. The third
is reported and left alone, because a boat someone deliberately moved is a fact
for the operator rather than a conflict for this tool to settle.

Two things it will not do, both deliberate:

- **It never writes an endpoint flagged `protected`.** Three loopback endpoints
  carry GPS into every telemetry record. A boat that loses them keeps capturing,
  keeps uploading and reports healthy while every reading arrives with no
  position, and the only place it shows is `records_without_position` climbing.
  They are checked here and reported, never touched.
- **It never modifies `GCS Client Link`.** The base station path is correct as it
  is: a tech standing next to the boat gets a vehicle in QGC with nothing to
  configure. Nothing here tidies it up just because a laptop can now dial the
  boat directly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import autopilot
import blueos
from autopilot import Endpoint

# The stock endpoint this module exists to turn on, and the one it must not touch.
GCS_SERVER_LINK = "GCS Server Link"
GCS_CLIENT_LINK = "GCS Client Link"

# What GCS Server Link has to be for a ground station to dial the boat.
LISTEN_TYPE = "udpin"
LISTEN_PLACE = "0.0.0.0"
LISTEN_PORT = 14550

# Owner written on an endpoint this tool creates. Stock endpoints all read
# "ardupilot-manager", so a distinct owner says where this one came from. It is
# never compared when judging correctness: a boat whose stock endpoint simply
# needed enabling keeps its stock owner and is entirely correct.
PROVISIONED_OWNER = "aquadrone-setup"

# The loopback endpoints that put a GPS position on every telemetry reading:
# autopilot -> MAVLink2Rest -> mavlink2rest -> MANTA Link polling localhost.
# None of them is a GCS link, which is why moving the GCS path from push to pull
# leaves recording and uploading untouched.
INTERNAL_ENDPOINTS = ("MAVLink2Rest", "MAVLink2RestServer", "Internal Link")

ACTION_CREATE = "create"
ACTION_REPAIR = "repair"

CHECK_PRESENT = "gcs server link"
CHECK_LISTENING = "listening address"
CHECK_ENABLED = "enabled"
CHECK_PERSISTENT = "persistent"
CHECK_INTERNAL = "internal endpoints"


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
    link: Endpoint | None
    desired: Endpoint | None
    findings: tuple[Finding, ...]
    actions: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(finding.line for finding in self.findings)

    @property
    def reachable(self) -> bool:
        """Whether a ground station can dial this boat as it stands.

        Deliberately narrower than `ok`. The loopback check is about telemetry
        rather than the GCS path, so a boat that has lost one still answers
        QGroundControl, and the operator in front of it still needs the address.
        """
        gcs_checks = {CHECK_PRESENT, CHECK_LISTENING, CHECK_ENABLED}
        return all(f.ok for f in self.findings if f.check in gcs_checks)

    @property
    def summary(self) -> str:
        if self.ok:
            return f"{GCS_SERVER_LINK} is enabled and listening. QGroundControl can dial this boat."
        failed = [finding.check for finding in self.findings if not finding.ok]
        return f"This boat needs attention: {', '.join(failed)}."


def audit(endpoints: tuple[Endpoint, ...]) -> AuditReport:
    """Compare a boat's endpoint list against the pull design. Pure.

    Takes the whole list rather than just the one endpoint, because two of the
    answers below are about the rest of it: what else is already on port 14550,
    and whether the loopback endpoints are still there.
    """
    internal = _internal_finding(endpoints)
    link = autopilot.find_endpoint(endpoints, GCS_SERVER_LINK)
    if link is None:
        return _absent_report(endpoints, internal)

    findings = (
        Finding(CHECK_PRESENT, True, f"present as {link.route}."),
        _listening_finding(link),
        _enabled_finding(link),
        _persistent_finding(link),
        internal,
    )
    actions = _actions_for(findings, link)
    # `desired` is filled in only when something is going to be written, so that
    # "the endpoint to write" and "there is nothing to write" cannot disagree. A
    # link pointing somewhere else would otherwise sit here as a ready-made body
    # for anyone who read the field without also reading the actions.
    desired = link.with_state(enabled=True, persistent=True) if actions else None
    return AuditReport(link, desired, findings, actions)


def _absent_report(endpoints: tuple[Endpoint, ...], internal: Finding) -> AuditReport:
    """What to say about a boat with no GCS Server Link at all.

    Creating one is only offered when the port is free. The autopilot manager
    refuses a second endpoint on a port already in use, so proposing it anyway
    would restart MAVLink Router, drop every live connection, and then fail.
    """
    occupant = _occupant(endpoints)
    if occupant is not None:
        return AuditReport(
            None,
            None,
            (
                Finding(
                    CHECK_PRESENT,
                    False,
                    f"missing, and {occupant.name} already holds {occupant.route}. Creating a "
                    f"second endpoint on that port would be refused, so nothing is proposed. "
                    f"Either rename {occupant.name} to {GCS_SERVER_LINK} in BlueOS, or point "
                    f"QGroundControl at it as it stands.",
                ),
                internal,
            ),
            (),
        )

    return AuditReport(
        None,
        _stock_server_link(),
        (
            Finding(
                CHECK_PRESENT,
                False,
                f"missing. Every stock BlueOS install ships one, so this boat's endpoint list "
                f"has been edited. It can be created as {LISTEN_TYPE} "
                f"{LISTEN_PLACE}:{LISTEN_PORT}.",
            ),
            internal,
        ),
        (ACTION_CREATE,),
    )


def _occupant(endpoints: tuple[Endpoint, ...]) -> Endpoint | None:
    """Whatever else is already listening on the port QGC dials, if anything."""
    for endpoint in endpoints:
        if endpoint.connection_type == LISTEN_TYPE and endpoint.argument == LISTEN_PORT:
            return endpoint
    return None


def _stock_server_link() -> Endpoint:
    """The endpoint to create on a boat that has lost its stock one.

    `persistent` is set explicitly because the API defaults it to false, and an
    endpoint created without it works perfectly until the next router reload and
    is then simply gone, with nothing on the boat to say it ever existed.
    """
    return Endpoint(
        name=GCS_SERVER_LINK,
        owner=PROVISIONED_OWNER,
        connection_type=LISTEN_TYPE,
        place=LISTEN_PLACE,
        argument=LISTEN_PORT,
        persistent=True,
        protected=False,
        enabled=True,
        overwrite_settings=False,
    )


def _listening_finding(link: Endpoint) -> Finding:
    if (link.connection_type, link.place, link.argument) == (
        LISTEN_TYPE,
        LISTEN_PLACE,
        LISTEN_PORT,
    ):
        return Finding(
            CHECK_LISTENING,
            True,
            f"{link.route}. 0.0.0.0 binds every interface, so this one endpoint serves the "
            "cellular path, the base station, and anything added later.",
        )
    return Finding(
        CHECK_LISTENING,
        False,
        f"{link.route}, not {LISTEN_TYPE} {LISTEN_PLACE}:{LISTEN_PORT}. Somebody pointed this "
        "boat somewhere on purpose, so it is left exactly as it is. Change it in BlueOS if it "
        "should be the stock listener.",
    )


def _enabled_finding(link: Endpoint) -> Finding:
    if link.enabled:
        return Finding(CHECK_ENABLED, True, "enabled, so the boat is listening.")
    if link.protected:
        return Finding(
            CHECK_ENABLED,
            False,
            "disabled, and flagged protected. This tool never writes a protected endpoint, so "
            "enabling it is a job for BlueOS's own Endpoints page.",
        )
    return Finding(
        CHECK_ENABLED,
        False,
        "disabled, which is how BlueOS ships it. QGroundControl gets no answer when it dials "
        "this boat until it is enabled.",
    )


def _persistent_finding(link: Endpoint) -> Finding:
    if link.persistent:
        return Finding(CHECK_PERSISTENT, True, "persistent, so it survives a router reload.")
    return Finding(
        CHECK_PERSISTENT,
        False,
        "not persistent, so it disappears at the next router reload and QGroundControl stops "
        "reaching this boat with nothing on the vehicle to show why.",
    )


def _internal_finding(endpoints: tuple[Endpoint, ...]) -> Finding:
    """Whether the loopback endpoints that put GPS on every reading are still there.

    Reported, never acted on. They are all flagged protected, and this tool does
    not write protected endpoints, so the only thing it can usefully do about a
    boat that has lost one is name it: the failure is otherwise invisible, since
    the boat keeps uploading and keeps reporting healthy.
    """
    missing = [
        name
        for name in INTERNAL_ENDPOINTS
        if (found := autopilot.find_endpoint(endpoints, name)) is None or not found.enabled
    ]
    if not missing:
        return Finding(
            CHECK_INTERNAL,
            True,
            f"{', '.join(INTERNAL_ENDPOINTS)} all present and enabled, so telemetry still "
            "carries a GPS position.",
        )
    return Finding(
        CHECK_INTERNAL,
        False,
        f"{', '.join(missing)} missing or disabled. The boat will keep recording, keep "
        "uploading and report healthy, but readings arrive with no GPS position: "
        "records_without_position climbs while records_with_position stays flat. Restore them "
        "from BlueOS; this tool does not write protected endpoints.",
    )


def _actions_for(findings: tuple[Finding, ...], link: Endpoint) -> tuple[str, ...]:
    """What to do about the findings. At most one thing.

    Every write restarts MAVLink Router and drops every live GCS connection, so a
    plan of two calls would take the link down twice. One PUT carries both flags.

    Nothing at all is proposed for a link that is protected or listening
    somewhere else: the first must not be written, and the second is somebody's
    deliberate change, to be reported rather than reverted.
    """
    failed = {finding.check for finding in findings if not finding.ok}
    if link.protected or CHECK_LISTENING in failed:
        return ()
    if failed & {CHECK_ENABLED, CHECK_PERSISTENT}:
        return (ACTION_REPAIR,)
    return ()


def describe_plan(report: AuditReport) -> str:
    """What the operator is about to authorise, naming each flag that changes."""
    if ACTION_CREATE in report.actions:
        return (
            f"Create {GCS_SERVER_LINK} ({LISTEN_TYPE} {LISTEN_PLACE}:{LISTEN_PORT}), enabled "
            "and persistent"
        )
    if ACTION_REPAIR in report.actions and report.link is not None:
        changes = []
        if not report.link.enabled:
            changes.append("enable it")
        if not report.link.persistent:
            changes.append("mark it persistent so it survives a router reload")
        return f"Update {GCS_SERVER_LINK} to {' and '.join(changes)}"
    return "Nothing to do"


def qgc_target(host: str) -> str:
    """Where to point QGroundControl for the boat reached at `host`.

    Built from the address the tech typed, with whichever API port they may have
    typed with it stripped off: QGC dials 14550, not the port this tool used.
    """
    return f"{blueos.host_only(host)}:{LISTEN_PORT}"


LogCallback = Callable[[str], None]


def apply_plan(host: str, report: AuditReport, log: LogCallback) -> None:
    """Run the audit's plan against a boat. Changes the vehicle.

    Driven by the report rather than by its own checks, so what runs is exactly
    what the operator was shown and agreed to. Writes at most once: see
    _actions_for for why that matters.
    """
    for action in report.actions:
        endpoint = report.desired
        # The last gate before the wire. Everything above builds `desired` from
        # GCS Server Link alone, and this is what makes that a property of the
        # module rather than of the path a reader happened to trace.
        if endpoint is None or endpoint.name != GCS_SERVER_LINK:
            raise autopilot.AutopilotError(
                f"Refusing to write {endpoint.name if endpoint else 'nothing'}: this tool only "
                f"ever writes {GCS_SERVER_LINK}."
            )

        if action == ACTION_CREATE:
            log(f"  Creating {GCS_SERVER_LINK} as {endpoint.route}, enabled and persistent...")
            autopilot.create_endpoints(host, (endpoint,))
        elif action == ACTION_REPAIR:
            log(f"  {describe_plan(report)}...")
            autopilot.update_endpoints(host, (endpoint,))
        else:  # pragma: no cover - unreachable unless a new action is added above
            raise autopilot.AutopilotError(f"No idea how to carry out {action!r}.")
        log("  MAVLink Router has restarted.")
