# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P3/P4 default-flip: default-resolution of the fan-in coupling + USB combo.

Pins the campaign brief's contracts + the review remediation:
  - marker semantics (operator coupling choice survives the auto pass, while USB
    combo permission still follows canonical source intent);
  - eligibility no-ops across the validated box shapes
    (jts.local eligible; jts3 roleful; jts5 composite; jts4 streambox loopback);
  - the USB combo arms ONLY on a gadget box that ALSO has canonical USB intent
    On, local sources allowed for the current role, and a ready derived
    lifecycle mirror — B2 capability-gated arming + split-brain fix;
  - off a combo box the fan-in keys are EXPLICIT `disabled`, never unset — F5
    jasper.env-precedence fix;
  - a grouped box resolves loopback (not a route-blocked ok=False) — F3;
  - an unreadable topology resolves loopback (fail-closed) in the auto path — F4;
  - a stale JASPER_FANIN_RING_SLOTS self-heals before the gates so it does not
    disarm a box a manual arm would keep — F6;
  - idempotence (auto pass twice = one write).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.env_file import read_value
from jasper.fanin import coupling_auto as ca
from jasper.fanin import coupling_reconcile as cr
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
)


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """Keep effective-env tests independent of the developer host's /etc state."""
    jasper_env = tmp_path / "jasper.env"
    jasper_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(cr, "JASPER_ENV_PATH", str(jasper_env))


# --------------------------------------------------------------------------
# Pure decision: is_operator_choice + resolve_auto_decision
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("operator", True),
        ("OPERATOR", True),
        ("  operator  ", True),
        (None, False),
        ("", False),
        ("auto", False),
        ("garbage", False),
        ("1", False),
    ],
)
def test_is_operator_choice(raw, expected):
    assert ca.is_operator_choice(raw) is expected


def test_streambox_profile_keeps_ring_loopback_while_usb_combo_arms(monkeypatch):
    """Ring hardware validation and USB DIRECT eligibility are independent."""

    from jasper import install_profile

    monkeypatch.setattr(
        install_profile,
        "read_install_profile",
        lambda: install_profile.STREAMBOX_INSTALL_PROFILE,
    )
    decision = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=ca.default_ring_gates(),
    )

    assert decision.coupling == COUPLING_LOOPBACK
    assert decision.combo_armed is True
    assert decision.usb_combo_actions
    assert all(action.value == "enabled" for action in decision.usb_combo_actions)
    assert "streambox profile" in decision.reason


def test_usbsink_effective_gate_reads_canonical_source_state_and_role(monkeypatch):
    from jasper import source_intent
    from jasper.local_sources import guard
    from jasper.music_sources import Source

    seen = []
    monkeypatch.setattr(
        source_intent,
        "source_intent_enabled",
        lambda source: seen.append(source) or True,
    )
    monkeypatch.setattr(guard, "local_sources_allowed", lambda: (True, None))
    monkeypatch.setattr(ca, "_usbsink_lifecycle_ready", lambda: True)

    assert ca.usbsink_effectively_enabled() is True
    assert seen == [Source.USBSINK]


def test_usbsink_desired_on_but_follower_parked_disarms_effective_gate(
    monkeypatch,
):
    from jasper import source_intent
    from jasper.local_sources import guard

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(
        guard,
        "local_sources_allowed",
        lambda: (False, "bonded follower"),
    )
    monkeypatch.setattr(
        ca,
        "_usbsink_lifecycle_ready",
        lambda: pytest.fail("parked role must short-circuit readiness probe"),
    )

    assert ca.usbsink_effectively_enabled() is False


def test_usbsink_desired_on_but_derived_lifecycle_not_ready_disarms(
    monkeypatch,
):
    from jasper import source_intent
    from jasper.local_sources import guard

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(guard, "local_sources_allowed", lambda: (True, None))
    monkeypatch.setattr(ca, "_usbsink_lifecycle_ready", lambda: False)

    assert ca.usbsink_effectively_enabled() is False


def test_usbsink_canonical_off_dominates_stale_enabled_mirror(monkeypatch):
    from jasper import source_intent

    monkeypatch.setattr(source_intent, "source_intent_enabled", lambda _source: False)
    monkeypatch.setattr(
        ca,
        "_usbsink_lifecycle_ready",
        lambda: pytest.fail("canonical Off must short-circuit readiness probe"),
    )

    assert ca.usbsink_effectively_enabled() is False


def _pass_gate(detail="ok"):
    return lambda: (True, detail)


def _fail_gate(detail="ineligible"):
    return lambda: (False, detail)


