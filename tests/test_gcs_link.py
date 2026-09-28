"""Tests for the GCS link audit: absent, correct, and pointing elsewhere.

Pure throughout. audit() takes what a boat said and never touches the network, so
these run with no hardware and the audit itself is safe against a boat in service.

The reference state every test measures against is DB Cooper's own endpoint list
on 2026-08-12, where QGroundControl reached the boat over cellular by dialling
10.198.95.122:14550 and measured 195 packets and 4 heartbeats in 6 seconds.

Two invariants are worth more than the rest of this file: no plan ever touches a
protected endpoint, and no plan ever touches GCS Client Link.
"""
from __future__ import annotations

import autopilot
import gcs_link
import pytest
from test_autopilot import DB_COOPER


def _endpoints(*, server_link=None, drop=()) -> tuple[autopilot.Endpoint, ...]:
    """DB Cooper's list, with GCS Server Link overridden or entries removed."""
    entries = []
    for entry in DB_COOPER:
        if entry["name"] in drop:
            continue
        if entry["name"] == gcs_link.GCS_SERVER_LINK and server_link is not None:
            entry = {**entry, **server_link}
        entries.append(entry)
    return autopilot.parse_endpoints(entries)


def _finding(report: gcs_link.AuditReport, check: str) -> gcs_link.Finding:
    return next(f for f in report.findings if f.check == check)


# -- The three answers -------------------------------------------------------


def test_the_reference_boat_passes_with_nothing_to_do():
    """DB Cooper as it stood once GCS Server Link was enabled."""
    report = gcs_link.audit(_endpoints())

    assert report.ok
    assert report.actions == ()
    assert "QGroundControl can dial this boat" in report.summary


def test_a_stock_disabled_link_is_the_one_thing_this_exists_to_fix():
    # How BlueOS ships: present, correct, and off.
    report = gcs_link.audit(_endpoints(server_link={"enabled": False}))

    assert not report.ok
    assert report.actions == (gcs_link.ACTION_REPAIR,)
    assert report.desired.enabled is True
    assert "how BlueOS ships it" in _finding(report, gcs_link.CHECK_ENABLED).message


def test_an_absent_link_is_reported_as_absent_not_as_a_failure_to_read():
    report = gcs_link.audit(_endpoints(drop=(gcs_link.GCS_SERVER_LINK,)))

    assert not report.ok
    assert report.actions == (gcs_link.ACTION_CREATE,)
    assert "missing" in _finding(report, gcs_link.CHECK_PRESENT).message


def test_a_boat_pointed_somewhere_else_is_reported_never_overwritten():
    """The third answer. Somebody moved this on purpose; that is a fact for the
    operator, not a conflict for this tool to settle behind their back."""
    report = gcs_link.audit(
        _endpoints(server_link={"connection_type": "udpout", "place": "10.198.95.222"})
    )

    assert not report.ok
    assert report.actions == ()
    assert "left exactly as it is" in _finding(report, gcs_link.CHECK_LISTENING).message


@pytest.mark.parametrize(
    "moved",
    [
        {"place": "192.168.2.1"},
        {"argument": 14551},
        {"connection_type": "udpout"},
    ],
)
def test_any_part_of_the_address_being_different_stops_the_plan(moved):
    report = gcs_link.audit(_endpoints(server_link={**moved, "enabled": False}))

    # Disabled *and* moved: enabling it would turn on a link aimed somewhere we
    # did not choose, which is worse than leaving it off.
    assert report.actions == ()


# -- The vanishing endpoint --------------------------------------------------


def test_a_non_persistent_link_is_a_failure_even_when_it_is_working():
    """Almost certainly why the old QGC Cellular ZT endpoint vanished on its own.

    The API defaults persistent to false, so an endpoint made without it works
    perfectly right up until the next router reload.
    """
    report = gcs_link.audit(_endpoints(server_link={"persistent": False}))

    assert not report.ok
    assert report.actions == (gcs_link.ACTION_REPAIR,)
    assert report.desired.persistent is True


def test_anything_created_is_persistent_and_enabled():
    report = gcs_link.audit(_endpoints(drop=(gcs_link.GCS_SERVER_LINK,)))

    assert report.desired.persistent is True
    assert report.desired.enabled is True
    assert report.desired.route == "udpin 0.0.0.0:14550"


def test_one_write_repairs_both_flags_at_once():
    # Every write restarts the router, so a two-call plan drops the link twice.
    report = gcs_link.audit(_endpoints(server_link={"enabled": False, "persistent": False}))

    assert len(report.actions) == 1
    assert (report.desired.enabled, report.desired.persistent) == (True, True)


