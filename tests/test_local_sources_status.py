# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for jasper.local_sources.status — the single owner of
per-source availability/enabled/effective state and the enable-time
precondition checks.

All product probes (systemd, DBus/Bluetooth, install profile, USB hardware
role, fan-in) are stubbed at the names ``status.py`` imports them under, so
these tests exercise only the derivation logic, never real systemctl/DBus.
"""
from __future__ import annotations

import pytest

from jasper.bluetooth.availability import BluetoothAvailability
from jasper.local_sources import local_source_lifecycle, status
from jasper.music_sources import Source

AIRPLAY_UNIT = local_source_lifecycle(Source.AIRPLAY).intent_unit
SPOTIFY_UNIT = local_source_lifecycle(Source.SPOTIFY).intent_unit
assert AIRPLAY_UNIT is not None
assert SPOTIFY_UNIT is not None

DEFAULT_INTENTS = {
    Source.AIRPLAY: True,
    Source.BLUETOOTH: True,
    Source.SPOTIFY: True,
    Source.USBSINK: False,
}


@pytest.fixture
def stub_backends(monkeypatch):
    """Stub every probe ``status.py`` owns so state reads are deterministic."""

    def _stub(
        *,
        active=(),
        activating=(),
        available_units=None,
        usb_ready=True,
        usb_card=False,
        bt=(True, False),
        bt_adapter=True,
        bt_missing_units=(),
        bt_hard_blocked=False,
        intents=None,
        parked=False,
        fanin_status=None,
    ):
        resolved_intents = dict(intents) if intents is not None else dict(DEFAULT_INTENTS)
        active_set = set(active)
        if AIRPLAY_UNIT in active_set:
            active_set.update(local_source_lifecycle(Source.AIRPLAY).health_units)
        if available_units is None:
            available_units = {
                *local_source_lifecycle(Source.AIRPLAY).health_units,
                *status.BLUETOOTH_RUNTIME_UNITS,
                status.BLUETOOTH_CONTROL_PLANE_UNIT,
                SPOTIFY_UNIT,
                status.USBSINK_UNIT,
                status.USBSINK_GADGET_UNIT,
            }
        activating_set = set(activating)

        def fake_read_unit_states(units, timeout=5.0):
            records = {}
            for unit in units:
                if unit not in available_units:
                    records[unit] = {"load_state": "not-found", "active_state": "inactive"}
                elif unit in activating_set:
                    records[unit] = {"load_state": "loaded", "active_state": "activating"}
                elif unit in active_set:
                    records[unit] = {"load_state": "loaded", "active_state": "active"}
                else:
                    records[unit] = {"load_state": "loaded", "active_state": "inactive"}
            return records

        monkeypatch.setattr(status, "read_source_intents", lambda: dict(resolved_intents))
        monkeypatch.setattr(
            status,
            "local_sources_allowed",
            lambda: (not parked, "bonded_follower" if parked else None),
        )
        monkeypatch.setattr(status, "_profile_allows_local_sources", lambda: True)
        monkeypatch.setattr(status, "read_unit_states", fake_read_unit_states)
        monkeypatch.setattr(
            status,
            "_usbsink_capability",
            lambda: (usb_ready, "" if usb_ready else "USB output DAC uses the shared port"),
        )
        monkeypatch.setattr(status, "_uac2_card_present", lambda: usb_card)
        monkeypatch.setattr(
            status,
            "probe_bluetooth_availability",
            lambda unit_available: BluetoothAvailability(
                available=bt_adapter and not bt_missing_units and not bt_hard_blocked,
                radio_present=bt_adapter,
                any_soft_blocked=not resolved_intents[Source.BLUETOOTH],
                all_soft_blocked=not resolved_intents[Source.BLUETOOTH],
                hard_blocked=bt_hard_blocked,
                missing_units=bt_missing_units,
            ),
        )
        monkeypatch.setattr(
            status, "read_fanin_status", lambda: fanin_status if fanin_status is not None else {},
        )

        async def _bt():
            return bt

        monkeypatch.setattr(status, "_bt_state", _bt)

    return _stub


# ---- _source_state (pure) ----------------------------------------------------


def test_source_state_keeps_availability_independent_from_effective_off():
    state = status._source_state(
        desired=False,
        observed=False,
        available=False,
        unavailable_reason="hardware cannot provide this source",
    )

    assert state == {
        "enabled": False,
        "desired": False,
        "effective": "off",
        "available": False,
        "unavailableReason": "hardware cannot provide this source",
    }


def test_source_state_reports_off_drift_even_when_source_is_unavailable():
    state = status._source_state(
        desired=False,
        observed=True,
        available=False,
        unavailable_reason="hardware cannot provide this source",
    )

    assert state["effective"] == "degraded"
    assert state["available"] is False
    assert state["unavailableReason"] == "hardware cannot provide this source"
    assert "current runtime state does not match" in str(state["degradedReason"])


# ---- read_source_status -------------------------------------------------------


def test_read_source_status_shape(stub_backends):
    stub_backends(
        active={AIRPLAY_UNIT, SPOTIFY_UNIT, *status.BLUETOOTH_RUNTIME_UNITS},
        usb_ready=False,
        bt=(True, True),
    )
    state = status.read_source_status()
    assert state["airplay"] == {
        "enabled": True, "desired": True, "effective": "on", "available": True,
    }
    assert state["spotify_connect"] == {
        "enabled": True, "desired": True, "effective": "on", "available": True,
    }
    assert state["bluetooth"] == {
        "enabled": True, "desired": True, "effective": "on", "available": True,
        "hasPairedHid": True,
    }
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["desired"] is False
    assert state["usbsink"]["effective"] == "off"
    assert state["usbsink"]["available"] is False
    assert "output DAC" in str(state["usbsink"]["unavailableReason"])


def test_read_source_status_renderer_units_unavailable(stub_backends):
    stub_backends(available_units=set(), usb_ready=True)
    state = status.read_source_status()

    # Availability never rewrites the user's durable choice.
    assert state["airplay"]["enabled"] is True
    assert state["airplay"]["effective"] == "unavailable"
    assert state["airplay"]["available"] is False
    assert "not installed on this speaker" in str(state["airplay"]["unavailableReason"])
    assert state["spotify_connect"]["effective"] == "unavailable"
    assert state["usbsink"]["available"] is False
    unavailable = " ".join(
        str(item.get("unavailableReason") or "") for item in state.values()
    )
    assert "install.sh" in unavailable


def test_read_source_status_profile_disables_stale_renderer_units(
    stub_backends, monkeypatch,
):
    stub_backends(
        active={AIRPLAY_UNIT, SPOTIFY_UNIT, status.USBSINK_UNIT},
        bt=(True, False),
        usb_ready=True,
    )
    monkeypatch.setattr(status, "_profile_allows_local_sources", lambda: False)

    state = status.read_source_status()

    assert state["airplay"]["effective"] == "unavailable"
    assert state["airplay"]["available"] is False
    assert state["spotify_connect"]["available"] is False
    assert state["bluetooth"]["available"] is False
    assert "not installed on this speaker" in str(state["bluetooth"]["unavailableReason"])
    assert state["usbsink"]["available"] is False


def test_read_source_status_bluetooth_unavailable(stub_backends):
    stub_backends(bt=(False, False), bt_adapter=False)
    state = status.read_source_status()["bluetooth"]
    assert state["effective"] == "unavailable"
    assert state["available"] is False
    assert state["hasPairedHid"] is False
    assert "Bluetooth adapter" in str(state["unavailableReason"])


@pytest.mark.parametrize(
    ("bt_hard_blocked", "bt_missing_units", "expected"),
    [
        (True, (), "hardware radio switch"),
        (False, ("bt-agent.service",), "bt-agent.service"),
    ],
)
def test_read_source_status_reuses_specific_bluetooth_unavailable_reason(
    stub_backends, bt_hard_blocked, bt_missing_units, expected,
):
    stub_backends(bt_hard_blocked=bt_hard_blocked, bt_missing_units=bt_missing_units)

    state = status.read_source_status()["bluetooth"]

    assert state["available"] is False
    assert expected in str(state["unavailableReason"])


def test_read_source_status_keeps_enabled_intent_when_runtime_is_degraded(
    stub_backends,
):
    stub_backends(active=set(), bt=(False, False))

    state = status.read_source_status()

    for key in ("airplay", "bluetooth", "spotify_connect"):
        assert state[key]["enabled"] is True
        assert state[key]["effective"] == "degraded"
        assert state[key]["available"] is True
        assert "degradedReason" in state[key]


def test_read_source_status_bluetooth_off_remains_available(stub_backends):
    stub_backends(
        bt=(False, False),
        intents={**DEFAULT_INTENTS, Source.BLUETOOTH: False},
    )

    state = status.read_source_status()["bluetooth"]

    assert state == {
        "enabled": False, "desired": False, "effective": "off",
        "available": True, "hasPairedHid": False,
    }


def test_read_source_status_usbsink_uac2_card_is_effective_degradation(stub_backends):
    """A visible UAC2 card with a stopped bridge is observable degradation."""
    stub_backends(
        usb_ready=True, usb_card=True,
        intents={**DEFAULT_INTENTS, Source.USBSINK: True},
    )

    state = status.read_source_status()["usbsink"]

    assert state["available"] is True
    assert state["desired"] is True
    assert state["effective"] == "degraded"
    assert "advertised" in str(state["degradedReason"])


def test_read_source_status_usbsink_bridge_without_uac2_is_degraded(stub_backends):
    stub_backends(
        usb_ready=True, usb_card=False,
        active={status.USBSINK_UNIT},
        intents={**DEFAULT_INTENTS, Source.USBSINK: True},
    )

    state = status.read_source_status()["usbsink"]

    assert state["desired"] is True
    assert state["effective"] == "degraded"
    assert "not advertised" in str(state["degradedReason"])


def test_read_source_status_usbsink_unavailable_when_gadget_unit_missing(stub_backends):
    stub_backends(
        usb_ready=True,
        available_units={
            AIRPLAY_UNIT,
            *status.BLUETOOTH_RUNTIME_UNITS,
            status.BLUETOOTH_CONTROL_PLANE_UNIT,
            SPOTIFY_UNIT,
            status.USBSINK_UNIT,
        },
    )

    state = status.read_source_status()["usbsink"]

    assert state["available"] is False
    assert state["effective"] == "off"
    assert "composite gadget unit" in str(state["unavailableReason"])


def test_parked_bluetooth_outranks_unavailable_across_sources_surface(stub_backends):
    stub_backends(parked=True, bt_adapter=False)
    state = status.read_source_status()["bluetooth"]
    assert state["effective"] == "parked"
    assert state["available"] is False


def test_state_reports_parked_pair_without_rewriting_desired(stub_backends):
    intents = {**DEFAULT_INTENTS, Source.BLUETOOTH: False}
    stub_backends(intents=intents, parked=True, bt=(False, False))

    state = status.read_source_status()

    assert state["pair"] == {"parked": True}
    for key, source in (
        ("airplay", Source.AIRPLAY),
        ("bluetooth", Source.BLUETOOTH),
        ("spotify_connect", Source.SPOTIFY),
        ("usbsink", Source.USBSINK),
    ):
        assert state[key]["enabled"] is intents[source]
        assert state[key]["effective"] == "parked"


def test_read_source_status_probes_unit_states_once(stub_backends, monkeypatch):
    """Polling cost: one ``read_unit_states`` batch per snapshot, not one
    per source or per unit."""
    stub_backends()
    fake = status.read_unit_states
    calls: list[tuple[str, ...]] = []

    def counting(units, timeout=5.0):
        calls.append(tuple(units))
        return fake(units, timeout=timeout)

    monkeypatch.setattr(status, "read_unit_states", counting)

    status.read_source_status()

    assert len(calls) == 1
    assert set(calls[0]) == set(status._STATE_UNITS)


# ---- sources_parked -----------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [((True, None), False), ((False, "role_transition_in_progress"), True)],
)
def test_sources_parked_inverts_role_verdict(monkeypatch, verdict, expected):
    monkeypatch.setattr(status, "local_sources_allowed", lambda: verdict)
    assert status.sources_parked() is expected


# ---- enable_blocker: one precedence table over every source/probe -----------


def _happy_backends(monkeypatch) -> None:
    monkeypatch.setattr(status, "_profile_allows_local_sources", lambda: True)
    monkeypatch.setattr(
        status,
        "read_unit_states",
        lambda units, timeout=5.0: {
            unit: {"load_state": "loaded", "active_state": "active"} for unit in units
        },
    )
    monkeypatch.setattr(status, "_usbsink_capability", lambda: (True, ""))
    monkeypatch.setattr(
        status,
        "probe_bluetooth_availability",
        lambda unit_available: BluetoothAvailability(
            available=True, radio_present=True,
            any_soft_blocked=False, all_soft_blocked=False, hard_blocked=False,
        ),
    )


@pytest.mark.parametrize(
    ("setup", "source", "expected_substring"),
    [
        # profile disallowed outranks every other check, for all four sources.
        ("profile_denied", Source.AIRPLAY, "AirPlay is not installed"),
        ("profile_denied", Source.SPOTIFY, "Spotify Connect is not installed"),
        ("profile_denied", Source.BLUETOOTH, "Bluetooth audio is not installed"),
        ("profile_denied", Source.USBSINK, "USB Audio Input is not installed"),
        # missing health/main unit, per source (USB: both units missing ->
        # the main-unit message wins over the gadget-specific one).
        ("no_units_loaded", Source.AIRPLAY, "AirPlay is not installed"),
        ("no_units_loaded", Source.SPOTIFY, "Spotify Connect is not installed"),
        ("no_units_loaded", Source.USBSINK, "USB Audio Input is not installed"),
        # USB gadget unit missing alone -> the gadget-specific message, not
        # the generic "not installed" one.
        ("missing_usb_gadget_unit", Source.USBSINK, "composite gadget unit"),
        # Bluetooth adapter/unit unavailability, reused verbatim from
        # bluetooth_unavailable_reason.
        ("bt_hard_blocked", Source.BLUETOOTH, "hardware radio switch"),
        ("bt_missing_units", Source.BLUETOOTH, "bt-agent.service"),
        ("bt_no_adapter", Source.BLUETOOTH, "Bluetooth adapter"),
        # USB hardware-capability refusal, once units and profile pass.
        ("usb_hardware_unavailable", Source.USBSINK, "USB output DAC uses the shared port"),
        # the happy row: every check passes.
        ("happy", Source.AIRPLAY, ""),
        ("happy", Source.SPOTIFY, ""),
        ("happy", Source.BLUETOOTH, ""),
        ("happy", Source.USBSINK, ""),
    ],
)
def test_enable_blocker_precedence(monkeypatch, setup, source, expected_substring):
    _happy_backends(monkeypatch)

    if setup == "profile_denied":
        monkeypatch.setattr(status, "_profile_allows_local_sources", lambda: False)
    elif setup == "no_units_loaded":
        monkeypatch.setattr(status, "read_unit_states", lambda units, timeout=5.0: {})
    elif setup == "missing_usb_gadget_unit":
        monkeypatch.setattr(
            status,
            "read_unit_states",
            lambda units, timeout=5.0: {
                unit: {"load_state": "loaded", "active_state": "active"}
                for unit in units
                if unit != status.USBSINK_GADGET_UNIT
            },
        )
    elif setup == "bt_hard_blocked":
        monkeypatch.setattr(
            status,
            "probe_bluetooth_availability",
            lambda unit_available: BluetoothAvailability(
                available=False, radio_present=True,
                any_soft_blocked=False, all_soft_blocked=False, hard_blocked=True,
            ),
        )
    elif setup == "bt_missing_units":
        monkeypatch.setattr(
            status,
            "probe_bluetooth_availability",
            lambda unit_available: BluetoothAvailability(
                available=False, radio_present=True,
                any_soft_blocked=False, all_soft_blocked=False, hard_blocked=False,
                missing_units=("bt-agent.service",),
            ),
        )
    elif setup == "bt_no_adapter":
        monkeypatch.setattr(
            status,
            "probe_bluetooth_availability",
            lambda unit_available: BluetoothAvailability(
                available=False, radio_present=False,
                any_soft_blocked=None, all_soft_blocked=None, hard_blocked=None,
            ),
        )
    elif setup == "usb_hardware_unavailable":
        monkeypatch.setattr(
            status,
            "_usbsink_capability",
            lambda: (False, "USB output DAC uses the shared port"),
        )
    elif setup != "happy":
        raise AssertionError(f"unhandled setup {setup!r}")

    result = status.enable_blocker(source)

    if expected_substring:
        assert expected_substring in result
    else:
        assert result == ""
