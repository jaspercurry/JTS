# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ARM direction accepts a converged ring-endpoint startup anchor.

THE LIVE FAILURE THESE PIN. On jts.local, 2026-08-15, the first composite ring
arm reached step 3 and failed:

    ok=False changed=False outputd=True fanin=True camilla=False recovered=True
    detail=eq_on_active_not_wired result=arm_ring_camilla_failed

The reconciler kept loopback, so nothing broke — but an anchor-riding box (the
fleet-typical mid-commission composite, the one #2514 exists to arm) could never
pass step 3 at all. ``reconcile_current_dsp`` resolves an all-muted staged
startup anchor to a transient active carrier, which refuses to host EQ
(``eq_on_active_not_wired`` -> status ``skipped``), and ``_reconcile_camilla``
accepted ``skipped`` only in the DISARM direction.

The carrier's refusal is correct and unchanged. What changed is the arm's
acceptance criterion: a ``skipped`` carrying exactly that refusal is converged
when :func:`ring_endpoint_anchor_converged` can PROVE, from the artifacts on
disk, that the loaded graph is this box's own published startup anchor and is
already at the ACTIVE ring endpoint at the box's wire. Anything else keeps
failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.fanin.coupling_reconcile import (
    CAMILLA_ANCHOR_CONVERGED_DETAIL,
    CARRIER_TRANSIENT_ACTIVE_REFUSAL,
    ring_endpoint_anchor_converged,
)
from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_WIRE_FORMAT_ENV_VAR,
    RING_WIRE_FORMAT_WIDE,
)

# The ALSA active lane a roleful box plays into BEFORE it is armed — the
# "incoherent endpoint" fixture below. Resolved from the contract rather than
# spelled, so a rename moves this test with it.
from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE

# The canonical saved dual-Apple composite: 4 outputs, left woofer/tweeter on
# 0/1, right on 2/3. Reused rather than re-fabricated so this file cannot drift
# from the shape P8b item 1b actually made ring-armable.
from tests.test_composite_ring_arm_enabling import _composite_active_2way

# The sibling suite's arm-spine setup (env isolation + every ring PREFLIGHT
# forced to pass), so the two arm-level tests below exercise the spine rather
# than re-testing gates that already have dedicated coverage there.
from tests.test_fanin_coupling_reconcile import (
    force_ring_gates_pass,
    isolate_base_jasper_env,
)


# --- fixtures ---------------------------------------------------------------


def _graph_yaml(
    *,
    capture_device: str,
    playback_device: str,
    fmt: str,
    playback_channels: int = 4,
) -> str:
    """A roleful boot graph's ``devices:`` block, in the shape the parser reads.

    Ring A is always the 2-channel stereo program; the composite's ACTIVE ring
    is 4 wide (``active_ring_channels_for_topology`` on the fixture topology),
    so these are the widths a coherent composite anchor declares.
    """
    return (
        "devices:\n"
        "  samplerate: 48000\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    channels: 2\n"
        f"    format: {fmt}\n"
        f'    device: "{capture_device}"\n'
        "  playback:\n"
        "    type: Alsa\n"
        f"    channels: {playback_channels}\n"
        f"    format: {fmt}\n"
        f'    device: "{playback_device}"\n'
        "filters:\n"
    )


