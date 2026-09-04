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
)
from jasper.fanin.ring_health import ring_endpoint_anchor_converged
from jasper.fanin_coupling import (
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
    mute_gains_db: "dict[int, float] | None" = None,
    bypassed: bool = False,
) -> str:
    """A roleful boot graph, in the shape the readers actually parse.

    Ring A is always the 2-channel stereo program; the composite's ACTIVE ring
    is 4 wide (``active_ring_channels_for_topology`` on the fixture topology),
    so these are the widths a coherent composite anchor declares.

    The ``filters``/``pipeline`` half is REAL, not a stub: the acceptance proves
    every output ends in a wired hard mute, so a fixture that omitted the mutes
    would make every test here a mute-refusal test by accident. Defaults to the
    honest anchor — every output at ``STARTUP_MUTE_GAIN_DB``, muted and wired.
    ``mute_gains_db`` overrides individual outputs (the unmuted-at-the-anchor
    attack); ``bypassed`` marks the mute step skipped, which leaves the channel
    live while the filter definition still reads as muted.
    """
    from jasper.active_speaker.camilla_yaml import (
        STARTUP_MUTE_GAIN_DB,
        output_commission_mute_name,
    )

    gains = dict(mute_gains_db or {})
    filters = []
    steps = []
    for index in range(playback_channels):
        name = output_commission_mute_name(index)
        gain = gains.get(index, STARTUP_MUTE_GAIN_DB)
        muted = gain <= STARTUP_MUTE_GAIN_DB
        filters.append(
            f"  {name}:\n"
            "    type: Gain\n"
            "    parameters:\n"
            f"      gain: {gain}\n"
            f"      mute: {'true' if muted else 'false'}\n"
        )
        steps.append(
            "  - type: Filter\n"
            f"    channels: [{index}]\n"
            f"    names: [{name}]\n"
            + ("    bypassed: true\n" if bypassed else "")
        )
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
        "filters:\n" + "".join(filters) + "pipeline:\n" + "".join(steps)
    )


def _stage_box(
    tmp_path: Path,
    monkeypatch,
    *,
    graph_yaml: str,
    graph_name: str = "active_speaker_staged_startup.yml",
    publish_anchor: object = True,
    staged_status: str = "staged",
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
    from jasper.fanin import ring_health as rh

    configs = tmp_path / "configs"
    configs.mkdir(parents=True, exist_ok=True)
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
                "status": staged_status,
                # True -> the real record. False -> a genuinely ABSENT anchor
                # (``config: null``, what ``status=not_staged`` writes).
                # Anything else is written verbatim, for the malformed shapes.
                "config": (
                    {"path": str(anchor)}
                    if publish_anchor is True
                    else (None if publish_anchor is False else publish_anchor)
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
    monkeypatch.setattr(rh, "JASPER_ENV_PATH", str(jasper_env))
    monkeypatch.setattr(rh, "FANIN_ENV_PATH", str(fanin_env))

    monkeypatch.setattr(rh, "load_topology_for_wire", _composite_active_2way)
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

    assert cr._reconcile_camilla(reason="arm") == (
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
        reason="test",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda: (True, detail),
        kick_hardware_reconcile=lambda: (True, ""),
        restart_voice=lambda: (True, ""),
    )


def test_the_converged_arm_carries_its_detail_into_the_result(
    tmp_path, _arm_spine_ready
):
    """The link between the camilla step and what the operator is told."""
    result = _arm_with_camilla_detail(tmp_path, CAMILLA_ANCHOR_CONVERGED_DETAIL)
    assert result.ok, result.detail
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
            changed=True,
            restarted_fanin=True,
            restarted_outputd=True,
            reconciled_camilla=True,
            detail=CAMILLA_ANCHOR_CONVERGED_DETAIL,
        ),
    )
    assert cr.main([COUPLING_SHM_RING]) == 0
    assert f"detail={CAMILLA_ANCHOR_CONVERGED_DETAIL}" in capsys.readouterr().out


# --- the DAEMON's answer beats the statefile's ------------------------------


