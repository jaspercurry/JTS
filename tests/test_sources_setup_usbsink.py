# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""USB Audio Input coverage for the shared /sources/ intent coordinator."""
from __future__ import annotations

import pytest

from jasper.local_sources import local_source_lifecycle
from jasper.music_sources import MUSIC_SOURCE_SPECS, Source
from jasper.web import sources_setup


DEFAULT_INTENTS = {
    Source.AIRPLAY: True,
    Source.BLUETOOTH: True,
    Source.SPOTIFY: True,
    Source.USBSINK: False,
}
AIRPLAY_UNIT = local_source_lifecycle(Source.AIRPLAY).intent_unit
SPOTIFY_CONNECT_UNIT = local_source_lifecycle(Source.SPOTIFY).intent_unit
assert AIRPLAY_UNIT is not None
assert SPOTIFY_CONNECT_UNIT is not None


def _patch_state_dependencies(
    monkeypatch,
    *,
    usb_ready: bool,
    usb_card: bool = False,
    usb_active: bool = False,
    available_units: set[str] | None = None,
    intents: dict[Source, bool] | None = None,
) -> None:
    if available_units is None:
        available_units = {
            AIRPLAY_UNIT,
            *sources_setup.BLUETOOTH_RUNTIME_UNITS,
            SPOTIFY_CONNECT_UNIT,
            sources_setup.USBSINK_UNIT,
            sources_setup.USBSINK_GADGET_UNIT,
        }
    monkeypatch.setattr(
        sources_setup,
        "read_source_intents",
        lambda: dict(intents or DEFAULT_INTENTS),
    )
    monkeypatch.setattr(sources_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(
        sources_setup,
        "_unit_available",
        lambda unit: unit in available_units,
    )
    monkeypatch.setattr(
        sources_setup,
        "_unit_active",
        lambda unit: unit == sources_setup.USBSINK_UNIT and usb_active,
    )
    class _Snapshot:
        @staticmethod
        def available(unit):
            return unit in available_units

        @staticmethod
        def active(unit):
            return unit == sources_setup.USBSINK_UNIT and usb_active

        @staticmethod
        def activating(_unit):
            return False

    monkeypatch.setattr(
        sources_setup, "probe_unit_snapshot", lambda _units: _Snapshot(),
    )
    monkeypatch.setattr(sources_setup, "_uac2_card_present", lambda: usb_card)
    monkeypatch.setattr(
        sources_setup,
        "_usbsink_capability",
        lambda: (usb_ready, "" if usb_ready else "USB hardware unavailable"),
    )
    monkeypatch.setattr(
        sources_setup.os.path,
        "isdir",
        lambda path: path == sources_setup.BLUETOOTH_ADAPTER_PATH,
    )

    async def fake_bt():
        return False, False

    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)


def test_usbsink_in_valid_sources():
    assert "usbsink" in sources_setup.VALID_SOURCES
    assert set(sources_setup.VALID_SOURCES) == {
        spec.wizard_key for spec in MUSIC_SOURCE_SPECS
    }


def test_systemd_units_come_from_local_source_lifecycle_registry():
    usbsink = local_source_lifecycle(Source.USBSINK)
    assert sources_setup.USBSINK_UNIT == usbsink.intent_unit
    assert sources_setup.USBSINK_GADGET_UNIT in usbsink.advertise_units
    assert sources_setup.USBSINK_GADGET_UNIT == "jasper-usbgadget.service"


def test_index_html_uses_shared_toggle_markup_and_csrf_meta():
    html = sources_setup._index_html("csrf-token").decode("utf-8")

    assert 'meta name="jts-csrf" content="csrf-token"' in html
    assert html.count('class="toggle"') == 4
    assert 'id="t-airplay"' in html
    assert 'id="t-bluetooth"' in html
    assert "{toggle_" not in html
    assert '<script type="module" src="/assets/sources/js/main.js">' in html


def test_gather_state_includes_unavailable_usbsink(monkeypatch):
    _patch_state_dependencies(monkeypatch, usb_ready=False)

    state = sources_setup._gather_state()["usbsink"]

    assert state["enabled"] is False
    assert state["desired"] is False
    assert state["effective"] == "unavailable"
    assert state["available"] is False


