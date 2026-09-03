# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ACTIVE-endpoint convergence step (#2285 P7).

What these pin is the STEP, not admission. Whether an unattended pass may touch
a roleful box at all is ``ring_roleful_unattended_ready``'s question (P6, pinned
in ``tests/test_ring_active_endpoint.py``); this step's questions are only the
ones no gate can answer — is the graph already there, did a human pin this box,
and is the applied record still what the speaker is playing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# EAGER, AND NOT UNUSED — deleting these re-opens a cross-file failure.
# Five modules bind ``load_output_topology_strict`` at MODULE scope (`from
# jasper.output_topology import ...`), so whichever of them is imported FIRST
# while this file's fixture has that function patched freezes the patch into its
# globals permanently — monkeypatch undoes the source module, never the copy.
# The fixture's own convergence call is what pulls them in, so importing them
# here, before any patching, is what keeps the patch local to this file.
# Symptom when this regresses: an unrelated test in
# tests/test_audio_hardware_reconcile.py fails with
# "'object' object has no attribute 'speaker_groups'".
import jasper.active_speaker.runtime_contract  # noqa: F401
import jasper.active_speaker.setup_status  # noqa: F401
import jasper.cli.active_speaker  # noqa: F401
import jasper.cli.output_topology_reset  # noqa: F401
import jasper.correction.runtime_safety  # noqa: F401
from jasper.fanin import converge

#: The REAL re-emit, captured before any fixture replaces the attribute.
#: ``_Box`` stubs ``converge._reemit_graph_at_ring``, so a test that wants the
#: genuine article back cannot read it off the module — by then it is the stub.
_REAL_REEMIT = converge._reemit_graph_at_ring


