# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P3/P4 default-flip: default-resolution of the fan-in coupling + USB combo.

Pins the campaign brief's contracts + the review remediation:
  - marker semantics (operator choice survives the auto pass; the operator-frozen
    result reports the box's ACTUAL persisted coupling, not a hardcoded loopback);
  - eligibility no-ops across the four validated box shapes
    (jts.local eligible; jts3 roleful; jts5 composite; jts4 fanin-less);
  - the USB combo arms ONLY on a gadget box that ALSO has USB audio turned on
    (jasper-usbsink.service enabled) — B2 fleet-wide-arming fix;
  - BOTH combo halves are written together: fan-in keys in fanin.env AND the
    JASPER_USBSINK_AUDIO_STANDBY key in usbsink.env, with jasper-usbsink restarted
    on a standby change — B1 split-brain fix;
  - off a combo box the keys are EXPLICIT `disabled`/`0`, never unset — F5
    jasper.env-precedence fix;
  - a grouped box resolves loopback (not a route-blocked ok=False) — F3;
  - an unreadable topology resolves loopback (fail-closed) in the auto path — F4;
  - a stale JASPER_FANIN_RING_SLOTS self-heals before the gates so it does not
    disarm a box a manual arm would keep — F6;
  - idempotence (auto pass twice = one write).
"""

from __future__ import annotations

from pathlib import Path

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


def _pass_gate(detail="ok"):
    return lambda: (True, detail)


def _fail_gate(detail="ineligible"):
    return lambda: (False, detail)


def test_decision_operator_marker_is_complete_no_op():
    d = ca.resolve_auto_decision(
        marker_raw="operator",
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=(("assets", _pass_gate()),),
    )
    assert d.owned is False
    # No env actions at all — the operator's revert must not be touched.
    assert d.usb_combo_actions == ()
    assert d.usbsink_standby_actions == ()


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
    assert [(a.action, a.key, a.value) for a in d.usbsink_standby_actions] == [
        ("set", ca.USBSINK_STANDBY_ENV_VAR, "1"),
    ]


def test_decision_gadget_without_usb_intent_does_not_arm_combo():
    """B2: the gadget dtoverlay is fleet-wide; without USB-audio intent the combo
    stays OFF (explicit disabled/0), even though the ring may still resolve."""
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
    assert [(a.action, a.key, a.value) for a in d.usbsink_standby_actions] == [
        ("set", ca.USBSINK_STANDBY_ENV_VAR, "0"),
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
    assert d.usbsink_standby_actions[0].value == "0"


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


def test_usb_combo_actions_enabled_when_armed():
    acts = ca.usb_combo_actions(armed=True)
    assert all(a.action == "set" and a.value == "enabled" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_usb_combo_actions_explicit_disabled_when_not_armed():
    # F5: explicit `disabled` (NOT unset) so a stale jasper.env `enabled` can't win.
    acts = ca.usb_combo_actions(armed=False)
    assert all(a.action == "set" and a.value == "disabled" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_usbsink_standby_actions_on_off():
    on = ca.usbsink_standby_actions(armed=True)
    assert [(a.action, a.key, a.value) for a in on] == [
        ("set", ca.USBSINK_STANDBY_ENV_VAR, "1")
    ]
    off = ca.usbsink_standby_actions(armed=False)
    # F5: explicit `0` (NOT unset).
    assert [(a.action, a.key, a.value) for a in off] == [
        ("set", ca.USBSINK_STANDBY_ENV_VAR, "0")
    ]


def test_usbsink_env_path_agrees_between_coupling_writers():
    # Drift guard: the standby key lives in the same usbsink.env both the combo
    # auto-detector and the coupling reconciler own. Pin the two literals so
    # they never diverge.
    assert ca.USBSINK_ENV_PATH == cr.USBSINK_ENV_PATH
    assert ca.USBSINK_ENV_PATH == "/var/lib/jasper/usbsink.env"


# --------------------------------------------------------------------------
# usb_gadget_stack_present — the dtoverlay probe (reused /sources/ detection)
# --------------------------------------------------------------------------


def test_gadget_present_true_when_dtoverlay_line(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\ndtoverlay=dwc2,dr_mode=peripheral\n")
    assert ca.usb_gadget_stack_present(str(cfg)) is True


def test_gadget_present_true_with_leading_whitespace(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("  dtoverlay=dwc2,dr_mode=peripheral # gadget\n")
    assert ca.usb_gadget_stack_present(str(cfg)) is True


def test_gadget_present_false_when_absent(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n")
    assert ca.usb_gadget_stack_present(str(cfg)) is False


def test_gadget_present_false_when_config_missing(tmp_path):
    assert ca.usb_gadget_stack_present(str(tmp_path / "nope.txt")) is False


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
    usbsink=None,
    camilla_ok=True,
    usbsink_ok=True,
    leader=False,
):
    """Run reconcile_auto with recorded daemon ops.

    ``usb_intent`` defaults to ``gadget`` so a test that says "gadget on" gets the
    combo armed unless it opts out — matching the common jts.local case (gadget
    present AND USB audio on). ``usbsink`` is the usbsink.env tmp path (defaults to a
    sibling of ``fanin``)."""
    if usb_intent is None:
        usb_intent = gadget
    if usbsink is None:
        usbsink = fanin.parent / "usbsink.env"

    def rf():
        restarts.append("fanin")
        return (True, "")

    def ro():
        restarts.append("outputd")
        return (True, "")

    def ru():
        restarts.append("usbsink")
        return (usbsink_ok, "" if usbsink_ok else "usbsink restart failed")

    def rc(coupling):
        restarts.append(f"camilla:{coupling}")
        return (camilla_ok, "reconciled" if camilla_ok else "bad")

    return cr.reconcile_auto(
        reason="t",
        env_path=fanin,
        outputd_env_path=outputd,
        usbsink_env_path=usbsink,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        restart_fanin=rf,
        restart_outputd=ro,
        restart_usbsink=ru,
        reconcile_camilla=rc,
        active_leader_check=lambda: leader,
    )


def test_auto_operator_marker_is_total_no_op(tmp_path, monkeypatch):
    """Marker semantics: an operator-frozen box gets ZERO env changes and NO
    daemon ops — the coupling + combo the operator set stay exactly as they are.
    And the result reports the box's ACTUAL persisted coupling (Nit8)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
    )
    _stub_ring_gates(monkeypatch, eligible=True)  # would resolve ring if owned
    # reconcile_coupling must NOT be called on the operator path.
    called = {"n": 0}
    monkeypatch.setattr(
        cr, "reconcile_coupling", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=restarts)
    assert r.owned is False
    assert r.ok is True
    # Nit8: report the frozen box's real coupling, not a hardcoded loopback.
    assert r.coupling == COUPLING_SHM_RING
    assert called["n"] == 0
    assert restarts == []
    # Env untouched: marker + coupling survive; NO combo keys were written to
    # fanin.env OR usbsink.env.
    text = fanin.read_text()
    assert "JASPER_FANIN_COUPLING_CHOICE=operator" in text
    assert ca.USB_DIRECT_ENV_VAR not in text
    assert not usbsink.exists() or ca.USBSINK_STANDBY_ENV_VAR not in usbsink.read_text()