def test_gather_state_preserves_desired_on_while_usb_hardware_unavailable(
    monkeypatch,
):
    _patch_state_dependencies(
        monkeypatch,
        usb_ready=False,
        intents={**DEFAULT_INTENTS, Source.USBSINK: True},
    )

    state = sources_setup._gather_state()["usbsink"]

    assert state["desired"] is True
    assert state["enabled"] is True
    assert state["effective"] == "unavailable"
    assert "USB hardware unavailable" in str(state["unavailableReason"])


def test_gather_state_usbsink_available_when_hardware_role_allows(monkeypatch):
    _patch_state_dependencies(monkeypatch, usb_ready=True)

    state = sources_setup._gather_state()["usbsink"]

    assert state == {
        "enabled": False,
        "desired": False,
        "effective": "off",
        "available": True,
    }


def test_gather_state_usbsink_uac2_card_is_effective_degradation(monkeypatch):
    """A visible UAC2 card with a stopped bridge is observable degradation."""
    intents = {**DEFAULT_INTENTS, Source.USBSINK: True}
    _patch_state_dependencies(
        monkeypatch,
        usb_ready=True,
        usb_card=True,
        usb_active=False,
        intents=intents,
    )

    state = sources_setup._gather_state()["usbsink"]

    assert state["available"] is True
    assert state["enabled"] is True
    assert state["desired"] is True
    assert state["effective"] == "degraded"
    assert "advertised" in str(state["degradedReason"])


def test_gather_state_usbsink_bridge_without_uac2_is_degraded(monkeypatch):
    intents = {**DEFAULT_INTENTS, Source.USBSINK: True}
    _patch_state_dependencies(
        monkeypatch,
        usb_ready=True,
        usb_card=False,
        usb_active=True,
        intents=intents,
    )

    state = sources_setup._gather_state()["usbsink"]

    assert state["desired"] is True
    assert state["effective"] == "degraded"
    assert "not advertised" in str(state["degradedReason"])


def test_gather_state_usbsink_unavailable_when_gadget_unit_missing(monkeypatch):
    available = {
        AIRPLAY_UNIT,
        *sources_setup.BLUETOOTH_RUNTIME_UNITS,
        SPOTIFY_CONNECT_UNIT,
        sources_setup.USBSINK_UNIT,
    }
    _patch_state_dependencies(
        monkeypatch,
        usb_ready=True,
        available_units=available,
    )

    state = sources_setup._gather_state()["usbsink"]

    assert state["available"] is False
    assert state["effective"] == "unavailable"
    assert "composite gadget unit" in str(state["unavailableReason"])


@pytest.mark.parametrize("enabled", [True, False])
def test_apply_usbsink_delegates_once_to_shared_coordinator(
    monkeypatch, enabled,
):
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda _unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_capability", lambda: (True, ""))
    monkeypatch.setattr(
        sources_setup,
        "request_source_intent",
        lambda source, desired: calls.append((source, desired)),
    )

    sources_setup._apply("usbsink", enabled)

    assert calls == [(Source.USBSINK, enabled)]


def test_apply_usbsink_rejects_missing_gadget_before_requesting_intent(
    monkeypatch,
):
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(
        sources_setup,
        "_unit_available",
        lambda unit: unit != sources_setup.USBSINK_GADGET_UNIT,
    )
    monkeypatch.setattr(sources_setup, "_usbsink_capability", lambda: (True, ""))
    monkeypatch.setattr(
        sources_setup,
        "request_source_intent",
        lambda *_args: pytest.fail("must not request unavailable USB intent"),
    )

    with pytest.raises(RuntimeError, match="composite gadget unit"):
        sources_setup._apply("usbsink", True)


def test_apply_usbsink_off_persists_when_gadget_is_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda _unit: False)
    monkeypatch.setattr(
        sources_setup,
        "_usbsink_capability",
        lambda: (False, "USB output DAC uses the shared port"),
    )
    monkeypatch.setattr(
        sources_setup,
        "request_source_intent",
        lambda source, desired: calls.append((source, desired)),
    )

    sources_setup._apply("usbsink", False)

    assert calls == [(Source.USBSINK, False)]