def _stage_box(
    tmp_path: Path,
    monkeypatch,
    *,
    graph_yaml: str,
    graph_name: str = "active_speaker_staged_startup.yml",
    publish_anchor: object = True,
    wire_format: str = RING_WIRE_FORMAT_WIDE,
) -> Path:
    """Put a whole box on disk: a loaded graph, a statefile, a staged record.

    Every input :func:`ring_endpoint_anchor_converged` reads is a real file read
    through the real reader — the statefile via ``JASPER_CAMILLA_STATEFILE``,
    the staged record via ``JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH``, and
    the declared wire off an isolated two-file ``jasper.env`` -> ``fanin.env``
    chain. The wire in particular is NOT stubbed at the reader: stubbing it
    skips ``resolve_ring_wire_format``'s validation, which made an
    illegal-token test pass through a format MISMATCH instead of through the
    refusal it claimed to prove. Only the saved output topology is a seam — a
    composite is what makes an ACTIVE ring resolve at all.
    """
    import json

    from jasper.active_speaker.runtime_contract import write_camilla_statefile
    from jasper.fanin import coupling_reconcile as cr

    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    graph = configs / graph_name
    graph.write_text(graph_yaml, encoding="utf-8")

    statefile = tmp_path / "outputd-statefile.yml"
    write_camilla_statefile(statefile, graph)
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    metadata = tmp_path / "active_speaker_staged_config.json"
    anchor = configs / "active_speaker_staged_startup.yml"
    metadata.write_text(
        json.dumps(
            {
                "status": "staged",
                "config": (
                    {"path": str(anchor)} if publish_anchor is True else publish_anchor
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata)
    )

    jasper_env = tmp_path / "jasper.env"
    jasper_env.write_text("", encoding="utf-8")
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{RING_WIRE_FORMAT_ENV_VAR}={wire_format}\n", encoding="utf-8"
    )
    monkeypatch.setattr(cr, "JASPER_ENV_PATH", str(jasper_env))
    monkeypatch.setattr(cr, "FANIN_ENV_PATH", str(fanin_env))

    monkeypatch.setattr(cr, "load_topology_for_wire", _composite_active_2way)
    return graph


def _skipped_carrier_refusal(monkeypatch, *, reason: str) -> list[dict]:
    """Make ``reconcile_current_dsp`` answer a carrier refusal, recording calls."""
    from jasper.sound import runtime

    observed: list[dict] = []

    async def fake_reconcile_current_dsp(**kwargs):
        observed.append(kwargs)
        return {"status": "skipped", "reason": reason}

    monkeypatch.setattr(runtime, "reconcile_current_dsp", fake_reconcile_current_dsp)
    return observed


# --- (f) the cross-module literal this acceptance keys on -------------------


def test_the_carrier_still_raises_the_refusal_code_the_arm_keys_on():
    """CONTRACT PIN. ``coupling_reconcile`` spells the carrier's refusal code as
    a local constant (the carrier raises a bare literal, and this module already
    keys on ``reconcile_current_dsp``'s status vocabulary the same way). If the
    carrier renames it, the acceptance below becomes silently unreachable and
    every anchor box goes back to failing step 3 — with no test failing. This is
    the test that fails instead.
    """
    from jasper.sound.graph_carrier import CarrierCannotHostEq, _ActiveGraphCarrier

    carrier = _ActiveGraphCarrier(None, is_baseline=False)
    with pytest.raises(CarrierCannotHostEq) as err:
        carrier.reemit(object())
    assert err.value.reason_code == CARRIER_TRANSIENT_ACTIVE_REFUSAL


# --- (a) the arm converges on a coherent ring-endpoint anchor ---------------


def test_a_coherent_ring_endpoint_anchor_is_proved_converged(tmp_path, monkeypatch):
    """THE FIX. The whole predicate, against real files, on the real fixture."""
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert ok, detail
    assert RING_WIRE_FORMAT_WIDE in detail


def test_the_arm_camilla_step_converges_on_that_anchor(tmp_path, monkeypatch):
    """The step 3 that failed on jts.local now converges, with its OWN detail.

    Distinct from ``reconciled``/``unchanged`` (a graph that was written) and
    from the refusal, because the journal and the operator's stdout line have to
    say which of the three happened.
    """
    from jasper.fanin import coupling_reconcile as cr

    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    _skipped_carrier_refusal(monkeypatch, reason=CARRIER_TRANSIENT_ACTIVE_REFUSAL)

    assert cr._reconcile_camilla(COUPLING_SHM_RING, reason="arm") == (
        True,
        CAMILLA_ANCHOR_CONVERGED_DETAIL,
    )
    assert CAMILLA_ANCHOR_CONVERGED_DETAIL not in ("reconciled", "unchanged")


@pytest.fixture
def _arm_spine_ready(tmp_path, monkeypatch):
    """The sibling suite's own arm-spine setup, reused rather than re-derived."""
    isolate_base_jasper_env(tmp_path, monkeypatch)
    force_ring_gates_pass(monkeypatch)


def _arm_with_camilla_detail(tmp_path, detail: str):
    """Run a real arm whose camilla step answers ``detail``. Returns the result."""
    from jasper.fanin.coupling_reconcile import reconcile_coupling

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("", encoding="utf-8")
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("", encoding="utf-8")
    return reconcile_coupling(
        COUPLING_SHM_RING,
        reason="test",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, detail),
        kick_hardware_reconcile=lambda: (True, ""),
        restart_voice=lambda: (True, ""),
        active_leader_check=lambda: False,
    )