def test_decision_operator_marker_freezes_coupling_but_not_usb_permission():
    d = ca.resolve_auto_decision(
        marker_raw="operator",
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=(("assets", _pass_gate()),),
        current_coupling=COUPLING_SHM_RING,
    )
    assert d.owned is False
    assert d.coupling == COUPLING_SHM_RING
    assert d.combo_armed is True
    assert all(action.value == "enabled" for action in d.usb_combo_actions)


def test_decision_eligible_gadget_box_with_intent_resolves_ring_and_combo_on():
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=(("assets", _pass_gate()), ("topology", _pass_gate())),
    )
    assert d.owned is True
    assert d.coupling == COUPLING_SHM_RING
    assert d.combo_armed is True
    assert [(a.action, a.key, a.value) for a in d.usb_combo_actions] == [
        ("set", ca.USB_DIRECT_ENV_VAR, "enabled"),
        ("set", ca.HOST_CLOCK_ENV_VAR, "enabled"),
        ("set", ca.CUSHION_DECAY_ENV_VAR, "enabled"),
    ]


def test_decision_gadget_without_usb_intent_does_not_arm_combo():
    """B2: gadget-capable hardware without USB-audio intent keeps the combo
    off (fan-in keys explicitly disabled)."""
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=False,
        ring_gates=(("assets", _pass_gate()),),
    )
    assert d.combo_armed is False
    assert [(a.action, a.key, a.value) for a in d.usb_combo_actions] == [
        ("set", ca.USB_DIRECT_ENV_VAR, "disabled"),
        ("set", ca.HOST_CLOCK_ENV_VAR, "disabled"),
        ("set", ca.CUSHION_DECAY_ENV_VAR, "disabled"),
    ]


def test_decision_usb_intent_without_gadget_does_not_arm_combo():
    """Intent on but no gadget hardware → combo off (both signals required)."""
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=False,
        usb_intent_enabled=True,
        ring_gates=(("assets", _pass_gate()),),
    )
    assert d.combo_armed is False
    assert all(a.value == "disabled" for a in d.usb_combo_actions)


def test_decision_first_failing_gate_short_circuits_to_loopback():
    # The topology gate fails (a roleful box) -> loopback, reason names the gate,
    # and the later gates are NOT consulted.
    calls: list[str] = []

    def spy(name, ok):
        def g():
            calls.append(name)
            return (ok, name)

        return g

    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=False,
        usb_intent_enabled=False,
        ring_gates=(
            ("assets", spy("assets", True)),
            ("topology", spy("topology", False)),
            ("geometry", spy("geometry", True)),
        ),
    )
    assert d.owned is True
    assert d.coupling == COUPLING_LOOPBACK
    assert "topology" in d.reason
    assert calls == ["assets", "topology"]  # short-circuit: geometry never ran


def test_decision_gate_raising_fails_safe_to_loopback():
    def boom():
        raise OSError("topology unreadable")

    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=False,
        usb_intent_enabled=False,
        ring_gates=(("assets", _pass_gate()), ("topology", boom)),
    )
    assert d.coupling == COUPLING_LOOPBACK
    assert "topology" in d.reason


def test_combo_is_armed_requires_both_signals():
    assert ca.combo_is_armed(gadget_present=True, usb_intent_enabled=True) is True
    assert ca.combo_is_armed(gadget_present=True, usb_intent_enabled=False) is False
    assert ca.combo_is_armed(gadget_present=False, usb_intent_enabled=True) is False
    assert ca.combo_is_armed(gadget_present=False, usb_intent_enabled=False) is False


def test_usb_combo_authority_has_no_runtime_health_override():
    """Only gadget availability plus canonical intent can resolve composition."""

    decision = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=(),
    )

    assert decision.combo_armed is True
    assert not hasattr(decision, "fallback_active")