def test_auto_eligible_gadget_box_with_intent_arms_ring_and_both_combo_halves(
    tmp_path, monkeypatch
):
    """jts.local shape: solo, ring-eligible, gadget present, USB audio ON -> shm_ring
    + BOTH combo halves (fanin keys enabled AND usbsink standby=1), usbsink restarted
    into standby BEFORE fan-in opens the gadget (arm ordering)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_SHM_RING
    assert r.combo_armed is True
    assert r.usb_combo_changed is True
    assert r.usbsink_standby_changed is True
    assert r.restarted_usbsink is True
    assert r.ok is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "enabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "enabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "enabled"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_SHM_RING
    # The standby half landed in usbsink.env, not fanin.env.
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "1"
    assert ca.USBSINK_STANDBY_ENV_VAR not in text
    # Auto NEVER stamps the operator marker (stays auto-owned).
    assert read_value(text, ca.COUPLING_CHOICE_ENV_VAR) is None
    # ARM ordering: usbsink restarted into standby BEFORE fan-in opens the gadget.
    assert "usbsink" in restarts and "fanin" in restarts
    assert restarts.index("usbsink") < restarts.index("fanin")
    assert r.restarted_fanin_for_combo is False


def test_auto_gadget_present_but_usb_audio_off_does_not_arm_combo(tmp_path, monkeypatch):
    """B2: a box with the gadget dtoverlay (fleet-wide) but USB audio turned OFF must
    NOT arm the combo — it writes explicit-off values, not enabled."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=True, usb_intent=False, usbsink=usbsink, restarts=restarts
    )
    assert r.combo_armed is False
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "0"
    # The ring can still resolve (eligible), but the combo is off.
    assert r.coupling == COUPLING_SHM_RING