class _Box:
    """A roleful box, admitted by every gate, graph not yet at the ring."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.reemits: list[str] = []
        self.kicks: list[str] = []
        self.reemit_ok = True
        self.converged = False
        self.applied: dict | None = None
        self.displacement = ""
        self.roleful = True
        self.gates: tuple = ()

        cr = "jasper.fanin.coupling_reconcile"
        monkeypatch.setattr(f"{cr}.default_ring_gates", lambda: self.gates)
        monkeypatch.setattr(
            "jasper.output_topology.load_output_topology_strict",
            lambda *a, **k: object(),
        )
        monkeypatch.setattr(
            "jasper.active_speaker.runtime_contract.active_ring_channels_for_topology",
            lambda _t: 4 if self.roleful else None,
        )
        monkeypatch.setattr(
            "jasper.active_speaker.baseline_profile."
            "load_applied_baseline_profile_state",
            lambda *a, **k: self.applied,
        )
        monkeypatch.setattr(
            "jasper.active_speaker.baseline_profile.applied_profile_displacement",
            lambda *a, **k: self.displacement,
        )
        monkeypatch.setattr(f"{cr}.read_loaded_camilla_graph", lambda *a, **k: _graph())
        monkeypatch.setattr(
            f"{cr}.graph_at_active_ring_endpoint",
            lambda _g: (self.converged, "endpoint detail"),
        )
        monkeypatch.setattr(
            f"{cr}._start_audio_hardware_reconcile",
            lambda **k: (self.kicks.append("kick"), (True, ""))[1],
        )
        monkeypatch.setattr(
            converge,
            "_reemit_graph_at_ring",
            lambda: (
                self.reemits.append("reemit"),
                (self.reemit_ok, "" if self.reemit_ok else "refused"),
            )[1],
        )

    def run(self) -> str:
        return converge.converge_active_endpoint(reason="test")

    def touched_anything(self) -> bool:
        return bool(self.reemits or self.kicks)


def _graph():
    class _G:
        note = ""
        path = "/var/lib/camilladsp/configs/before.yml"
        devices: dict = {}

    return _G()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    return _Box(tmp_path, monkeypatch)


def test_a_plugged_in_roleful_box_converges_itself(box):
    """THE HEADLINE: no operator command anywhere in this path."""
    assert box.run() == "graph_reemitted"
    assert box.reemits == ["reemit"] and box.kicks == ["kick"]


def test_a_flat_box_is_a_no_op(box):
    box.roleful = False

    assert box.run() == "noop_not_roleful"
    assert not box.touched_anything()


def test_an_unreadable_topology_refuses_rather_than_moving(box, monkeypatch):
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )

    assert box.run() == "preflight_refused"
    assert not box.touched_anything()


def test_it_is_a_no_op_once_the_graph_is_at_the_ring(box):
    """IDEMPOTENCE. This runs at every boot, deploy and DAC hotplug, so a
    converged box doing any work here is a cost paid forever."""
    box.converged = True

    assert box.run() == "already_converged"
    assert not box.touched_anything()


def test_a_commissioned_box_can_report_converged(box, monkeypatch):
    """THE ENDPOINT PREDICATE, NOT THE ANCHOR ONE.

    ``ring_endpoint_anchor_converged`` also demands anchor identity and
    all-muted, both false forever on a box riding an applied baseline. Asking it
    here would re-emit a commissioned box's graph at every boot, deploy and
    hotplug — the design's own step 0e named that predicate, and it is wrong for
    one of the two arms P6 admits.
    """
    from jasper.fanin import coupling_reconcile as cr

    box.applied = {"config": {"path": "/x.yml"}}
    box.converged = True
    asked: list[str] = []
    monkeypatch.setattr(
        cr,
        "ring_endpoint_anchor_converged",
        lambda **k: asked.append("asked") or (False, "not the anchor"),
    )

    assert box.run() == "already_converged"
    assert asked == []


def test_a_refused_gate_stops_the_move(box):
    """PROVE, THEN MOVE — a refusal leaves zero evidence behind."""
    box.gates = (("ring_assets", lambda: (False, "the ioplug is not installed")),)

    assert box.run() == "preflight_refused"
    assert not box.touched_anything()


def test_the_one_skipped_gate_is_the_fixed_point_itself(box):
    """``ring_topology`` is skipped because its roleful arm ends in
    ``active_ring_endpoint_proof``, which reads the marker derived from the graph
    this has not moved yet. Requiring it here IS the fixed point. Skipping any
    other gate would move a graph the box was never admitted to run."""
    asked: list[str] = []
    box.gates = (
        ("ring_topology", lambda: asked.append("ring_topology") or (False, "no proof")),
        ("ring_assets", lambda: asked.append("ring_assets") or (True, "")),
    )

    assert box.run() == "graph_reemitted"
    assert asked == ["ring_assets"]


def test_the_width_gate_does_not_refuse_the_graph_it_has_not_moved_yet():
    """THE OTHER GATE THAT READS THE LOADED GRAPH, and why it needs no skip.

    ``ring_edge_width`` is in the real ``default_ring_gates()`` and this step
    runs it BEFORE the move, so if it refused an unmoved graph nothing would
    ever converge — the headline feature would be a silent no-op on every box.
    It does not: an end it cannot inspect costs the message its claim, never the
    arm its verdict. Asserted against the REAL function rather than argued from
    its docstring, because "the gate probably passes" is how a feature ships
    dead.
    """
    from jasper.fanin.ring_health import (
        LoadedCamillaGraph,
        ring_edge_width_ready,
    )

    unmoved = LoadedCamillaGraph(
        path="/x.yml",
        devices={
            "capture_device": "jasper_capture",
            "playback_device": "outputd_content",
        },
    )

    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="", graph=unmoved)

    assert ok is True, detail
    assert "names no ring PCM" in detail


def test_a_raising_gate_is_a_refusal(box):
    box.gates = (("ring_assets", lambda: (_ for _ in ()).throw(ValueError("bad"))),)

    assert box.run() == "preflight_refused"
    assert not box.touched_anything()


def test_an_authoritative_applied_record_converges_freely(box):
    """#2558's closing contract, polarity one: "" converges."""
    box.applied = {"config": {"path": "/x.yml"}}
    box.displacement = ""

    assert box.run() == "graph_reemitted"