def test_usb_combo_actions_enabled_when_armed():
    acts = ca.usb_combo_actions(armed=True)
    assert all(a.action == "set" and a.value == "enabled" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_usb_combo_actions_explicit_disabled_when_not_armed():
    # F5: explicit `disabled` (NOT unset) so a stale jasper.env `enabled` can't win.
    acts = ca.usb_combo_actions(armed=False)
    assert all(a.action == "set" and a.value == "disabled" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_live_gadget_probe_reads_shared_resolved_capability(monkeypatch):
    monkeypatch.setattr(
        ca,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=True),
    )
    assert ca.read_usb_gadget_available() is True

    monkeypatch.setattr(
        ca,
        "current_usb_data_role",
        lambda: SimpleNamespace(gadget_available=False),
    )
    assert ca.read_usb_gadget_available() is False


def test_resolved_choice_label():
    assert ca.resolved_choice_label("operator") == "operator"
    assert ca.resolved_choice_label(None) == "auto"
    assert ca.resolved_choice_label("garbage") == "auto"


# --------------------------------------------------------------------------
# reconcile_auto orchestration — env writes, marker no-op, combo, idempotence
# --------------------------------------------------------------------------


def _stub_ring_gates(monkeypatch, *, eligible: bool):
    """Make both the auto-decision ring gates AND the arm preflights resolve to
    ``eligible``. Stubs at the reconciler boundary so no /dev/shm or topology
    file is touched. Also stubs the route gate + the slot self-heal so a real
    grouping read / conf.d read is never needed."""
    assets = ("ring_assets", lambda: (eligible, "assets"))
    topo = ("ring_topology", lambda: (eligible, "topology"))
    monkeypatch.setattr(ca, "default_ring_gates", lambda: (assets, topo))
    monkeypatch.setattr(cr, "ring_route_ready", lambda route_mode: (eligible, "route"))
    monkeypatch.setattr(cr, "ring_geometry_ready", lambda text: (eligible, "geom"))
    monkeypatch.setattr(cr, "ring_slot_geometry_ready", lambda text: (eligible, "slots"))
    # The F6 slot self-heal runs before the gates; keep it a no-op in unit tests
    # (it re-reads the conf.d otherwise). Its own behavior is covered separately.
    monkeypatch.setattr(cr, "_migrate_stale_fanin_ring_slots", lambda snap, reason: snap)
    # Arm-spine preflights (only reached when eligible + coupling flips to ring).
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(cr, "ring_assets_ready", lambda: (eligible, "assets"))
    monkeypatch.setattr(cr, "ring_topology_ready_strict", lambda: (eligible, "topology"))
    monkeypatch.setattr(cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None)


def _auto(
    fanin,
    outputd,
    *,
    gadget,
    restarts,
    usb_intent=None,
    camilla_ok=True,
    leader=False,
    fanin_ok=True,
    camilla_stop_ok=True,
    camilla_start_ok=True,
):
    """Run reconcile_auto with recorded daemon ops.

    ``usb_intent`` defaults to ``gadget`` so a test that says "gadget on" gets the
    combo armed unless it opts out — matching the common jts.local case (gadget
    present AND USB audio on). ``camilla_stop``/``camilla_start`` are the coordinated
    combo-restart pause/resume ops (record ``camilla_stop``/``camilla_start`` in
    ``restarts``) so the RTTIME-SIGKILL coordination can be exercised hardware-free."""
    if usb_intent is None:
        usb_intent = gadget

    def rf():
        restarts.append("fanin")
        return (fanin_ok, "" if fanin_ok else "fanin restart failed")

    def ro():
        restarts.append("outputd")
        return (True, "")

    def rsc():
        restarts.append("camilla_stop")
        return (camilla_stop_ok, "" if camilla_stop_ok else "camilla stop failed")

    def rstc():
        restarts.append("camilla_start")
        return (camilla_start_ok, "" if camilla_start_ok else "camilla start failed")

    def rc(coupling):
        restarts.append(f"camilla:{coupling}")
        return (camilla_ok, "reconciled" if camilla_ok else "bad")

    return cr.reconcile_auto(
        reason="t",
        env_path=fanin,
        outputd_env_path=outputd,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        restart_fanin=rf,
        restart_outputd=ro,
        stop_camilla=rsc,
        start_camilla=rstc,
        reconcile_camilla=rc,
        active_leader_check=lambda: leader,
    )

def test_auto_operator_marker_preserves_coupling_and_converges_usb_combo(
    tmp_path, monkeypatch,
):
    """The transport is frozen, but canonical USB On still arms capture."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
    )
    _stub_ring_gates(monkeypatch, eligible=True)  # would resolve ring if owned
    # The coupling owner is bypassed; only combo convergence may restart fan-in.
    called = {"n": 0}
    monkeypatch.setattr(
        cr, "reconcile_coupling", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.owned is False
    assert r.ok is True
    # Nit8: report the frozen box's real coupling, not a hardcoded loopback.
    assert r.coupling == COUPLING_SHM_RING
    assert called["n"] == 0
    assert "fanin" in restarts
    # Marker + exact coupling survive while the USB combo becomes explicit On.
    text = fanin.read_text()
    assert "JASPER_FANIN_COUPLING_CHOICE=operator" in text
    assert f"{ca.USB_DIRECT_ENV_VAR}=enabled" in text


@pytest.mark.parametrize("coupling", [COUPLING_LOOPBACK, COUPLING_SHM_RING])
@pytest.mark.parametrize(
    "initial_value,usb_intent,expected_value",
    [
        ("enabled", False, "disabled"),
        ("disabled", True, "enabled"),
    ],
)
def test_operator_frozen_coupling_still_converges_usb_authorization(
    tmp_path,
    monkeypatch,
    coupling,
    initial_value,
    usb_intent,
    expected_value,
):
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        f"JASPER_FANIN_CAMILLA_COUPLING={coupling}\n"
        f"{ca.USB_DIRECT_ENV_VAR}={initial_value}\n"
        f"{ca.HOST_CLOCK_ENV_VAR}={initial_value}\n"
        f"{ca.CUSHION_DECAY_ENV_VAR}={initial_value}\n"
    )
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []

    result = _auto(
        fanin,
        outputd,
        gadget=True,
        usb_intent=usb_intent,
        restarts=restarts,
    )

    assert result.ok is True
    assert result.owned is False
    assert result.coupling == coupling
    assert cr.read_persisted_coupling(fanin) == coupling
    assert read_value(fanin.read_text(), ca.USB_DIRECT_ENV_VAR) == expected_value
    assert "fanin" in restarts
    assert not any(item.startswith("camilla:") for item in restarts)


def test_auto_eligible_gadget_box_with_intent_arms_ring_and_combo(
    tmp_path, monkeypatch
):
    """jts.local shape: solo, ring-eligible, gadget present, USB audio ON resolves
    shm_ring and enables the fan-in direct-capture combo."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_SHM_RING
    assert r.combo_armed is True
    assert r.usb_combo_changed is True
    assert r.ok is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "enabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "enabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "enabled"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_SHM_RING
    # Auto NEVER stamps the operator marker (stays auto-owned).
    assert read_value(text, ca.COUPLING_CHOICE_ENV_VAR) is None
    assert r.restarted_fanin_for_combo is False


def test_auto_gadget_present_but_usb_audio_off_does_not_arm_combo(tmp_path, monkeypatch):
    """B2: a gadget-capable box with USB audio Off must not arm the combo; it
    writes explicit-off values rather than enabled ones."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, usb_intent=False, restarts=restarts
    )
    assert r.combo_armed is False
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    # The ring can still resolve (eligible), but the combo is off.
    assert r.coupling == COUPLING_SHM_RING


def test_auto_malformed_usb_intent_disarms_stale_combo_then_fails(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Invalid canonical USB intent must not abort before the safe disarm.

    The operator marker freezes the coupling choice only; it cannot preserve a
    stale USB DIRECT lane whose authorization is unreadable.
    """

    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
        f"{ca.USB_DIRECT_ENV_VAR}=enabled\n"
        f"{ca.HOST_CLOCK_ENV_VAR}=enabled\n"
        f"{ca.CUSHION_DECAY_ENV_VAR}=enabled\n"
        "JASPER_UNRELATED_SOURCE_SENTINEL=enabled\n"
    )
    outputd.write_text("")
    monkeypatch.setattr(
        cr,
        "_migrate_stale_fanin_ring_slots",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed USB safety disarm must not run unrelated ring migrations"
        ),
    )

    def invalid_usb_intent():
        raise RuntimeError("bad USB intent value")

    monkeypatch.setattr(
        ca,
        "usbsink_effectively_enabled",
        invalid_usb_intent,
    )
    restarts: list[str] = []
    caplog.set_level("ERROR", logger=cr.__name__)

    result = cr.reconcile_auto(
        reason="malformed_usb_test",
        env_path=fanin,
        outputd_env_path=outputd,
        gadget_present=True,
        restart_fanin=lambda: (restarts.append("fanin"), (True, ""))[1],
        restart_outputd=lambda: (restarts.append("outputd"), (True, ""))[1],
        reconcile_camilla=lambda _coupling: (True, "reconciled"),
        active_leader_check=lambda: False,
    )

    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "disabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "disabled"
    assert read_value(text, "JASPER_UNRELATED_SOURCE_SENTINEL") == "enabled"
    assert read_value(text, ca.COUPLING_CHOICE_ENV_VAR) == "operator"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_LOOPBACK
    assert restarts == ["fanin"]
    assert result.combo_armed is False
    assert result.usb_combo_changed is True
    assert result.restarted_fanin_for_combo is True
    assert result.usb_intent_enabled is False
    assert result.ok is False
    assert "bad USB intent value" in result.detail
    assert "result=auto_usb_intent_fail_closed" in caplog.text