def test_auto_jts3_roleful_is_loopback_combo_off(tmp_path, monkeypatch):
    """jts3 shape: roleful topology (a ring gate fails) + no gadget -> loopback,
    combo written to explicit OFF (F5), and the arm never runs (no ring transition)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.combo_armed is False
    # No gadget: combo keys written to EXPLICIT off (F5 — defeats jasper.env
    # precedence), not left absent.
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "0"
    assert read_value(text, COUPLING_ENV_VAR) in (None, COUPLING_LOOPBACK)


def test_auto_jts5_composite_is_loopback(tmp_path, monkeypatch):
    """jts5 shape: composite dual-DAC (a ring gate fails) + no gadget -> loopback."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=[])
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.combo_armed is False


def test_auto_jts4_streambox_no_fanin_stack_exits_clean(tmp_path, monkeypatch):
    """jts4 shape: no fan-in stack. The auto pass must exit cleanly with no crash.
    Modeled as: not eligible, no gadget. (In production the unit is parked on a
    streambox, so this pass does not even run there — F7.) Combo resolves to the
    explicit OFF values (F5)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    # Already loopback (a streambox never armed anything).
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=[])
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    assert r.ok is True
    assert r.combo_armed is False
    # Combo written to explicit OFF (F5).
    assert read_value(fanin.read_text(), ca.USB_DIRECT_ENV_VAR) == "disabled"


def test_auto_gadget_lost_clears_stale_combo_keys(tmp_path, monkeypatch):
    """Single-writer discipline: a box that previously had the combo armed but LOST
    the gadget must have BOTH combo halves driven to their explicit OFF values —
    fanin keys to `disabled`, usbsink standby to `0` (F5: explicit off, not unset, so
    a stale jasper.env `enabled` can't win). usbsink is restarted (disarm ordering:
    after fan-in)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text(
        "JASPER_FANIN_USB_DIRECT=enabled\n"
        "JASPER_FANIN_HOST_CLOCK=enabled\n"
        "JASPER_FANIN_RESAMPLER_CUSHION_DECAY=enabled\n"
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
    )
    outputd.write_text("")
    usbsink.write_text("JASPER_USBSINK_AUDIO_STANDBY=1\n")
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.usbsink_standby_changed is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "disabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "disabled"
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "0"
    # DISARM ordering: usbsink restarted AFTER fan-in released the gadget.
    assert r.restarted_usbsink is True
    assert "usbsink" in restarts and "fanin" in restarts
    assert restarts.index("fanin") < restarts.index("usbsink")


def test_auto_is_idempotent_second_pass_writes_nothing(tmp_path, monkeypatch):
    """Idempotence: two identical auto passes converge with ONE write. The second
    pass reports both change flags False and leaves fanin.env + usbsink.env
    byte-identical (no daemon restarts on the second pass)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)

    r1 = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=[])
    assert r1.usb_combo_changed is True
    assert r1.usbsink_standby_changed is True
    after_first_fanin = fanin.read_text()
    after_first_usbsink = usbsink.read_text()

    restarts2: list[str] = []
    r2 = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=restarts2)
    assert r2.usb_combo_changed is False
    assert r2.usbsink_standby_changed is False
    assert r2.restarted_usbsink is False
    assert fanin.read_text() == after_first_fanin
    assert usbsink.read_text() == after_first_usbsink
    # The second pass writes nothing and bounces NO data-plane daemon. It DOES
    # re-run the lightweight camilla confirm (the shm_ring CONFIRM-path self-heal),
    # which is by design — that never glitches audio. Assert only that no fan-in /
    # outputd / usbsink restart fired.
    assert "fanin" not in restarts2
    assert "outputd" not in restarts2
    assert "usbsink" not in restarts2


def test_auto_combo_only_change_forces_fanin_restart(tmp_path, monkeypatch):
    """A combo-only change on an already-at-desired-coupling box (loopback, no
    ring transition -> confirm path, no bounce) still needs fan-in restarted so the
    new combo takes effect. The auto pass issues that one restart; the arming standby
    change also restarts usbsink first (arm ordering)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    # Already loopback; gadget present + USB audio on but combo NOT yet written.
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    outputd.write_text("")
    # Ineligible for ring (so coupling stays loopback = no arm bounce), gadget+intent.
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.combo_armed is True
    assert r.coupling == COUPLING_LOOPBACK
    # The confirm path did not restart fan-in, so the combo forced one.
    assert r.restarted_fanin_for_combo is True
    assert restarts.count("fanin") == 1
    # Arming standby restarted usbsink BEFORE the combo fan-in restart.
    assert r.restarted_usbsink is True
    assert restarts.index("usbsink") < restarts.index("fanin")


