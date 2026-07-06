# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P3/P4 default-flip: default-resolution of the fan-in coupling + USB combo.

Pins the campaign brief's contracts:
  - marker semantics (operator choice survives the auto pass);
  - eligibility no-ops across the four validated box shapes
    (jts.local eligible; jts3 roleful; jts5 composite; jts4 fanin-less);
  - USB combo written on a gadget box, cleared off a gadget box;
  - idempotence (auto pass twice = one write).
"""

from __future__ import annotations

import pytest

from jasper.env_file import read_value
from jasper.fanin import coupling_auto as ca
from jasper.fanin import coupling_reconcile as cr
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
)


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
        ring_gates=(("assets", _pass_gate()),),
    )
    assert d.owned is False
    # No env actions at all — the operator's revert must not be touched.
    assert d.usb_combo_actions == ()


def test_decision_eligible_gadget_box_resolves_ring_and_combo_on():
    d = ca.resolve_auto_decision(
        marker_raw=None,
        gadget_present=True,
        ring_gates=(("assets", _pass_gate()), ("topology", _pass_gate())),
    )
    assert d.owned is True
    assert d.coupling == COUPLING_SHM_RING
    assert [(a.action, a.key, a.value) for a in d.usb_combo_actions] == [
        ("set", ca.USB_DIRECT_ENV_VAR, "enabled"),
        ("set", ca.HOST_CLOCK_ENV_VAR, "enabled"),
        ("set", ca.CUSHION_DECAY_ENV_VAR, "enabled"),
    ]


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
        ring_gates=(("assets", _pass_gate()), ("topology", boom)),
    )
    assert d.coupling == COUPLING_LOOPBACK
    assert "topology" in d.reason


def test_usb_combo_actions_set_when_gadget_present():
    acts = ca.usb_combo_actions(True)
    assert all(a.action == "set" and a.value == "enabled" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


def test_usb_combo_actions_unset_when_gadget_absent():
    acts = ca.usb_combo_actions(False)
    assert all(a.action == "unset" for a in acts)
    assert {a.key for a in acts} == set(ca.USB_COMBO_ENV_VARS)


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
    file is touched."""
    assets = ("ring_assets", lambda: (eligible, "assets"))
    topo = ("ring_topology", lambda: (eligible, "topology"))
    monkeypatch.setattr(ca, "default_ring_gates", lambda: (assets, topo))
    monkeypatch.setattr(cr, "ring_geometry_ready", lambda text: (eligible, "geom"))
    monkeypatch.setattr(cr, "ring_slot_geometry_ready", lambda text: (eligible, "slots"))
    # Arm-spine preflights (only reached when eligible + coupling flips to ring).
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(cr, "ring_assets_ready", lambda: (eligible, "assets"))
    monkeypatch.setattr(cr, "ring_topology_ready", lambda: (eligible, "topology"))
    monkeypatch.setattr(cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None)


def _auto(fanin, outputd, *, gadget, restarts, camilla_ok=True, leader=False):
    """Run reconcile_auto with recorded daemon ops."""
    def rf():
        restarts.append("fanin")
        return (True, "")

    def ro():
        restarts.append("outputd")
        return (True, "")

    def rc(coupling):
        restarts.append(f"camilla:{coupling}")
        return (camilla_ok, "reconciled" if camilla_ok else "bad")

    return cr.reconcile_auto(
        reason="t",
        env_path=fanin,
        outputd_env_path=outputd,
        gadget_present=gadget,
        restart_fanin=rf,
        restart_outputd=ro,
        reconcile_camilla=rc,
        active_leader_check=lambda: leader,
    )