def test_the_converged_arm_carries_its_detail_into_the_result(
    tmp_path, _arm_spine_ready
):
    """The link between the camilla step and what the operator is told."""
    result = _arm_with_camilla_detail(tmp_path, CAMILLA_ANCHOR_CONVERGED_DETAIL)
    assert result.ok, result.detail
    assert result.direction == "arm"
    assert result.detail == CAMILLA_ANCHOR_CONVERGED_DETAIL


def test_an_ordinary_arm_carries_no_detail(tmp_path, _arm_spine_ready):
    """CONTROL. Without this, the assertion above would also pass if EVERY arm
    had started reporting its camilla detail — which would change the operator's
    line on jts3 and on every already-commissioned box in the fleet.
    """
    result = _arm_with_camilla_detail(tmp_path, "reconciled")
    assert result.ok, result.detail
    assert result.detail == ""


def test_the_converged_arm_says_so_on_the_operator_stdout_line(
    tmp_path, monkeypatch, capsys
):
    """OBSERVABILITY, at the surface the operator actually reads.

    ``main()`` prints ``detail=`` only when the result carries one, so an
    ordinary re-emit stays silent (the control below) and the acceptance does
    not. Driven through the CLI rather than asserted on the dataclass because
    the stdout line is what a session transcript preserves.
    """
    from jasper.fanin.coupling_reconcile import CouplingResult
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(tmp_path / "entry.lock"))
    monkeypatch.setattr(
        cr,
        "reconcile_coupling",
        lambda *a, **k: CouplingResult(
            ok=True,
            desired=COUPLING_SHM_RING,
            changed=True,
            direction="arm",
            restarted_fanin=True,
            restarted_outputd=True,
            reconciled_camilla=True,
            detail=CAMILLA_ANCHOR_CONVERGED_DETAIL,
        ),
    )
    assert cr.main([COUPLING_SHM_RING]) == 0
    assert f"detail={CAMILLA_ANCHOR_CONVERGED_DETAIL}" in capsys.readouterr().out


# --- (b)(c) every other graph shape keeps failing ---------------------------


def test_an_anchor_still_at_the_ALOOP_endpoint_is_refused(tmp_path, monkeypatch):
    """The box that has not run step 1 yet. Its graph IS the anchor, but it
    plays the snd-aloop active lane — accepting it would report an arm converged
    while CamillaDSP still writes the lane the ring replaced.
    """
    from jasper.fanin import coupling_reconcile as cr

    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device="plug:jasper_capture",
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert OUTPUTD_ACTIVE_PLAYBACK_DEVICE in detail

    _skipped_carrier_refusal(monkeypatch, reason=CARRIER_TRANSIENT_ACTIVE_REFUSAL)
    step_ok, step_detail = cr._reconcile_camilla(COUPLING_SHM_RING, reason="arm")
    assert step_ok is False
    assert step_detail.startswith(CARRIER_TRANSIENT_ACTIVE_REFUSAL)