@pytest.mark.parametrize(
    "verdict",
    [
        "applied_profile_displaced",
        "applied_profile_path_unknown",
        "running_config_path_unknown",
    ],
)
def test_any_non_empty_displacement_verdict_refuses(box, verdict):
    """Polarity two, over ALL THREE codes including the two "we could not
    check" ones: a provenance that cannot be established is not one to re-emit
    over, because the hazard is publishing a graph the speaker is not playing."""
    box.applied = {"config": {"path": "/x.yml"}}
    box.displacement = verdict

    assert box.run() == "applied_record_diverged"
    assert not box.touched_anything()


def test_an_anchor_riding_box_is_never_asked_the_divergence_question(box, monkeypatch):
    """THE GUARD IS THE RECORD'S EXISTENCE, NOT ITS VERDICT.

    ``applied_profile_displacement`` answers ``applied_profile_path_unknown``
    for a missing record, so asking it unconditionally would refuse every fresh
    install and every mid-commission box — the class that converges first.
    """
    asked: list[str] = []
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda *a, **k: asked.append("asked") or "applied_profile_path_unknown",
    )
    box.applied = None

    assert box.run() == "graph_reemitted"
    assert asked == []


def test_a_refused_reemit_does_not_kick(box):
    box.reemit_ok = False

    assert box.run() == "reemit_refused"
    assert box.kicks == []