def test_the_daemon_reported_path_wins_over_a_diverged_statefile(
    tmp_path, monkeypatch
):
    """THE FAIL-OPEN WINDOW THIS CLOSES.

    The refusal is about the graph the running daemon holds
    (``cam.get_config_file_path`` over CamillaDSP's websocket); the statefile is
    a durable POINTER with several other writers (``write_camilla_statefile``
    from ``baseline-reemit`` and ``runtime-safe-graph``, the pipe guard), so it
    can name a coherent anchor while the daemon is still on the previous graph.
    Proving identity against the statefile would report ``converged_anchor``
    with CamillaDSP writing the lane the ring replaced and outputd already
    flipped to read the ring — a ring with no writer, which is the #2364 silence
    class arrived at from the other side.

    Here the statefile names the coherent anchor and the daemon names something
    else. The daemon wins, so this REFUSES.
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
    # Sanity: through the statefile alone this box converges. Without this the
    # refusal below could come from the fixture rather than from the divergence.
    assert ring_endpoint_anchor_converged()[0] is True

    other = tmp_path / "configs" / "some_other_graph.yml"
    other.write_text(
        _graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
        encoding="utf-8",
    )
    ok, detail = ring_endpoint_anchor_converged(loaded_config_path=str(other))
    assert not ok
    assert "some_other_graph.yml" in detail, detail
    assert "not this box's published startup anchor" in detail, detail


def test_the_arm_hands_the_camilla_rung_the_daemon_reported_path(
    tmp_path, monkeypatch
):
    """The wiring, not just the parameter: the rung must actually pass the
    payload field through. Without this the divergence test above guards a
    function nobody calls that way.
    """
    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime

    async def fake_reconcile_current_dsp(**_kwargs):
        return {
            "status": "skipped",
            "reason": CARRIER_TRANSIENT_ACTIVE_REFUSAL,
            "current_config_path": "/var/lib/camilladsp/configs/daemon-says-this.yml",
        }

    monkeypatch.setattr(runtime, "reconcile_current_dsp", fake_reconcile_current_dsp)
    seen: list[object] = []
    monkeypatch.setattr(
        cr,
        "ring_endpoint_anchor_converged",
        lambda *, loaded_config_path=None: (
            seen.append(loaded_config_path) or (False, "stub")
        ),
    )

    cr._reconcile_camilla(reason="arm")
    assert seen == ["/var/lib/camilladsp/configs/daemon-says-this.yml"]


# --- the anchor must actually BE all-muted ----------------------------------


def test_an_unmuted_graph_at_the_anchor_path_is_refused(tmp_path, monkeypatch):
    """THE HEARING-SAFETY HARD STOP. The acceptance's own success sentence says
    "an all-muted anchor hosts no EQ" — so it measures that, rather than
    inheriting it from the writer that emitted the file. A tweeter left unmuted
    at −20 dB at the published anchor path was ACCEPTED before this.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
            mute_gains_db={1: -20.0},
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "not the all-muted anchor" in detail, detail
    assert "startup mute floor" in detail, detail
    assert detail.rstrip().endswith("1"), detail


@pytest.mark.parametrize(
    ("graph_overrides", "stage_overrides", "expected"),
    [
        pytest.param(
            {"mute_gains_db": {index: 0.0 for index in range(4)}},
            {},
            ("0, 1, 2, 3",),
            id="a_full_scale_graph_at_the_anchor_path_is_refused",
        ),
        pytest.param(
            {"bypassed": True},
            {},
            ("bypassed step",),
            id="a_bypassed_mute_step_is_refused",
        ),
        pytest.param(
            {"playback_device": OUTPUTD_ACTIVE_PLAYBACK_DEVICE},
            {},
            (RING_ACTIVE_PLAYBACK_DEVICE,),
            id="a_half_moved_anchor_playing_the_aloop_lane_is_refused",
        ),
        pytest.param(
            {"capture_device": "plug:jasper_capture"},
            {},
            (RING_CAPTURE_DEVICE,),
            id="a_half_moved_anchor_capturing_the_tap_is_refused",
        ),
        pytest.param(
            {"playback_channels": 2},
            {},
            ("2 channels, expected 4",),
            id="an_anchor_at_the_wrong_WIDTH_is_refused",
        ),
        pytest.param(
            {},
            {"staged_status": "blocked"},
            ("status='blocked'",),
            id="a_blocked_staged_record_is_refused",
        ),
    ],
)
def test_anchor_shape_variants_are_refused(
    tmp_path, monkeypatch, graph_overrides, stage_overrides, expected
):
    """Each case is one distinct anchor shape the acceptance must refuse."""
    graph_kwargs = dict(
        capture_device=RING_CAPTURE_DEVICE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        fmt=RING_WIRE_FORMAT_WIDE,
    )
    graph_kwargs.update(graph_overrides)
    _stage_box(
        tmp_path,
        monkeypatch,
        graph_yaml=_graph_yaml(**graph_kwargs),
        **stage_overrides,
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    for substring in expected:
        assert substring in detail, detail


def _append_boost_filter(text: str) -> str:
    """Add a +240 dB Gain to the graph's ``filters`` block."""
    return text.replace(
        "pipeline:\n",
        "  as_boost:\n    type: Gain\n    parameters:\n"
        "      gain: 240.0\n      mute: false\n"
        "pipeline:\n",
    )


def test_a_gain_appended_after_the_mutes_is_refused(tmp_path, monkeypatch):
    """ATTACK 1 of the three the repo already records.

    A ``+240 dB`` Gain appended as an EXTRA pipeline step on every channel.
    CamillaDSP applies later steps after earlier ones, so the mute is undone —
    yet fact 1 (a wired hard mute exists on the channel) still reads as
    satisfied. Accepted before the acceptance asked for TERMINALITY.
    """
    text = _append_boost_filter(
        _graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        )
    )
    for index in range(4):
        text += f"  - type: Filter\n    channels: [{index}]\n    names: [as_boost]\n"
    _stage_box(tmp_path, monkeypatch, graph_yaml=text)

    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "TERMINAL" in detail, detail
    assert "0, 1, 2, 3" in detail, detail