def test_auto_operator_marker_is_total_no_op(tmp_path, monkeypatch):
    """Marker semantics: an operator-frozen box gets ZERO env changes and NO
    daemon ops — the coupling + combo the operator set stay exactly as they are."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
    )
    _stub_ring_gates(monkeypatch, eligible=True)  # would resolve ring if owned
    # reconcile_coupling must NOT be called on the operator path.
    called = {"n": 0}
    monkeypatch.setattr(
        cr, "reconcile_coupling", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.owned is False
    assert r.ok is True
    assert called["n"] == 0
    assert restarts == []
    # Env untouched: marker + loopback survive; NO combo keys were written.
    text = fanin.read_text()
    assert "JASPER_FANIN_COUPLING_CHOICE=operator" in text
    assert ca.USB_DIRECT_ENV_VAR not in text


def test_auto_eligible_gadget_box_arms_ring_and_writes_combo(tmp_path, monkeypatch):
    """jts.local shape: solo, ring-eligible, gadget present -> shm_ring + combo on."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_SHM_RING
    assert r.usb_combo_changed is True
    assert r.ok is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) == "enabled"
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) == "enabled"
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) == "enabled"
    assert read_value(text, COUPLING_ENV_VAR) == COUPLING_SHM_RING
    # Auto NEVER stamps the operator marker (stays auto-owned).
    assert read_value(text, ca.COUPLING_CHOICE_ENV_VAR) is None
    # The arm restarted fan-in; no separate combo restart needed.
    assert r.restarted_fanin_for_combo is False
    assert "fanin" in restarts


def test_auto_jts3_roleful_is_loopback_no_combo(tmp_path, monkeypatch):
    """jts3 shape: roleful topology (a ring gate fails) + no gadget -> loopback,
    combo cleared, and the arm never runs (no ring transition)."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=False, restarts=restarts)
    assert r.owned is True
    assert r.coupling == COUPLING_LOOPBACK
    # No gadget: combo keys stay absent (unset actions on an already-empty file
    # are a no-op).
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) is None
    assert r.usb_combo_changed is False
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
    assert r.usb_combo_changed is False


def test_auto_jts4_streambox_no_fanin_stack_exits_clean(tmp_path, monkeypatch):
    """jts4 shape: no fan-in stack. The auto pass must exit cleanly with no crash
    and no combo writes. Modeled as: not eligible, no gadget, and the daemon ops
    are absent (the reconcile no-ops on an already-loopback box)."""
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
    assert r.usb_combo_changed is False
    # No combo keys written on a non-gadget box.
    assert ca.USB_DIRECT_ENV_VAR not in fanin.read_text()


def test_auto_gadget_lost_clears_stale_combo_keys(tmp_path, monkeypatch):
    """Single-writer discipline: a box that previously had the combo armed but
    LOST the gadget must have the combo keys CLEARED (mirrors jasper-aec-reconcile
    clearing stale mic-device vars)."""
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
    r = _auto(fanin, outputd, gadget=False, restarts=[])
    assert r.usb_combo_changed is True
    text = fanin.read_text()
    assert read_value(text, ca.USB_DIRECT_ENV_VAR) is None
    assert read_value(text, ca.HOST_CLOCK_ENV_VAR) is None
    assert read_value(text, ca.CUSHION_DECAY_ENV_VAR) is None


def test_auto_is_idempotent_second_pass_writes_nothing(tmp_path, monkeypatch):
    """Idempotence: two identical auto passes converge with ONE write. The second
    pass reports usb_combo_changed=False and leaves the env byte-identical."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    fanin.write_text("")
    outputd.write_text("")
    _stub_ring_gates(monkeypatch, eligible=True)

    r1 = _auto(fanin, outputd, gadget=True, restarts=[])
    assert r1.usb_combo_changed is True
    after_first = fanin.read_text()

    r2 = _auto(fanin, outputd, gadget=True, restarts=[])
    assert r2.usb_combo_changed is False
    assert fanin.read_text() == after_first


def test_auto_combo_only_change_forces_fanin_restart(tmp_path, monkeypatch):
    """A combo-only change on an already-at-desired-coupling box (loopback, no
    ring transition -> confirm path, no bounce) still needs fan-in restarted so the
    new combo takes effect. The auto pass issues that one restart."""
    fanin = tmp_path / "fanin.env"
    outputd = tmp_path / "outputd.env"
    # Already loopback; gadget present but combo NOT yet written.
    fanin.write_text("JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    outputd.write_text("")
    # Ineligible for ring (so coupling stays loopback = no arm bounce), gadget on.
    _stub_ring_gates(monkeypatch, eligible=False)
    restarts: list[str] = []
    r = _auto(fanin, outputd, gadget=True, restarts=restarts)
    assert r.usb_combo_changed is True
    assert r.coupling == COUPLING_LOOPBACK
    # The confirm path did not restart fan-in, so the combo forced one.
    assert r.restarted_fanin_for_combo is True
    assert restarts.count("fanin") == 1