def test_auto_jts3_roleful_is_loopback_combo_off(tmp_path, monkeypatch):
    """jts3 shape: roleful topology (a ring gate fails) + no gadget -> loopback,
    combo written to explicit OFF (F5), and the arm never runs (no ring transition)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.combo_armed is False
    # No gadget: combo keys written to EXPLICIT off (F5 — defeats jasper.env
    # precedence), not left absent.
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, COUPLING_ENV_VAR) in (None, COUPLING_LOOPBACK)


def test_auto_jts5_composite_is_loopback(tmp_path, monkeypatch):
    """jts5 shape: composite dual-DAC (a ring gate fails) + no gadget -> loopback."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    r = _auto(fanin, outputd, gadget=False, restarts=[])
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.combo_armed is False


def test_auto_jts4_streambox_no_fanin_stack_exits_clean(tmp_path, monkeypatch):
    """Legacy jts4 shape: ineligible ring and no gadget exits cleanly.

    Current streamboxes run this owner because they share fan-in and may need
    USB DIRECT; the install-profile gate independently keeps ring on loopback.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    # Already loopback (a streambox never armed anything).
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    r = _auto(fanin, outputd, gadget=False, restarts=[])
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.ok is True
    assert r.combo_armed is False
    # Combo written to explicit OFF (F5).
    assert read_value(fanin.read_text(), ca.USB_DIRECT_ENV_VAR) == "disabled"


def test_auto_gadget_lost_clears_stale_combo_keys(tmp_path, monkeypatch):
    """Single-writer discipline: a box that previously had the combo armed but LOST
    the gadget has the fan-in combo keys driven to their explicit OFF value
    (`disabled`, F5: not unset, so a stale jasper.env `enabled` can't win).
    fan-in restarts to release the gadget. USB audio is left unavailable."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_USB_DIRECT=enabled\n"
        "JASPER_FANIN_HOST_CLOCK=enabled\n"
        "JASPER_FANIN_RESAMPLER_CUSHION_DECAY=enabled\n"
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
    )
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert r.usb_combo_changed is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "disabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "disabled"
    # fan-in restarts to release the gadget; no second audio owner is involved.
    assert "fanin" in restarts


