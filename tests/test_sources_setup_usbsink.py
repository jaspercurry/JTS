# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the USB-sink toggle on /sources/.

Covers:
- VALID_SOURCES contains 'usbsink'
- _usbsink_available() reads config.txt correctly
- _gather_state() returns the right shape for usbsink
- _apply('usbsink', ...) drives systemctl with the right unit

The systemctl side is tested via monkeypatch on _systemctl; we don't
exec real systemd in unit tests.
"""
from __future__ import annotations

from jasper.local_sources import local_source_lifecycle
from jasper.music_sources import MUSIC_SOURCE_SPECS, Source
from jasper.web import sources_setup


# ----------------------------------------------------------------------
# VALID_SOURCES inclusion
# ----------------------------------------------------------------------


def test_usbsink_in_valid_sources():
    assert "usbsink" in sources_setup.VALID_SOURCES
    assert set(sources_setup.VALID_SOURCES) == {
        spec.wizard_key for spec in MUSIC_SOURCE_SPECS
    }


def test_systemd_units_come_from_local_source_lifecycle_registry():
    assert sources_setup.AIRPLAY_UNIT == (
        local_source_lifecycle(Source.AIRPLAY).intent_unit
    )
    assert sources_setup.SPOTIFY_CONNECT_UNIT == (
        local_source_lifecycle(Source.SPOTIFY).intent_unit
    )
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
    # Behaviour now ships as an ES module (post canonical-design migration);
    # the page references it and the jsonHeaders() CSRF plumbing lives there.
    assert '<script type="module" src="/assets/sources/js/main.js">' in html


# ----------------------------------------------------------------------
# _usbsink_available — dtoverlay presence in config.txt
# ----------------------------------------------------------------------


def _patch_config(monkeypatch, content: str | None):
    """Helper that points _usbsink_available at a controlled config.txt
    (or simulates a missing file by setting content=None)."""
    if content is None:
        monkeypatch.setattr(
            "builtins.open",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
        )
        return
    monkeypatch.setattr(
        sources_setup, "BOOT_CONFIG_PATH", "/tmp/test-config.txt",
    )
    # Use a function-scoped tempfile-like wrapper via monkeypatch on
    # `open`. Simpler: actually write to a real path.
    pass


def test_usbsink_available_returns_true_when_dtoverlay_present(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "# JTS install\n"
        "[pi5]\n"
        "dtoverlay=dwc2,dr_mode=peripheral\n"
        "country=US\n"
    )
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is True


def test_usbsink_available_tolerates_leading_whitespace(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("    dtoverlay=dwc2,dr_mode=peripheral\n")
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is True


def test_usbsink_available_returns_false_when_dtoverlay_missing(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "# JTS install\n"
        "[pi5]\n"
        "country=US\n"  # no dtoverlay line
    )
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is False


def test_usbsink_available_does_not_match_commented_line(monkeypatch, tmp_path):
    """A `#dtoverlay=...` (commented) shouldn't be treated as enabled.
    Without this check, an operator's experimental comment-out leaves
    the wizard claiming the feature is ready when it actually isn't."""
    cfg = tmp_path / "config.txt"
    cfg.write_text("# dtoverlay=dwc2,dr_mode=peripheral\n")
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is False


def test_usbsink_available_returns_false_on_missing_file(monkeypatch, tmp_path):
    """Not running on a Pi? config.txt won't exist. Fail-soft to
    `available=false` so the toggle is disabled."""
    monkeypatch.setattr(
        sources_setup, "BOOT_CONFIG_PATH", str(tmp_path / "missing.txt"),
    )
    assert sources_setup._usbsink_available() is False


# ----------------------------------------------------------------------
# _gather_state — usbsink row shape
# ----------------------------------------------------------------------