def test_no_automated_path_can_pass_force(monkeypatch):
    """The safety here is an ABSENCE.

    ``_reemit_staged_startup_anchor`` refuses while a per-driver commissioning
    load is active, because moving the anchor mid-load re-points the operator's
    own stop control. This caller's safety IS that refusal, so ``--force`` is
    not threaded through.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "jasper.cli.active_speaker.main", lambda argv: seen.append(list(argv)) or 0
    )

    assert _REAL_REEMIT() == (True, "")
    (argv,) = seen
    assert argv[:3] == ["baseline-reemit", "--endpoint", "ring"]
    assert not any("force" in arg for arg in argv)


@pytest.mark.parametrize(
    "auto,no_apply", [(False, False), (False, True), (True, True)]
)
def test_the_explicit_operator_arm_is_untouched(monkeypatch, auto, no_apply):
    """SCOPE PIN. Convergence rides the UNATTENDED pass only: an operator
    already typing the CLI sees no change in behaviour and no change in ladder.
    """
    import types

    from jasper.fanin import converge as cv
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    converged: list[str] = []
    monkeypatch.setattr(
        cv,
        "converge_active_endpoint",
        lambda **k: converged.append(k.get("reason", "")) or "converged",
    )
    monkeypatch.setattr(
        cr,
        "reconcile_auto",
        lambda **k: types.SimpleNamespace(
            gadget_present=False,
            usb_intent_enabled=False,
            combo_armed=False,
            usb_combo_changed=False,
            restarted_fanin_for_combo=False,
            ok=True,
            reason="",
            detail="",
        ),
    )
    monkeypatch.setattr(
        cr,
        "reconcile_coupling",
        lambda **k: types.SimpleNamespace(
            ok=True,
            changed=False,
            restarted_outputd=False,
            restarted_fanin=False,
            reconciled_camilla=False,
            detail="",
        ),
    )
    args = types.SimpleNamespace(
        auto=auto, no_apply=no_apply, coupling=None, reason="test"
    )

    cr._run_entry_verb(args)

    assert converged == []


def test_a_convergence_that_raises_costs_the_box_its_convergence_not_its_reconcile(
    monkeypatch, caplog
):
    """THE STEP RUNS AHEAD OF EVERYTHING THE PASS HAS ALWAYS DONE.

    A corrupt ``fanin.env`` raises ``UnicodeDecodeError`` out of the very first
    read — ``_read_snapshot`` catches ``OSError`` only — and this step is now the
    first thing the unattended pass does. Unguarded, that would turn "this box
    did not converge" into "this box did not reconcile at all".
    """
    import logging
    import types

    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    converged: list[str] = []

    def _raise_after_recording(**kwargs):
        converged.append(kwargs["reason"])
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    monkeypatch.setattr(
        "jasper.fanin.converge.converge_active_endpoint", _raise_after_recording
    )
    reached: list[str] = []
    monkeypatch.setattr(
        cr,
        "reconcile_auto",
        lambda **k: reached.append("pass ran")
        or types.SimpleNamespace(
            gadget_present=False,
            usb_intent_enabled=False,
            combo_armed=False,
            usb_combo_changed=False,
            restarted_fanin_for_combo=False,
            ok=True,
            reason="",
            detail="",
        ),
    )
    args = types.SimpleNamespace(
        auto=True, no_apply=False, coupling=None, reason="test"
    )

    with caplog.at_level(logging.ERROR, logger="jasper.fanin.coupling_reconcile"):
        rc = cr._run_entry_verb(args)

    assert rc == 0
    assert converged == ["test"], "the pass's own reason must reach the step"
    assert reached == ["pass ran"], "the reconcile must still run"
    assert any("converge_raised" in r.getMessage() for r in caplog.records)


def test_a_non_derived_raise_inside_the_cli_costs_only_the_convergence(
    box, monkeypatch
):
    """THE AVAILABILITY WRAP, and why it is broad where its neighbours are narrow.

    ``cli.main`` converts exactly THREE classes into ``parser.exit``:
    ``ActiveSpeakerConfigError``, ``OutputTopologyError``, ``OSError``. Every
    other class the CLI's whole tree can raise arrives at this caller live, and
    a narrowed catch here aborts the entire unattended pass — the box losing its
    RECONCILE, not merely its convergence, which is the contract this step
    exists to keep.

    THREE CLAIMS, THREE EVIDENCE CLASSES, kept apart rather than blurred into
    "reachable shapes". The abort is MEASURED. The type-confused applied record
    is DEMONSTRATED — it is precisely what this test injects. The mid-deploy
    ``ImportError`` is ARGUED only, reasoned from this pass running WHILE
    install.sh rsyncs Python under it with both modules lazy-importing; nobody
    has reproduced it.

    INJECTED AT ``_cmd_baseline_reemit``, NOT at ``cli.main``, and the
    distinction is the whole test: patching ``cli.main`` would raise OUTSIDE
    the frame whose converter is being characterised, so the pin would pass
    without ever exercising the escape path.

    Asserts on the PASS, not only on the step.
    """
    import types

    from jasper.fanin import converge as cv
    from jasper.fanin import coupling_reconcile as cr

    def _boom(*a, **k):
        raise TypeError("a type-confused applied record")

    monkeypatch.setattr("jasper.cli.active_speaker._cmd_baseline_reemit", _boom)

    # 1. The step itself: refuses, typed, and moves nothing.
    ok, detail = _REAL_REEMIT()
    assert ok is False
    assert "baseline-reemit raised: TypeError" in detail
    assert "type-confused" in detail

    # 2. The step in context: the REAL re-emit runs under the real convergence,
    #    which refuses rather than raising.
    monkeypatch.setattr(cv, "_reemit_graph_at_ring", _REAL_REEMIT)
    assert box.run() == "reemit_refused"
    assert box.kicks == [], "a refused re-emit must not kick the reconciler"

    # 3. The whole pass: its non-convergence duties still run.
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    reached: list[str] = []
    monkeypatch.setattr(
        cr,
        "reconcile_auto",
        lambda **k: reached.append("pass ran")
        or types.SimpleNamespace(
            gadget_present=False,
            usb_intent_enabled=False, combo_armed=False, usb_combo_changed=False,
            restarted_fanin_for_combo=False, ok=True, reason="", detail="",
        ),
    )
    args = types.SimpleNamespace(
        auto=True, no_apply=False, coupling=None, reason="test"
    )

    assert cr._run_entry_verb(args) == 0
    assert reached == ["pass ran"], "the reconcile must still run"