def test_auto_is_idempotent_second_pass_writes_nothing(tmp_path, monkeypatch):
    """Idempotence: two identical auto passes converge with ONE write. The second
    pass reports no combo change and leaves fanin.env byte-identical."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)

    r1 = _auto(fanin, outputd, gadget=True, restarts=[])
    assert r1.usb_combo_changed is True
    after_first_fanin = fanin.read_text()

    restarts2: list[str] = []
    r2 = _auto(fanin, outputd, gadget=True, restarts=restarts2)
    assert r2.usb_combo_changed is False
    assert fanin.read_text() == after_first_fanin
    # The second pass writes nothing and bounces NO data-plane daemon. It DOES
    # re-run the lightweight camilla confirm (the shm_ring CONFIRM-path self-heal),
    # which is by design — that never glitches audio. Assert only that no fan-in /
    # outputd restart fired.
    assert "fanin" not in restarts2
    assert "outputd" not in restarts2


def test_auto_combo_only_change_forces_fanin_restart(tmp_path, monkeypatch):
    """A combo-only change on an already-at-desired-coupling box (loopback, no
    ring transition -> confirm path, no bounce) still needs fan-in restarted so the
    new combo takes effect. The auto pass issues that one restart."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    # Already loopback; gadget present + USB audio on but combo NOT yet written.
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    outputd.write_text("")
    # Ineligible for ring (so coupling stays loopback = no arm bounce), gadget+intent.
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.combo_armed is True
    assert r.coupling == COUPLING_LOOPBACK
    # The confirm path did not restart fan-in, so the combo forced one.
    assert r.restarted_fanin_for_combo is True
    assert restarts.count("fanin") == 1
    # LOOPBACK skips the camilla coordination (snd-aloop decouples the two): the
    # combo fan-in restart does NOT pause/resume camilla.
    assert "camilla_stop" not in restarts
    assert "camilla_start" not in restarts


def _armed_shm_ring_outputd() -> str:
    """The outputd.env an ALREADY-armed shm_ring box carries. With this present a
    subsequent reconcile sees NO outputd move, so the coupling stays put and the
    reconcile takes the lightweight CONFIRM path (not _arm_ring) — the shape that
    makes the combo force a bare fan-in restart the coordination must wrap."""
    return cr._apply_actions("", cr._outputd_actions(COUPLING_SHM_RING, ""))[0]