# -- What must never be touched ----------------------------------------------


def test_gcs_client_link_is_never_what_gets_written():
    """Michael's standing instruction: the base station path stays as it is.

    Checked across every state that produces a plan, rather than on one path a
    reader happened to trace.
    """
    for server_link in ({"enabled": False}, {"persistent": False}, None):
        report = gcs_link.audit(_endpoints(server_link=server_link))
        assert report.desired is None or report.desired.name == gcs_link.GCS_SERVER_LINK

    absent = gcs_link.audit(_endpoints(drop=(gcs_link.GCS_SERVER_LINK,)))
    assert absent.desired.name == gcs_link.GCS_SERVER_LINK


def test_a_protected_server_link_is_reported_and_left_alone():
    # Not how BlueOS ships it, but the guard is the point: protected means this
    # tool does not write it, whatever else is wrong.
    report = gcs_link.audit(_endpoints(server_link={"enabled": False, "protected": True}))

    assert not report.ok
    assert report.actions == ()
    assert "protected" in _finding(report, gcs_link.CHECK_ENABLED).message


def test_the_plan_never_proposes_writing_a_protected_endpoint():
    for server_link in ({"enabled": False}, {"persistent": False}, None):
        report = gcs_link.audit(_endpoints(server_link=server_link))
        assert report.desired is None or report.desired.protected is False


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"connection_type": "udpout", "place": "10.198.95.222"},
        {"enabled": False, "protected": True},
        {"place": "192.168.2.1", "enabled": False},
    ],
    ids=["already-correct", "moved", "protected", "moved-and-disabled"],
)
def test_nothing_to_write_means_there_is_no_body_to_write(state):
    """A report with no actions carries no endpoint either.

    Otherwise a moved link sits in `desired` as a ready-made request body for
    anyone who reads that field without also reading the actions.
    """
    report = gcs_link.audit(_endpoints(server_link=state))

    assert report.actions == ()
    assert report.desired is None


@pytest.mark.parametrize("lost", gcs_link.INTERNAL_ENDPOINTS)
def test_a_missing_loopback_endpoint_is_reported_with_the_counter_to_watch(lost):
    """The failure that is otherwise invisible: the boat keeps uploading and
    reports healthy while every reading arrives with no GPS position."""
    report = gcs_link.audit(_endpoints(drop=(lost,)))

    finding = _finding(report, gcs_link.CHECK_INTERNAL)
    assert not finding.ok
    assert lost in finding.message
    assert "records_without_position" in finding.message


def test_a_disabled_loopback_endpoint_counts_as_lost():
    # Disabling one costs the same GPS position that removing it does.
    endpoints = autopilot.parse_endpoints(
        [{**e, "enabled": False} if e["name"] == "MAVLink2Rest" else e for e in DB_COOPER]
    )

    assert not _finding(gcs_link.audit(endpoints), gcs_link.CHECK_INTERNAL).ok


def test_a_boat_with_a_broken_loopback_endpoint_is_still_reachable():
    """`reachable` is narrower than `ok` on purpose. Losing a loopback endpoint
    is a telemetry fault, not a GCS one, and the operator still needs the address."""
    report = gcs_link.audit(_endpoints(drop=("MAVLink2Rest",)))

    assert not report.ok
    assert report.reachable


@pytest.mark.parametrize(
    "broken", [{"enabled": False}, {"place": "192.168.2.1"}], ids=["disabled", "moved"]
)
def test_a_boat_qgc_cannot_dial_is_not_reachable(broken):
    assert not gcs_link.audit(_endpoints(server_link=broken)).reachable


def test_an_absent_link_is_not_reachable():
    assert not gcs_link.audit(_endpoints(drop=(gcs_link.GCS_SERVER_LINK,))).reachable


def test_a_broken_loopback_endpoint_does_not_stop_the_gcs_link_being_fixed():
    # Independent problems. The GCS repair is still offered and still correct.
    report = gcs_link.audit(
        _endpoints(server_link={"enabled": False}, drop=("MAVLink2Rest",))
    )

    assert report.actions == (gcs_link.ACTION_REPAIR,)
    assert not _finding(report, gcs_link.CHECK_INTERNAL).ok


# -- The port already being taken --------------------------------------------