def test_a_gain_injected_into_the_mute_step_is_refused(tmp_path, monkeypatch):
    """ATTACK 2. The same gain appended INTO each mute step's own ``names`` list,
    after the mute — a step's filters apply in order, so this re-amplifies
    without adding a step at all.
    """
    from jasper.active_speaker.camilla_yaml import output_commission_mute_name

    text = _append_boost_filter(
        _graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        )
    )
    for index in range(4):
        name = output_commission_mute_name(index)
        text = text.replace(f"names: [{name}]\n", f"names: [{name}, as_boost]\n")
    _stage_box(tmp_path, monkeypatch, graph_yaml=text)

    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "TERMINAL" in detail, detail


def test_a_dither_step_after_the_mutes_is_refused(tmp_path, monkeypatch):
    """ATTACK 3. A ``Dither`` step appended after the mutes — it GENERATES signal
    into a channel the mute is supposed to have silenced, so "no step of any
    other type follows the mute" is the fact that catches it.
    """
    text = _graph_yaml(
        capture_device=RING_CAPTURE_DEVICE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        fmt=RING_WIRE_FORMAT_WIDE,
    ) + "  - type: Dither\n    channels: [0, 1, 2, 3]\n"
    _stage_box(tmp_path, monkeypatch, graph_yaml=text)

    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "TERMINAL" in detail, detail