def test_a_half_moved_anchor_playing_the_aloop_lane_is_refused(tmp_path, monkeypatch):
    """The OTHER half-move, isolated so the playback axis is guarded on its own.

    Both device checks live in one ``or``, so a fixture that trips the capture
    half proves nothing about the playback half. This graph captures the ring
    and plays the snd-aloop active lane; the test above is its mirror.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert RING_ACTIVE_PLAYBACK_DEVICE in detail


def test_a_half_moved_anchor_capturing_the_tap_is_refused(tmp_path, monkeypatch):
    """The #2364 trap, refused on this path too: a graph that PLAYS the ring
    while capturing the snd-aloop tap reads a device nobody writes once fan-in
    stops feeding it — digital silence with every daemon healthy.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device="plug:jasper_capture",
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert RING_CAPTURE_DEVICE in detail


def test_a_commissioning_load_is_never_accepted(tmp_path, monkeypatch):
    """A per-driver commissioning graph is a TRANSIENT with a driver armed at
    level. It classifies like the anchor and can sit at the ring endpoint, so
    only the PATH tells them apart — which is why identity is proved against the
    box's published anchor record rather than against a classification.
    """
    from jasper.active_speaker.staging import DEFAULT_COMMISSIONING_CONFIG_NAME
    from jasper.fanin import coupling_reconcile as cr

    _stage_box(
        tmp_path,
        monkeypatch,
        graph_name=DEFAULT_COMMISSIONING_CONFIG_NAME,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert DEFAULT_COMMISSIONING_CONFIG_NAME in detail

    _skipped_carrier_refusal(monkeypatch, reason=CARRIER_TRANSIENT_ACTIVE_REFUSAL)
    step_ok, _ = cr._reconcile_camilla(COUPLING_SHM_RING, reason="arm")
    assert step_ok is False


def test_an_anchor_at_the_wrong_wire_is_refused(tmp_path, monkeypatch):
    """Coherent endpoint, WRONG width class: the graph was emitted narrow while
    the box declares the wide wire. The ioplug attaches with what the block says,
    so this is an attach failure waiting, not a converged arm.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt="S16_LE",
        ),
        wire_format=RING_WIRE_FORMAT_WIDE,
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "S16_LE" in detail and RING_WIRE_FORMAT_WIDE in detail


def test_an_anchor_at_the_wrong_WIDTH_is_refused(tmp_path, monkeypatch):
    """The CHANNELS axis, which the arm's own width preflight proves but the
    CONFIRM path does not run. Without it, an armed anchor box whose ACTIVE-ring
    width later sheared would be reported converged on every confirm tick
    instead of striking out and recovering to loopback — the ioplug attaches
    with what the block says, so a sheared width is a hard attach failure.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
            playback_channels=2,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "2 channels, expected 4" in detail, detail


def test_a_box_publishing_no_anchor_is_refused(tmp_path, monkeypatch):
    """FAIL-CLOSED on missing evidence: without a published staged record there
    is nothing the loaded graph could be proved to BE.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        publish_anchor=False,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "no staged startup anchor" in detail


def test_an_unreadable_graph_is_refused(tmp_path, monkeypatch):
    """FAIL-CLOSED at the entry. No statefile, an unreadable config, or one with
    no parseable devices block all reach here — none of them is proof, and a
    predicate that shrugged would accept an arm it could not see.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "cannot read the loaded CamillaDSP graph" in detail


def test_an_unusable_wire_declaration_is_refused(tmp_path, monkeypatch):
    """A wire token neither language recognizes. ``jasper-fanin`` parks at exit
    78 on the same value, so there is no wire to prove the graph against and the
    acceptance must not fall back to a guess.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        wire_format="S24_WHAT",
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "S24_WHAT" in detail


def test_a_malformed_staged_record_is_refused_not_raised(tmp_path, monkeypatch):
    """A record whose ``config`` is a truthy NON-mapping. The idiom the web
    reader uses (``(staged.get("config") or {}).get("path")``) raises
    AttributeError on exactly this shape — and an exception escaping here would
    unwind the ordered arm past the snapshot restore that makes a refused arm
    non-destructive. It must be a refusal, like every other missing proof.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        publish_anchor="not-a-mapping",
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "no staged startup anchor" in detail


def test_a_different_carrier_refusal_is_never_routed_to_the_acceptance(
    tmp_path, monkeypatch
):
    """The acceptance is keyed on ONE refusal code. A bonded-member refusal is a
    different contract with a different remedy, and a coherent-looking box must
    not launder it into a converged arm.
    """
    from jasper.fanin import coupling_reconcile as cr

    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    _skipped_carrier_refusal(monkeypatch, reason="eq_on_active_bonded_member")
    consulted: list[bool] = []
    monkeypatch.setattr(
        cr,
        "ring_endpoint_anchor_converged",
        lambda: (consulted.append(True) or (True, "should never be asked")),
    )

    assert cr._reconcile_camilla(COUPLING_SHM_RING, reason="arm") == (
        False,
        "eq_on_active_bonded_member",
    )
    assert consulted == []


# --- (d)(e) the untouched directions ----------------------------------------


def test_the_disarm_exemption_is_byte_identical(tmp_path, monkeypatch):
    """The loopback direction still accepts ANY skip, without consulting the
    acceptance at all — the disarm's whole point is that a flat box has nothing
    to flip, and a roleful box's graph is moved by the rollback ladder instead.
    """
    from jasper.fanin import coupling_reconcile as cr

    consulted: list[bool] = []
    monkeypatch.setattr(
        cr,
        "ring_endpoint_anchor_converged",
        lambda: (consulted.append(True) or (False, "should never be asked")),
    )
    for reason in (CARRIER_TRANSIENT_ACTIVE_REFUSAL, "flat_profile_noop"):
        _skipped_carrier_refusal(monkeypatch, reason=reason)
        assert cr._reconcile_camilla(COUPLING_LOOPBACK, reason="disarm") == (
            True,
            "skipped",
        )
    assert consulted == []


def test_an_applied_baseline_arm_is_unchanged(monkeypatch):
    """jts3's path. A commissioned box's graph IS a baseline carrier, so the
    reconcile RECONCILES and never reaches the acceptance — the arm reports the
    same detail it always did, and stays silent on the operator's stdout line.
    """
    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime

    async def fake_reconcile_current_dsp(**kwargs):
        return {"status": "reconciled"}

    monkeypatch.setattr(runtime, "reconcile_current_dsp", fake_reconcile_current_dsp)
    consulted: list[bool] = []
    monkeypatch.setattr(
        cr,
        "ring_endpoint_anchor_converged",
        lambda: (consulted.append(True) or (True, "should never be asked")),
    )

    assert cr._reconcile_camilla(COUPLING_SHM_RING, reason="arm") == (
        True,
        "reconciled",
    )
    assert consulted == []


def test_an_ordinary_arm_prints_no_detail(tmp_path, monkeypatch, capsys):
    """CONTROL for the stdout assertion above: without it, that test would also
    pass if EVERY arm had started printing its camilla detail.
    """
    from jasper.fanin.coupling_reconcile import CouplingResult
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(tmp_path / "entry.lock"))
    monkeypatch.setattr(
        cr,
        "reconcile_coupling",
        lambda *a, **k: CouplingResult(
            ok=True,
            desired=COUPLING_SHM_RING,
            changed=True,
            direction="arm",
            restarted_fanin=True,
            restarted_outputd=True,
            reconciled_camilla=True,
        ),
    )
    assert cr.main([COUPLING_SHM_RING]) == 0
    assert "detail=" not in capsys.readouterr().out