def test_auto_combo_change_on_ring_pauses_camilla_around_fanin_restart(
    tmp_path, monkeypatch
):
    """RTTIME-SIGKILL fix — the load-bearing sequence. On a LIVE shm_ring box a
    combo-only change takes the confirm path (no arm bounce) and the combo forces a
    fan-in restart; that restart MUST pause CamillaDSP first and resume it after, so
    the ioplug capture reader can't busy-spin the SCHED_FIFO daemon into a SIGKILL."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    # Already shm_ring (the live-ring coupling) + standby already 1, so the ONLY
    # change is the combo fan-in keys -> confirm path -> combo-forced fan-in restart.
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.coupling == COUPLING_SHM_RING
    assert r.usb_combo_changed is True
    assert r.restarted_fanin_for_combo is True
    assert r.ok is True
    # The confirm path did NOT reconcile-bounce fan-in; the ONE fan-in restart is the
    # combo's, and it is wrapped: camilla stopped BEFORE, started AFTER.
    assert restarts.count("fanin") == 1
    assert "camilla_stop" in restarts and "camilla_start" in restarts
    assert restarts.index("camilla_stop") < restarts.index("fanin")
    assert restarts.index("fanin") < restarts.index("camilla_start")


def test_auto_ring_combo_camilla_stop_failure_aborts_fanin_restart(tmp_path, monkeypatch):
    """Failure honesty: if camilla can't be paused on a live ring, the combo fan-in
    restart is ABORTED (restarting fan-in with camilla live is what SIGKILLs it),
    surfaced ok=False, and camilla is started back — never left stopped-forever."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, restarts=restarts,
        camilla_stop_ok=False,
    )
    assert r.ok is False
    assert r.restarted_fanin_for_combo is False
    assert "fanin" not in restarts  # fan-in restart was aborted
    assert "camilla_stop" in restarts and "camilla_start" in restarts  # start-back tried
    assert "aborted fan-in restart" in (r.detail or "")


def test_auto_ring_combo_fanin_restart_failure_still_resumes_camilla(tmp_path, monkeypatch):
    """Failure honesty: if the combo fan-in restart fails AFTER camilla was stopped,
    camilla is STILL resumed (start called) — never left stopped-forever — and the
    failure is surfaced ok=False."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    outputd.write_text(_armed_shm_ring_outputd())
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, restarts=restarts,
        fanin_ok=False,
    )
    assert r.ok is False
    assert r.restarted_fanin_for_combo is False
    assert restarts.index("camilla_stop") < restarts.index("fanin")
    assert restarts.index("fanin") < restarts.index("camilla_start")


# --------------------------------------------------------------------------
# F3 — a grouped box resolves loopback (not a route-blocked ok=False)
# --------------------------------------------------------------------------


def _stub_ring_gates_except_route(monkeypatch, *, eligible: bool):
    """Like _stub_ring_gates but leaves the REAL ring_route_ready in place, so a
    route decision (grouped vs solo) flows through for the F3 test."""
    assets = ("ring_assets", lambda: (eligible, "assets"))
    topo = ("ring_topology", lambda: (eligible, "topology"))
    monkeypatch.setattr(ca, "default_ring_gates", lambda: (assets, topo))
    monkeypatch.setattr(cr, "ring_geometry_ready", lambda text: (eligible, "geom"))
    monkeypatch.setattr(cr, "ring_slot_geometry_ready", lambda text: (eligible, "slots"))
    monkeypatch.setattr(cr, "_migrate_stale_fanin_ring_slots", lambda snap, reason: snap)
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(cr, "ring_assets_ready", lambda: (eligible, "assets"))
    monkeypatch.setattr(cr, "ring_topology_ready_strict", lambda: (eligible, "topology"))
    monkeypatch.setattr(cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None)


def test_auto_grouped_leader_resolves_loopback_and_succeeds(tmp_path, monkeypatch):
    """F3: a grouped (active-leader) box would pass the asset/topology/geometry gates
    on its stereo shape, but the ring is not supported while grouped. The route gate
    must resolve loopback so the reconcile succeeds — NOT resolve shm_ring and then
    get route-blocked with ok=False (a failing boot unit on a healthy box)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    # Everything eligible EXCEPT we let the real route gate see an active leader.
    _stub_ring_gates_except_route(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=False, restarts=restarts, leader=True
    )
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.ok is True
    # It resolved loopback via the route gate, so no ring arm / no block.
    assert r.coupling_result is not None
    assert r.coupling_result.direction != "blocked"


def test_ring_route_ready_blocks_grouped_allows_solo():
    ok_solo, _ = cr.ring_route_ready("solo")
    assert ok_solo is True
    ok_unknown, _ = cr.ring_route_ready("unknown")
    assert ok_unknown is True  # indeterminate never blocks a legitimate solo arm
    ok_leader, detail = cr.ring_route_ready("active_leader")
    assert ok_leader is False
    assert "loopback" in detail


# --------------------------------------------------------------------------
# F4 — the auto topology gate fails CLOSED on an unreadable topology
# --------------------------------------------------------------------------


def test_ring_topology_strict_fails_closed_on_unreadable(monkeypatch):
    """F4: the strict topology gate (auto path) resolves NOT-eligible when the
    topology cannot be read, where the human-arm gate fails open."""
    from jasper.output_topology import OutputTopologyError

    def boom():
        raise OutputTopologyError("topology file corrupt")

    monkeypatch.setattr(cr, "load_output_topology_strict", boom, raising=False)
    import jasper.output_topology as ot

    monkeypatch.setattr(ot, "load_output_topology_strict", boom)

    open_ok, open_detail = cr.ring_topology_ready()  # human arm: fail-open
    assert open_ok is True
    assert "deferring to outputd" in open_detail

    strict_ok, strict_detail = cr.ring_topology_ready_strict()  # auto: fail-closed
    assert strict_ok is False
    assert "fail-closed" in strict_detail