def test_the_real_emitted_anchor_passes_terminality(tmp_path):
    """THE COUNTER-RISK, checked rather than assumed.

    Terminality is only safe to demand if the SHIPPED emitter satisfies it —
    otherwise this hardening would refuse the very box the PR exists to unblock.
    Emits a genuine anchor through ``stage_protected_startup_config`` (no
    hand-built YAML) and walks every output with the same shared primitive the
    acceptance uses.
    """
    import yaml as yaml_lib

    from jasper.active_speaker.camilla_yaml import (
        STARTUP_MUTE_GAIN_DB,
        output_commission_mute_name,
    )
    from jasper.active_speaker.graph_safety import (
        output_terminally_muted,
        view_from_yaml_dict,
    )
    from jasper.active_speaker.staging import stage_protected_startup_config

    from tests.active_speaker_fixtures import (
        mono_output_topology,
        valid_camilla_config,
    )

    out = tmp_path / "anchor.yml"
    payload = stage_protected_startup_config(
        mono_output_topology(),
        config_path=out,
        metadata_path=tmp_path / "anchor.json",
        validate=valid_camilla_config,
        created_at="2026-08-15T00:00:00Z",
    )
    assert payload["status"] == "staged", payload.get("issues")

    doc = yaml_lib.safe_load(out.read_text(encoding="utf-8"))
    view = view_from_yaml_dict(doc)
    width = payload["config"]["playback_channels"]
    assert width >= 1
    assert all(
        output_terminally_muted(
            doc,
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
        for index in range(width)
    ), "the shipped emitter must satisfy terminality, or this gate is unshippable"


def test_an_unparseable_anchor_is_refused(tmp_path, monkeypatch):
    """Fail-closed on a graph whose devices block parses but whose YAML does
    not — the two readers disagree, and disagreement is not proof.
    """
    good = _graph_yaml(
        capture_device=RING_CAPTURE_DEVICE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        fmt=RING_WIRE_FORMAT_WIDE,
    )
    _stage_box(tmp_path, monkeypatch, graph_yaml=good + "\n  : : torn\n")
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "YAML does not parse" in detail, detail


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
    step_ok, step_detail = cr._reconcile_camilla(reason="arm")
    assert step_ok is False
    assert step_detail.startswith(CARRIER_TRANSIENT_ACTIVE_REFUSAL)


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
    step_ok, _ = cr._reconcile_camilla(reason="arm")
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
    assert "no usable staged startup anchor" in detail, detail
    # CONTROL for the malformed-record message below: an ABSENT anchor must not
    # borrow the corrupt-record sentence either.
    assert "record present but malformed" not in detail, detail


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

    ASSERTS THE PARSER'S OWN REFUSAL SENTENCE, not the token. Asserting only
    ``"S24_WHAT" in detail`` cannot tell this refusal from a LEGAL-mismatch one:
    under a lenient parser the bad token resolves to the wire, the format
    compare then fails, and the mismatch sentence quotes the same token — so the
    assertion passes while the parser has stopped rejecting anything. The
    correctness lens demonstrated exactly that mutation surviving. The control
    below pins the other sentence.
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
    assert "S24_WHAT" in detail, detail
    assert "refusing to arm on a wire this box cannot declare" in detail, detail
    assert "does not state this box's ring wire" not in detail, detail


def test_a_legal_wire_mismatch_produces_the_OTHER_sentence(tmp_path, monkeypatch):
    """CONTROL for the test above. A legal-but-different wire is a MISMATCH, and
    it must not borrow the parser's refusal sentence — otherwise the assertion
    above would pass for a box whose token was never rejected at all.
    """
    _stage_box(
        tmp_path,
        monkeypatch,
        wire_format="S16_LE",
        graph_yaml=_graph_yaml(
            capture_device=RING_CAPTURE_DEVICE,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
            fmt=RING_WIRE_FORMAT_WIDE,
        ),
    )
    ok, detail = ring_endpoint_anchor_converged()
    assert not ok
    assert "does not state this box's ring wire" in detail, detail
    assert "refusing to arm on a wire this box cannot declare" not in detail, detail


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
    assert "record present but malformed" in detail, detail
    assert "config is str, not a mapping" in detail, detail
    # The DISTINCTION is the point: a corrupt record must not read as an absent
    # one, or a debugging operator re-stages when the record is the defect.
    assert "staged status=" not in detail, detail


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

    assert cr._reconcile_camilla(reason="arm") == (
        False,
        "eq_on_active_bonded_member",
    )
    assert consulted == []


# --- the JOURNAL half of the observability promise --------------------------


def _anchor_log_records(caplog):
    """(result, levelno) for every acceptance line this module emitted."""
    out = []
    for record in caplog.records:
        message = record.getMessage()
        for result in ("camilla_converged_anchor", "camilla_anchor_not_converged"):
            if f"result={result}" in message:
                out.append((result, record.levelno))
    return out


def test_the_journal_records_both_acceptance_outcomes(tmp_path, monkeypatch, caplog):
    """THE SURFACE AN OPERATOR GREPS ON A PI, pinned.

    The stdout half had an assertion and a control; the journal half — the only
    place the refusal REASON is recorded, and what the doc states verbatim — had
    nothing, so a rename or an INFO/WARNING flip would have been silent. Four
    states, the same probe shape the resilience lens ran by hand.
    """
    import logging

    from jasper.fanin import coupling_reconcile as cr

    coherent = _graph_yaml(
        capture_device=RING_CAPTURE_DEVICE,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        fmt=RING_WIRE_FORMAT_WIDE,
    )
    incoherent = _graph_yaml(
        capture_device="plug:jasper_capture",
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        fmt=RING_WIRE_FORMAT_WIDE,
    )

    # 1. ARM on a coherent anchor -> INFO, converged.
    _stage_box(tmp_path, monkeypatch, graph_yaml=coherent)
    _skipped_carrier_refusal(monkeypatch, reason=CARRIER_TRANSIENT_ACTIVE_REFUSAL)
    with caplog.at_level(logging.INFO, logger=cr.logger.name):
        cr._reconcile_camilla(reason="arm")
    assert _anchor_log_records(caplog) == [
        ("camilla_converged_anchor", logging.INFO)
    ]

    # 2. ARM on an incoherent anchor -> WARNING, not converged, reason recorded.
    caplog.clear()
    _stage_box(tmp_path / "b", monkeypatch, graph_yaml=incoherent)
    with caplog.at_level(logging.INFO, logger=cr.logger.name):
        cr._reconcile_camilla(reason="arm")
    assert _anchor_log_records(caplog) == [
        ("camilla_anchor_not_converged", logging.WARNING)
    ]
    assert f"refusal={CARRIER_TRANSIENT_ACTIVE_REFUSAL}" in caplog.text

    # 3. A DIFFERENT refusal -> the acceptance is never consulted, so neither
    #    line appears.
    caplog.clear()
    _skipped_carrier_refusal(monkeypatch, reason="eq_on_active_bonded_member")
    with caplog.at_level(logging.INFO, logger=cr.logger.name):
        cr._reconcile_camilla(reason="arm")
    assert _anchor_log_records(caplog) == []


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

    assert cr._reconcile_camilla(reason="arm") == (
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
            changed=True,
            restarted_fanin=True,
            restarted_outputd=True,
            reconciled_camilla=True,
        ),
    )
    assert cr.main([COUPLING_SHM_RING]) == 0
    assert "detail=" not in capsys.readouterr().out
