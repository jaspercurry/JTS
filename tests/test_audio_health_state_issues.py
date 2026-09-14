# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`_state_issues`: the incident-row builder `AudioHealthSampler._tick` and
ADR-0178's parked-transport tests feed with the same signal-path/service-state
facts `compose_audio_health` already classified.

`compose_audio_health`'s own verdicts (signal_path, source cards, overall)
stay pinned in test_audio_health.py; these pin only the rows this module
raises from them — a household-off drift, a required companion service
failing, and a stopped CamillaDSP with no other surface (#2163).
"""

from __future__ import annotations

import pytest

from jasper.control.audio_state_issues import _state_issues

from .audio_health_fixtures import _CAMILLA_CLEAN_STOP, _airplay, _compose, _outputd


def test_a_deaf_content_source_is_an_issue_a_healthy_one_is_not() -> None:
    """#3458: outputd emitting silence it did not intend read `clean` here.

    Every other input to the signal path — the backend, both watchdogs, the
    xrun counts — stays healthy through it, so the verdict outputd publishes
    is the only thing that can move this off green.
    """
    deaf = _compose(outputd=_outputd(content_deaf=True))
    assert deaf["signal_path"]["code"] == "output_deaf"
    assert deaf["signal_path"]["status"] == "issue"
    assert deaf["overall"]["status"] == "issue"
    assert _compose(outputd=_outputd())["signal_path"]["code"] == "clean"
    # A red headline with no incident row leaves `current_incident` None, so
    # the fault never enters history and never records a recovery. Every other
    # issue-status path code raises one; this is where it comes from.
    rows = _state_issues(
        _airplay(),
        _outputd(content_deaf=True),
        deaf["signal_path"],
        {"status": "ok", "runtime": {"raw_mode": "disabled"}},
        None,
        None,
        None,
        activity_unknown=False,
        coherence_park=None,
        undeclared_hardware=None,
        transport_park=None,
    )
    assert "path.outputd_content_deaf" in {row["key"] for row in rows}


def test_household_off_but_active_is_reported_as_drift() -> None:
    service_states = {
        "librespot.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"spotify": False},
    )
    spotify = next(source for source in health["sources"] if source["id"] == "spotify")
    assert spotify["state"] == "unavailable"

    issues = _state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"spotify": False},
    )
    assert any(issue["key"].endswith("off_drift") for issue in issues)


def test_usb_off_ignores_always_on_management_gadget() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "inactive",
            "result": "success",
        },
        "jasper-usbsink-volume.service": {
            "load_state": "loaded",
            "active_state": "inactive",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": False},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "off"
    assert usb["status"] == "idle"

    issues = _state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": False},
    )
    assert not any(issue["key"].startswith("usbsink.service.") for issue in issues)


def test_usb_off_with_active_audio_service_is_reported_as_drift() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": False},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "unavailable"
    assert usb["status"] == "issue"

    issues = _state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": False},
    )
    drift_keys = {issue["key"] for issue in issues if issue["key"].endswith("off_drift")}
    assert drift_keys == {
        "usbsink.service.jasper-usbsink.service.off_drift",
    }


def test_usb_on_still_requires_its_management_gadget() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "failed",
            "result": "exit-code",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": True},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "unavailable"
    assert usb["status"] == "issue"

    issues = _state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": True},
    )
    assert any(
        issue["key"] == "usbsink.service.jasper-usbgadget.service"
        for issue in issues
    )


def test_required_pairing_agent_failure_degrades_bluetooth() -> None:
    health = _compose(service_states={
        "bluealsa-aplay.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "bluealsa.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "bt-agent.service": {
            "active_state": "failed",
            "load_state": "loaded",
            "result": "exit-code",
        },
    })

    bluetooth = next(
        source for source in health["sources"] if source["id"] == "bluetooth"
    )
    assert bluetooth["state"] == "unavailable"
    assert bluetooth["status"] == "issue"
    assert any(
        issue["key"] == "bluetooth.service.bt-agent.service"
        for issue in _state_issues(
            _airplay(),
            _outputd(),
            {"status": "idle", "headline": "No source is playing", "detail": ""},
            {"status": "idle"},
            None,
            {
                "bluealsa-aplay.service": {
                    "active_state": "active",
                    "load_state": "loaded",
                    "result": "success",
                },
                "bluealsa.service": {
                    "active_state": "active",
                    "load_state": "loaded",
                    "result": "success",
                },
                "bt-agent.service": {
                    "active_state": "failed",
                    "load_state": "loaded",
                    "result": "exit-code",
                },
            },
        )
    )
    assert health["overall"]["status"] == "idle"


# --------------------------------------------------------------------------- #
# A stopped CamillaDSP is visible (#2163).
#
# `jasper-camilla.service` has no Condition gate and, per its own unit file,
# "must NEVER stay stopped" — but a CLEAN stop passed every surface: doctor's
# `check_service_runtime_state` flags only `failed`, `system_metrics` hides
# cleanly-inactive units, and camilla is in no source's `health_units`.
# --------------------------------------------------------------------------- #

def _camilla_issues(camilla_state: dict | None, *, warmup: bool = False) -> list[dict]:
    service_states = (
        None if camilla_state is None else {"jasper-camilla.service": camilla_state}
    )
    issues = _state_issues(
        _airplay(warmup=warmup),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        None,
    )
    return [issue for issue in issues if issue["key"] == "path.camilla_stopped"]


@pytest.mark.parametrize(
    ("camilla_state", "warmup", "issue_count"),
    [
        # The exact state that was invisible: inactive, result=success.
        (_CAMILLA_CLEAN_STOP, False, 1),
        (
            {
                "load_state": "loaded", "active_state": "failed",
                "sub_state": "failed", "result": "exit-code",
            },
            False, 1,
        ),
        ({"load_state": "loaded", "active_state": "active", "result": "success"},
         False, 0),
        ({"load_state": "loaded", "active_state": "activating", "result": "success"},
         False, 0),
        ({"load_state": "loaded", "active_state": "reloading", "result": "success"},
         False, 0),
        (None, False, 0),  # no systemctl
        ({}, False, 0),  # before the first probe
        # A deploy restarts jasper-control, so the boot grace covers its
        # bounce — same gate the sibling path.fanin_unavailable /
        # path.outputd_unavailable issues use.
        (_CAMILLA_CLEAN_STOP, True, 0),
    ],
)
def test_camilla_stopped_issue_row_matches_unit_state(
    camilla_state: dict | None, warmup: bool, issue_count: int,
) -> None:
    issues = _camilla_issues(camilla_state, warmup=warmup)

    assert len(issues) == issue_count
    if issue_count:
        issue = issues[0]
        assert issue["key"] == "path.camilla_stopped"
        assert issue["severity"] == "issue"
        assert issue["scope"] == "path"
        # `AudioHealthSampler._tick` drops availability-impact issues whose
        # source_id is not the active source. A path-scoped continuity issue
        # is kept, which is what makes this reach `/state` and the /system
        # audio card.
        assert issue["impact"] == "continuity"
        assert issue["source_id"] is None