def test_auto_usbsink_restart_failure_is_not_ok(tmp_path, monkeypatch):
    """A combo transition that cannot restart usbsink leaves the two gadget owners
    in a split state — surface it as ok=False (the unit exits non-zero) rather than a
    silently-broken USB path."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    r = _auto(
        fanin, outputd, gadget=True, usbsink=usbsink, usbsink_ok=False, restarts=[]
    )
    assert r.usbsink_standby_changed is True
    assert r.restarted_usbsink is False
    assert r.ok is False


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
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    # Everything eligible EXCEPT we let the real route gate see an active leader.
    _stub_ring_gates_except_route(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(
        fanin, outputd, gadget=False, usbsink=usbsink, restarts=restarts, leader=True
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
    usbsink = tmp_path / "usbsink.env"
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
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=restarts)
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
    usbsink = tmp_path / "usbsink.env"
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
    r = _auto(fanin, outputd, gadget=False, usbsink=usbsink, restarts=restarts)

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
    usbsink = tmp_path / "usbsink.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)

    r = _auto(fanin, outputd, gadget=True, usbsink=usbsink, restarts=[])

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
    # The usbsink standby half of the combo lands (documented as armed together).
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "1"


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


# ---- runtime-fallback flap guard (defect 2026-07-10) -----------------------


def _ring_gates_ok():
    return (("ring_assets", lambda: (True, "a")), ("ring_topology", lambda: (True, "t")))


def test_resolve_fallback_active_forces_combo_off_when_eligible():
    """A combo-eligible box (gadget + USB intent) whose live capture broke carries
    the fallback marker -> combo forced OFF (explicit disabled/0), fallback_active
    reported, but the ring coupling is UNAFFECTED."""
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=True,
        ring_gates=_ring_gates_ok(),
        fallback_active=True,
    )
    assert d.combo_armed is False
    assert d.fallback_active is True
    assert d.coupling == COUPLING_SHM_RING  # ring decision untouched by the fallback
    # Both combo halves written to their explicit-off values.
    for a in d.usb_combo_actions:
        assert a.value == ca.USB_COMBO_DISABLED_VALUE
    assert d.usbsink_standby_actions[0].value == ca.USBSINK_STANDBY_OFF_VALUE


def test_resolve_fallback_on_ineligible_box_is_not_reported():
    """fallback_active is only meaningful when the box WOULD otherwise arm; on an
    ineligible box (USB intent off) it is not reported as a fallback."""
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        usb_intent_enabled=False,  # not eligible
        ring_gates=_ring_gates_ok(),
        fallback_active=True,
    )
    assert d.combo_armed is False
    assert d.fallback_active is False


def test_reconcile_auto_fallback_marker_disarms_a_combo_box(tmp_path, monkeypatch):
    """End-to-end: an eligible gadget box (would arm) with fallback_active=True
    resolves the combo OFF and writes explicit-off env, landing on the aloop bridge
    state."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    usbsink = tmp_path / "usbsink.env"
    # Pre-arm the combo so the disarm is an actual change.
    fanin.write_text(
        f"{ca.USB_DIRECT_ENV_VAR}=enabled\n{ca.HOST_CLOCK_ENV_VAR}=enabled\n"
        f"{ca.CUSHION_DECAY_ENV_VAR}=enabled\n"
    )
    outputd.write_text("")
    usbsink.write_text(f"{ca.USBSINK_STANDBY_ENV_VAR}=1\n")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = cr.reconcile_auto(
        reason="health",
        env_path=fanin,
        outputd_env_path=outputd,
        usbsink_env_path=usbsink,
        gadget_present=True,
        usb_intent_enabled=True,
        fallback_active=True,  # the marker forces combo off
        restart_fanin=lambda: (restarts.append("fanin"), (True, ""))[1],
        restart_outputd=lambda: (restarts.append("outputd"), (True, ""))[1],
        restart_usbsink=lambda: (restarts.append("usbsink"), (True, ""))[1],
        reconcile_camilla=lambda coupling: (True, "reconciled"),
        active_leader_check=lambda: False,
    )
    assert r.combo_armed is False
    assert r.fallback_active is True
    assert r.usb_combo_changed is True
    # Combo env forced to explicit-off on both halves.
    assert read_value(fanin.read_text(), ca.USB_DIRECT_ENV_VAR) == "disabled"
    assert read_value(usbsink.read_text(), ca.USBSINK_STANDBY_ENV_VAR) == "0"
    # usbsink restarted LAST on disarm (after fan-in released the gadget).
    assert "usbsink" in restarts and "fanin" in restarts
    assert restarts.index("fanin") < restarts.index("usbsink")