def test_gather_state_includes_usbsink(monkeypatch):
    """Verify the usbsink key is present with `enabled` + `available`.
    Mock systemctl and the BT probe so we get deterministic output."""
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )
    # BT side: pretend no hardware.
    async def fake_bt():
        return (False, False, False)
    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    # USB available probe: return False (not on a Pi).
    monkeypatch.setattr(
        sources_setup, "_usbsink_available", lambda: False,
    )

    state = sources_setup._gather_state()
    assert "usbsink" in state
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False


def test_gather_state_usbsink_available_when_dtoverlay_set(monkeypatch):
    """When dtoverlay is present, available should be True regardless
    of unit enabled state."""
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )
    async def fake_bt():
        return (False, False, False)
    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    state = sources_setup._gather_state()
    assert state["usbsink"]["available"] is True


def test_gather_state_usbsink_uac2_card_counts_as_enabled(monkeypatch):
    """The toggle must reflect host-visible truth, not only a healthy bridge.

    If the uac2 ALSA card is present while the Rust bridge is crash-looping,
    computers still see the USB audio device. Reporting enabled=False in that
    state is the split-brain bug this page exists to avoid. The host-visible
    signal is the UAC2Gadget card, NOT the always-on composite gadget unit.
    """
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    # Bridge daemon inactive, but the uac2 card is present (function composed).
    monkeypatch.setattr(sources_setup, "_unit_active", lambda unit: False)
    monkeypatch.setattr(sources_setup, "_unit_starting", lambda unit: False)
    monkeypatch.setattr(sources_setup, "_uac2_card_present", lambda: True)

    async def fake_bt():
        return (False, False, False)

    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)

    state = sources_setup._gather_state()

    assert state["usbsink"]["available"] is True
    assert state["usbsink"]["enabled"] is True
    assert "degradedReason" in state["usbsink"]
    assert "advertised" in state["usbsink"]["degradedReason"]


def test_gather_state_usbsink_unavailable_when_gadget_unit_missing(monkeypatch):
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)

    def available(unit: str) -> bool:
        return unit != sources_setup.USBSINK_GADGET_UNIT

    monkeypatch.setattr(sources_setup, "_unit_available", available)
    monkeypatch.setattr(
        sources_setup,
        "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )
    monkeypatch.setattr(sources_setup, "_uac2_card_present", lambda: False)

    async def fake_bt():
        return (False, False, False)

    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    state = sources_setup._gather_state()
    assert state["usbsink"]["available"] is False
    assert "composite gadget unit" in state["usbsink"]["unavailableReason"]


# ----------------------------------------------------------------------
# _apply — systemctl drives the right unit
# ----------------------------------------------------------------------


def test_apply_usbsink_enable_recomposes_gadget_between_persist_and_start(monkeypatch):
    # Enable is a four-step ordering: persist the enable intent via the root
    # source-intent helper (do NOT start yet), recompose the gadget so it adds
    # the uac2 function (card appears), start the bridge (its wait-card now
    # passes), then kick the fan-in coupling reconcile so the USB low-latency
    # combo arms this session (not only at the next boot/deploy). enable is
    # manage-unit-files, which the non-root broker can't run, so it goes through
    # the source-intent helper; the last three are broker (manage_units) calls.
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "_persist_source_intent",
        lambda unit, enabled: calls.append(("persist", unit, enabled)),
    )
    monkeypatch.setattr(
        sources_setup, "manage_units",
        lambda *units, **kw: calls.append(("manage", units, kw.get("verb")))
        or {"ok": True},
    )
    notice = sources_setup._apply("usbsink", True)
    assert notice is None  # kick succeeded → no reboot notice
    assert calls == [
        ("persist", sources_setup.USBSINK_UNIT, True),
        ("manage", (sources_setup.USBSINK_GADGET_UNIT,), "restart"),
        ("manage", (sources_setup.USBSINK_UNIT,), "start"),
        ("manage", (sources_setup.COUPLING_AUTO_UNIT,), "start"),
    ]