# --------------------------------------------------------------------------
# F6 — a stale JASPER_FANIN_RING_SLOTS self-heals BEFORE the auto gates run
# --------------------------------------------------------------------------


def test_auto_stale_ring_slots_self_heals_and_keeps_ring(tmp_path, monkeypatch):
    """F6: a box armed with a stale JASPER_FANIN_RING_SLOTS=8 line must NOT be
    disarmed to loopback — the auto pass runs the SAME slot self-heal a manual arm
    does before the slot gate, so the residue is overridden and the ring resolves."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
        "JASPER_FANIN_RING_SLOTS=8\n"
    )
    outputd.write_text("")

    # Assets/topology/route/geometry eligible; conf.d says n_slots=2 so the stale
    # `=8` is shear-prone and self-heals. Use the REAL slot gate + migration so the
    # F6 wiring is exercised end to end.
    assets = ("ring_assets", lambda: (True, "assets"))
    topo = ("ring_topology", lambda: (True, "topology"))
    monkeypatch.setattr(ca, "default_ring_gates", lambda: (assets, topo))
    monkeypatch.setattr(cr, "ring_route_ready", lambda route_mode: (True, "route"))
    monkeypatch.setattr(cr, "ring_geometry_ready", lambda text: (True, "geom"))
    monkeypatch.setattr(cr, "ring_assets_ready", lambda: (True, "assets"))
    monkeypatch.setattr(cr, "ring_topology_ready_strict", lambda: (True, "topology"))
    monkeypatch.setattr(cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None)
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    # conf.d Ring-A n_slots = 2 (the pinned default); the on-disk `=8` disagrees.
    monkeypatch.setattr(ra, "ring_conf_n_slots", lambda pcm, conf_d=None: 2)

    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert r.owned is True
    # The stale slots line was overridden (self-heal), so the ring resolved — NOT
    # disarmed to loopback.
    assert r.coupling == COUPLING_SHM_RING
    assert read_value(fanin.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


def test_auto_stale_base_ring_slots_self_heals_and_keeps_ring(tmp_path, monkeypatch):
    """F6 through the real systemd env chain.

    A stale ``JASPER_FANIN_RING_SLOTS=8`` in /etc/jasper/jasper.env is still the
    effective fan-in value when fanin.env has no later override. The auto pass must
    write the coherent fanin.env override before its slot gate runs.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    jasper_env = tmp_path / "jasper.env"
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n", encoding="utf-8")
    outputd.write_text("", encoding="utf-8")
    jasper_env.write_text("JASPER_FANIN_RING_SLOTS=8\n", encoding="utf-8")
    monkeypatch.setattr(cr, "JASPER_ENV_PATH", str(jasper_env))

    assets = ("ring_assets", lambda: (True, "assets"))
    topo = ("ring_topology", lambda: (True, "topology"))
    monkeypatch.setattr(ca, "default_ring_gates", lambda: (assets, topo))
    monkeypatch.setattr(cr, "ring_route_ready", lambda route_mode: (True, "route"))
    monkeypatch.setattr(cr, "ring_geometry_ready", lambda text: (True, "geom"))
    monkeypatch.setattr(cr, "ring_assets_ready", lambda: (True, "assets"))
    monkeypatch.setattr(cr, "ring_topology_ready_strict", lambda: (True, "topology"))
    monkeypatch.setattr(cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None)
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "ring_conf_n_slots", lambda pcm, conf_d=None: 2)

    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)

    assert r.owned is True
    assert r.coupling == COUPLING_SHM_RING
    assert read_value(fanin.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


# --------------------------------------------------------------------------
# Fresh-install low-latency reproduction — pins the measurement doc's claim
# --------------------------------------------------------------------------
#
# docs/HANDOFF-usb-latency-measurement.md §2 ("this is what a fresh install
# ships") asserts that EVERY low-latency USB value is either a shipped code
# default or armed automatically by the coupling auto-pass on an eligible gadget
# box. That is a load-bearing promise (the ~55.5 ms measured number only holds if
# a fresh flash actually reproduces the measured config with no operator action).
# These tests PIN that promise, so a silent drift in any of the named values
# reddens here and the doc's claim is caught rather than becoming stale prose.
#
# Two halves, matching the doc's two §2 tables:
#   1. the host-clock combo the auto-pass ARMS on an eligible gadget box, and
#   2. the ring-geometry CODE DEFAULTS the doc's table names.
#
# ``ring_slots default == 2`` is pinned to config.rs's ``env_u32(…, 2)`` source
# text in tests/test_fanin_coupling_rust_contract.py
# (test_shm_ring_env_var_names_and_defaults_agree); here we only reference the
# Python constant that pin ties the Rust default to, so we do not duplicate the
# source-text read.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"


def test_fresh_install_auto_arms_exactly_the_documented_combo_block(
    tmp_path, monkeypatch
):
    """§2 combo table: on an ELIGIBLE gadget box (gadget present + usbsink intent
    enabled + ring-eligible topology) the auto-pass writes EXACTLY this block into
    fanin.env — the three combo flags ``enabled`` AND coupling ``shm_ring`` — and
    stamps NO operator marker. If the auto-pass ever stopped arming one of these,
    a fresh install would silently ship a slower config than the doc claims.
    """
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)

    r = _auto(fanin, outputd, gadget=True, restarts=[])

    assert r.owned is True
    assert r.combo_armed is True
    text = fanin.read_text()
    # EXACTLY the documented combo env block (measurement doc §2 host-clock table).
    documented_combo = {
        ca.USB_DIRECT_ENV_VAR: "enabled",
        ca.HOST_CLOCK_ENV_VAR: "enabled",
        ca.CUSHION_DECAY_ENV_VAR: "enabled",
        COUPLING_ENV_VAR: COUPLING_SHM_RING,
    }
    for key, value in documented_combo.items():
        assert read_value(text, key) == value, (
            f"fresh-install auto-pass must write {key}={value} on an eligible "
            "gadget box (measurement doc §2); it did not"
        )
    # Auto ownership is preserved — the auto-pass never freezes the box to an
    # operator choice (that would stop the combo re-arming across deploys).
    assert read_value(text, ca.COUPLING_CHOICE_ENV_VAR) is None