def test_something_else_holding_the_port_is_reported_rather_than_collided_with():
    """The manager refuses a second endpoint on a port already in use, so
    proposing one would restart the router, drop every link, and then fail."""
    endpoints = autopilot.parse_endpoints(
        [
            {**e, "name": "QGC Cellular ZT"} if e["name"] == gcs_link.GCS_SERVER_LINK else e
            for e in DB_COOPER
        ]
    )

    report = gcs_link.audit(endpoints)

    assert report.actions == ()
    assert report.desired is None
    assert "QGC Cellular ZT" in _finding(report, gcs_link.CHECK_PRESENT).message


# -- Words the operator reads ------------------------------------------------


def test_the_plan_names_each_flag_that_changes():
    enable_only = gcs_link.audit(_endpoints(server_link={"enabled": False}))
    persist_only = gcs_link.audit(_endpoints(server_link={"persistent": False}))
    both = gcs_link.audit(_endpoints(server_link={"enabled": False, "persistent": False}))

    assert gcs_link.describe_plan(enable_only) == "Update GCS Server Link to enable it"
    assert "persistent" in gcs_link.describe_plan(persist_only)
    assert "enable it and mark it persistent" in gcs_link.describe_plan(both)


def test_a_boat_with_nothing_to_do_says_so():
    assert gcs_link.describe_plan(gcs_link.audit(_endpoints())) == "Nothing to do"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("10.198.95.122", "10.198.95.122:14550"),
        ("10.198.95.122:8000", "10.198.95.122:14550"),
        ("blueos.local", "blueos.local:14550"),
    ],
)
def test_the_qgc_target_drops_whichever_api_port_the_tech_typed(typed, expected):
    # QGC dials 14550. Handing back :8000 sends an operator somewhere with no
    # MAVLink on it and no clue why the link never comes up.
    assert gcs_link.qgc_target(typed) == expected


# -- Applying ----------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []
        self.lines = []

    def create(self, host, endpoints, timeout=None):
        self.calls.append(("POST", host, endpoints))
        return "ok"

    def update(self, host, endpoints, timeout=None):
        self.calls.append(("PUT", host, endpoints))
        return "ok"


@pytest.fixture
def recorder(monkeypatch):
    spy = _Recorder()
    monkeypatch.setattr(autopilot, "create_endpoints", spy.create)
    monkeypatch.setattr(autopilot, "update_endpoints", spy.update)
    return spy


def test_repairing_puts_exactly_one_endpoint_once(recorder):
    report = gcs_link.audit(_endpoints(server_link={"enabled": False, "persistent": False}))

    gcs_link.apply_plan("blueos.local", report, recorder.lines.append)

    assert len(recorder.calls) == 1, "every write restarts the router"
    method, host, endpoints = recorder.calls[0]
    assert (method, host) == ("PUT", "blueos.local")
    assert [e.name for e in endpoints] == [gcs_link.GCS_SERVER_LINK]


def test_creating_posts_the_stock_listener(recorder):
    report = gcs_link.audit(_endpoints(drop=(gcs_link.GCS_SERVER_LINK,)))

    gcs_link.apply_plan("blueos.local", report, recorder.lines.append)

    method, _, endpoints = recorder.calls[0]
    assert method == "POST"
    assert endpoints[0].route == "udpin 0.0.0.0:14550"
    assert endpoints[0].persistent is True


def test_a_correct_boat_is_not_written_to_at_all(recorder):
    report = gcs_link.audit(_endpoints())

    gcs_link.apply_plan("blueos.local", report, recorder.lines.append)

    assert recorder.calls == []


def test_applying_says_the_router_restarted(recorder):
    # The operator has to know their QGC link is dead, because QGC will not say.
    report = gcs_link.audit(_endpoints(server_link={"enabled": False}))

    gcs_link.apply_plan("blueos.local", report, recorder.lines.append)

    assert any("restarted" in line for line in recorder.lines)


def test_a_plan_aimed_at_anything_but_the_server_link_is_refused(recorder):
    """The last gate before the wire, tested by forging a report that the audit
    itself would never produce."""
    forged = gcs_link.AuditReport(
        link=None,
        desired=autopilot.find_endpoint(_endpoints(), gcs_link.GCS_CLIENT_LINK),
        findings=(),
        actions=(gcs_link.ACTION_REPAIR,),
    )

    with pytest.raises(autopilot.AutopilotError, match=gcs_link.GCS_CLIENT_LINK):
        gcs_link.apply_plan("blueos.local", forged, recorder.lines.append)

    assert recorder.calls == []