def test_apply_usbsink_enable_kicks_coupling_reconcile_start_only(monkeypatch):
    # The combo-arm kick must be a START (not restart/stop) of the reconciler:
    # jasper-web may only `start` jasper-fanin-coupling-auto.service via the
    # broker's START_ONLY_UNITS grant.
    kicks = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "_persist_source_intent", lambda *a, **k: None,
    )

    def fake_manage(*units, **kw):
        if units == (sources_setup.COUPLING_AUTO_UNIT,):
            kicks.append(kw.get("verb"))
        return {"ok": True}

    monkeypatch.setattr(sources_setup, "manage_units", fake_manage)
    sources_setup._apply("usbsink", True)
    assert kicks == ["start"]


def test_apply_usbsink_enable_failed_kick_returns_reboot_notice(monkeypatch):
    # If the coupling reconcile can't be kicked, the toggle intent still applied
    # (bridge enabled + gadget recomposed), but the combo did not arm live. The
    # caller must get a "takes effect after reboot" notice — never silent success.
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "_persist_source_intent", lambda *a, **k: None,
    )

    def fake_manage(*units, **kw):
        if units == (sources_setup.COUPLING_AUTO_UNIT,):
            return {"ok": False, "error": "restart broker unavailable"}
        return {"ok": True}

    monkeypatch.setattr(sources_setup, "manage_units", fake_manage)
    notice = sources_setup._apply("usbsink", True)
    assert notice == sources_setup._USBSINK_COMBO_REBOOT_NOTICE
    assert "reboot" in notice


def test_apply_usbsink_disable_failed_kick_returns_reboot_notice(monkeypatch):
    # Disable must also disarm the combo; a failed kick leaves fan-in
    # direct-capturing a gadget whose audio function was dropped, so the same
    # honest reboot notice fires.
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "_persist_source_intent", lambda *a, **k: None,
    )

    def fake_manage(*units, **kw):
        if units == (sources_setup.COUPLING_AUTO_UNIT,):
            return {"ok": False, "error": "boom"}
        return {"ok": True}

    monkeypatch.setattr(sources_setup, "manage_units", fake_manage)
    notice = sources_setup._apply("usbsink", False)
    assert notice == sources_setup._USBSINK_COMBO_REBOOT_NOTICE


def test_apply_usbsink_disable_recomposes_gadget_keeping_network(monkeypatch):
    # Disable persists the disable intent (root source-intent helper) + stops the
    # bridge (broker), then recomposes the gadget so it drops the uac2 function —
    # the host-visible audio device disappears while the always-on USB network
    # (ncm) stays up. The gadget is RESTARTED, never stopped; the persist is the
    # source-intent helper, never the broker's disable-now verb.
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "_persist_source_intent",
        lambda unit, enabled: calls.append(("persist", unit, enabled)),
    )
    monkeypatch.setattr(
        sources_setup, "manage_units",
        lambda *units, **kw: calls.append(("manage", units, kw.get("verb")))
        or {"ok": True},
    )
    sources_setup._apply("usbsink", False)
    assert calls[0] == ("persist", sources_setup.USBSINK_UNIT, False)
    assert calls[1] == ("manage", (sources_setup.USBSINK_UNIT,), "stop")
    assert calls[2] == ("manage", (sources_setup.USBSINK_GADGET_UNIT,), "restart")
    # Then the coupling reconcile is kicked (start-only) to disarm the combo.
    assert calls[3] == ("manage", (sources_setup.COUPLING_AUTO_UNIT,), "start")
    # The gadget is never stopped — that would drop the always-on USB network.
    assert not any(
        c == ((sources_setup.USBSINK_GADGET_UNIT,), "stop") for c in calls
    )


def test_apply_usbsink_rejects_missing_gadget_unit(monkeypatch):
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)

    def available(unit: str) -> bool:
        return unit != sources_setup.USBSINK_GADGET_UNIT

    monkeypatch.setattr(sources_setup, "_unit_available", available)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    try:
        sources_setup._apply("usbsink", True)
    except RuntimeError as e:
        assert "composite gadget unit" in str(e)
    else:  # pragma: no cover - assertion shape
        raise AssertionError("missing gadget unit should reject USB toggle")