def test_fresh_install_ring_geometry_defaults_match_the_doc_table():
    """§2 ring-geometry table: the Camilla ring-emit geometry the doc names —
    chunksize 128 / target_level 128 / queuelimit 1 / rate_adjust off — and the
    2-slot Ring A default. These are shipped CODE defaults (no auto-pass needed);
    a fresh install reproduces them because they are the constant values. Pinning
    the literals here catches a silent drift the doc could not.
    """
    # The doc's table values are these constants; assert the literals so a drift
    # in the constant itself (not just its usage) reddens.
    assert RING_CAMILLA_CHUNKSIZE == 128
    assert RING_CAMILLA_TARGET_LEVEL == 128
    assert RING_CAMILLA_QUEUELIMIT == 1
    assert RING_CAMILLA_ENABLE_RATE_ADJUST is False
    # ring_slots default == 2 (config.rs env_u32(…, 2) is pinned to this constant
    # in test_fanin_coupling_rust_contract.py; referenced, not re-read here).
    assert DEFAULT_FANIN_RING_SLOTS == 2


def test_fresh_install_ring_geometry_emits_the_doc_table_values():
    """The same §2 ring-geometry values as they actually land in the emitted
    CamillaDSP ring config (``emit_flat_ring_config`` — the config the statefile
    seeder re-seeds on a ring-armed box). Pins the values end-to-end through the
    emitter, not just the constants, so a wiring change that dropped one can't slip
    past. Hardware-free: a pure YAML-text emit, no CamillaDSP process.
    """
    from jasper.sound.camilla_yaml import emit_flat_ring_config

    text = emit_flat_ring_config()
    assert "chunksize: 128" in text
    assert "target_level: 128" in text
    assert "queuelimit: 1" in text
    assert "enable_rate_adjust: false" in text
    # Both ring ends are the SHM-ring devices (the end-to-end shm_ring topology).
    assert 'device: "jts_ring_capture"' in text
    assert 'device: "jts_ring_playback"' in text


def test_fresh_install_cushion_decay_floor_default_is_576():
    """§2 host-clock table: JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES ships
    at the hardware-validated 576 floor (config.rs DEFAULT_CUSHION_DECAY_FLOOR_FRAMES).
    The Rust behavioural test (cushion_decay_floor_defaults_to_validated_floor)
    asserts the config equals the CONSTANT but is tautological on the constant's
    value; this source-text pin catches the constant itself silently drifting off
    the 576 the doc's table names. Hardware-free (the crate does not build on
    macOS; the CI Linux rust job builds it).
    """
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    text = _FANIN_CONFIG_RS.read_text(encoding="utf-8")
    assert "pub const DEFAULT_CUSHION_DECAY_FLOOR_FRAMES: u32 = 576;" in text, (
        "config.rs DEFAULT_CUSHION_DECAY_FLOOR_FRAMES must stay 576 — the "
        "hardware-validated floor the measurement doc §2 table ships"
    )
