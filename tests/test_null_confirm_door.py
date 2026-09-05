# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The summed reverse-null instrument: stimulus, admission, and the banked row.

Pins the properties the measurement rests on rather than the code that produces
them: the two branches are sample-identical where both are open, the ONE gain is
the most restrictive cap backed off, admission judges each branch on the band its
gate actually emits, and every banked row carries enough to be graded alone.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.program_admission import (
    ProgramAdmissionRefusal,
    readmit_program_from_wav,
)
from jasper.active_speaker.session_volume_plan import session_measurement_volume_db
from jasper.audio_measurement.analysis import ShoulderSpan
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.interference_nulls import (
    branch_gap_null_depth_ceiling_db,
)
from jasper.audio_measurement.program import (
    PROGRAM_PHASE_MEASURE,
    RoleBand,
    build_null_confirm_program,
    null_confirm_channel_plan,
    segment_emitted_band_hz,
    segment_stimulus,
)
from jasper.cli import null_door
from tests.test_active_speaker_program_admission import _profile_and_targets

FC_HZ = 2000.0


def _roles(woofer=(500.0, 3000.0), tweeter=(1500.0, 10_000.0)):
    return [
        RoleBand("woofer", 0, FrequencyBand(*woofer)),
        RoleBand("tweeter", 1, FrequencyBand(*tweeter)),
    ]


def _program(gain_db=-20.0, roles=None, **kw):
    return build_null_confirm_program(
        FC_HZ,
        roles or _roles(),
        gain_db=gain_db,
        sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        **kw,
    )


def _full_amplitude_window(segment) -> tuple[int, int]:
    """Where this branch plays at full level: the gate, fades excluded."""
    lo = segment.gate_start_sample + (
        segment.gate_fade_samples if segment.gate_start_sample else 0
    )
    end = (
        segment.n_samples if segment.gate_end_sample is None
        else segment.gate_end_sample
    )
    hi = end - (
        segment.gate_fade_samples if segment.gate_end_sample is not None else 0
    )
    return lo, hi


# --------------------------------------------------------------------------- #
# the stimulus
# --------------------------------------------------------------------------- #


def test_branches_are_sample_identical_where_both_are_open():
    """THE property the measurement is. Two branches cancel acoustically only if
    they are the same waveform; anything that makes them differ — a regenerated
    sub-sweep, a per-role gain, a fade on an edge the gate does not cut — turns
    the null into a measurement of the stimulus rather than of the crossover."""
    program, _plan = _program()
    segments = program.stimulus_segments()
    assert len(segments) == 2

    windows = [_full_amplitude_window(s) for s in segments]
    lo = max(w[0] for w in windows)
    hi = min(w[1] for w in windows)
    assert lo < hi, "the branches share no full-amplitude window"

    waveforms = [segment_stimulus(s) for s in segments]
    assert np.array_equal(waveforms[0][lo:hi], waveforms[1][lo:hi])
    # Not vacuous: there is real signal in that window.
    assert float(np.max(np.abs(waveforms[0][lo:hi]))) > 0.0


def test_every_branch_carries_one_shared_gain():
    """The identity above is only reachable at ONE level. A per-role clamp inside
    the composer would bake different amplitudes into the two waveforms, so this
    pins the invariant that makes the caller's min-cap clamp necessary."""
    program, _plan = _program(gain_db=-20.0)
    gains = {s.gain_db for s in program.stimulus_segments()}
    assert gains == {-20.0}


def test_the_gate_silences_and_never_regenerates():
    """A gated branch is its parent with samples removed, not a fresh sweep."""
    program, plan = _program()
    tweeter = next(
        s for s in program.stimulus_segments() if s.role == "tweeter"
    )
    assert tweeter.is_gated
    rendered = segment_stimulus(tweeter)
    assert float(np.max(np.abs(rendered[: tweeter.gate_start_sample]))) == 0.0
    # And the parent band is what the schedule still declares, so regeneration
    # stays deterministic across both branches.
    assert (tweeter.f1_hz, tweeter.f2_hz) == plan.band_hz


def test_an_ungated_program_keeps_its_program_id():
    """The gate fields are omitted from an ungated segment's dict, so every
    program composed before they existed hashes to the same ``program_id``."""
    from jasper.audio_measurement.program import build_measure_program

    program = build_measure_program(
        {"woofer": -6.0, "tweeter": -46.0}, _roles(), downstream_gain_db=-10.0,
    )
    for segment in program.stimulus_segments():
        assert not segment.is_gated
        assert "gate_start_sample" not in segment.to_dict()


@pytest.mark.parametrize("fc_hz", [80.0, 150.0, 300.0, 10_000.0, 12_000.0])
def test_a_corner_without_run_up_past_its_shoulders_is_refused(fc_hz):
    """The summed sweep's envelope cannot reach past both shoulders at these
    corners, so a depth read there would be two clamped edge bins. Refused at
    compose time rather than reported as a measurement."""
    wide = _roles(woofer=(20.0, 20_000.0), tweeter=(20.0, 20_000.0))
    with pytest.raises(ValueError):
        build_null_confirm_program(
            fc_hz, wide, gain_db=-20.0,
            sweep_duration_limits_s={"woofer": 6.0, "tweeter": 6.0},
        )


def test_declared_bands_that_do_not_bracket_fc_are_refused():
    """Both branches must be open AT Fc with the fades clear of it — that
    coincidence is the whole measurement. A pair whose overlap misses Fc has no
    coordinate at which it can be measured cancelling."""
    with pytest.raises(ValueError):
        null_confirm_channel_plan(
            FC_HZ,
            _roles(woofer=(500.0, 1200.0), tweeter=(3000.0, 10_000.0)),
            sweep_s=2.0,
        )


def test_the_overlap_brackets_fc_and_is_narrower_than_the_sweep():
    """The plan's overlap is the acoustic analogue of two banked curves' shared
    band: where BOTH branches carry evidence, which is where a shoulder may go."""
    _program_, plan = _program()
    assert plan.overlap_hz[0] < FC_HZ < plan.overlap_hz[1]
    assert plan.band_hz[0] < plan.overlap_hz[0]
    assert plan.overlap_hz[1] < plan.band_hz[1]


def test_the_sweep_fits_the_tightest_role_duration_limit():
    """One sweep serves both branches, so it fits the tighter limit — composing
    at the nominal length is what admission refuses on every real 2-way."""
    program, _plan = _program()
    for segment in program.stimulus_segments():
        assert segment.n_samples / 48_000 <= 2.0


def test_a_confirm_needs_its_roles_duration_limits():
    with pytest.raises(ValueError):
        build_null_confirm_program(FC_HZ, _roles(), gain_db=-20.0)


# --------------------------------------------------------------------------- #
# admission
# --------------------------------------------------------------------------- #


def test_admission_judges_each_branch_on_the_band_its_gate_emits():
    """A gated branch's parent band is wider than what it plays. Admission must
    judge the EMITTED band: the parent would refuse a driver for frequencies the
    gate silences, and would let an ungated claim stand for one that does emit
    them."""
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    program, _plan = build_null_confirm_program(
        FC_HZ,
        _roles(woofer=(500.0, 3000.0), tweeter=(1500.0, 10_000.0)),
        # The most restrictive cap (-65) less the session volume, backed off —
        # what the door computes, spelled out so this test stays about the BAND.
        gain_db=-65.0 - sv - 0.01,
        sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        downstream_gain_db=sv,
    )
    admission = _admit(program, topology, profile, targets, sv)
    by_role = {s.role: s for s in admission.segments}
    assert set(by_role) == {"woofer", "tweeter"}
    for role, segment_admission in by_role.items():
        source = next(s for s in program.stimulus_segments() if s.role == role)
        assert segment_admission.band == segment_emitted_band_hz(source)

    # The DISCRIMINATING fact, and the reason the assertions above are not
    # enough on their own: the parent sweep starts a full octave below the
    # tweeter's permitted floor, so judging this program by `segment.f1_hz`
    # refuses it. Admission is a VERDICT, not a report — reverting the
    # derivation would leave every band field above still reading correctly
    # while the plan underneath was judged on frequencies the gate silences.
    tweeter = next(s for s in program.stimulus_segments() if s.role == "tweeter")
    permitted_lo = 1500.0
    assert tweeter.f1_hz < permitted_lo < segment_emitted_band_hz(tweeter)[0]
    assert admission.allowed, [r.value for r in admission.refusals]
    assert all(s.execution_allowed for s in admission.segments)


def _admit(program, topology, profile, targets, sv, pcm=None):
    """Through the production play-time door -- ``admit_excitation_program``
    had zero production callers and was retired."""
    import tempfile
    from pathlib import Path

    from jasper.audio_measurement.program import write_program_wav

    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "program.wav"
        if pcm is None:
            write_program_wav(wav, program)
        else:
            from scipy.io import wavfile
            clipped = np.clip(pcm, -1.0, 1.0)
            wavfile.write(
                str(wav), program.sample_rate_hz, (clipped * 32767.0).astype(np.int16)
            )
        return readmit_program_from_wav(
            program, wav, topology=topology, safety_profile=profile,
            role_targets=targets, session_volume_db=sv,
        )


def test_admission_verifies_the_rendered_bytes_obey_the_gate():
    """The gate's SECOND leg: metadata cannot verify itself.

    `segment_emitted_band_hz` narrows a gated segment's claimed band from the
    SCHEDULE. If the renderer stopped applying gates, that narrow claim would
    stay intact while the full parent sweep — an octave below the tweeter's
    declared floor — reached the driver, and admission would approve it. This
    drives exactly that scenario by handing admission an UNGATED render of a
    gated program, which is what a broken `segment_stimulus` produces.
    """
    from jasper.audio_measurement.program import segment_stimulus

    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    program, _plan = build_null_confirm_program(
        FC_HZ,
        _roles(woofer=(500.0, 3000.0), tweeter=(1500.0, 10_000.0)),
        gain_db=-65.0 - sv - 0.01,
        sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        downstream_gain_db=sv,
    )
    assert _admit(program, topology, profile, targets, sv).allowed

    # The attack: same schedule, same narrow band claims, gates NOT applied.
    ungated = np.zeros((program.total_samples, program.channels), dtype=np.float32)
    for seg in program.stimulus_segments():
        bare = segment_stimulus(replace(seg, gate_start_sample=0,
                                        gate_end_sample=None, gate_fade_samples=0))
        ungated[seg.start_sample:seg.start_sample + seg.n_samples, seg.channel] = bare

    verdict = _admit(program, topology, profile, targets, sv, pcm=ungated)
    assert not verdict.allowed
    assert ProgramAdmissionRefusal.GATE_NOT_APPLIED in verdict.refusals


def test_the_bytes_leg_is_vacuous_for_the_wizard_summed_program():
    """Direction two: an ungated program has no gate claim, so nothing to
    verify. The shipped summed sweep must not start being refused."""
    from jasper.audio_measurement.program import build_verify_program

    program = build_verify_program(FC_HZ, gain_db=-20.0, downstream_gain_db=-10.0)
    assert all(not s.is_gated for s in program.stimulus_segments())
    assert _gate_leaks(program) == []


def test_the_bytes_leg_is_vacuous_for_per_driver_programs():
    """Direction three, the FALSE-REFUSAL one: `jasper-measure`'s per-driver
    programs are full-band per role and ungated. They legitimately emit across
    the whole declared band, and a check that fired on them would refuse a
    correct measurement."""
    from jasper.audio_measurement.program import build_measure_program

    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    program = build_measure_program(
        {"woofer": -6.0, "tweeter": -46.0},
        _roles(woofer=(500.0, 1600.0), tweeter=(1600.0, 10_000.0)),
        downstream_gain_db=sv,
    )
    assert all(not s.is_gated for s in program.stimulus_segments())
    assert _gate_leaks(program) == []
    assert _admit(program, topology, profile, targets, sv).allowed


def _gate_leaks(program):
    from jasper.active_speaker.program_admission import _gate_leak_refusals
    from jasper.audio_measurement.program import render_program_pcm

    return _gate_leak_refusals(program, render_program_pcm(program))


def test_a_measure_phase_program_needs_no_admission_widening():
    """The confirm rides an already-admitted phase. Its stimulus moves no gate:
    if this ever composes under a phase ``_validate_program`` does not admit, the
    protection boundary moved to make room for it."""
    program, _plan = _program()
    assert program.phase == PROGRAM_PHASE_MEASURE


# --------------------------------------------------------------------------- #
# the door's own arithmetic
# --------------------------------------------------------------------------- #


def test_the_stimulus_gain_is_the_most_restrictive_cap_backed_off():
    """C2 / #3343 blocker-A: the composer applies ONE caller-supplied gain and
    documents that the caller clamps. This is that clamp. A gain derived from
    anything but ``min(caps)`` composes above some driver's ceiling."""
    context = SimpleNamespace(
        fc_hz=FC_HZ,
        roles_bands=_roles(),
        driver_caps_dbfs={"woofer": 0.0, "tweeter": -65.0},
        driver_sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        session_volume_db=-30.0,
    )
    program, _plan, gain_db = null_door._compose(context, FC_HZ)
    # Effective peak = gain + session volume, and admission caps it at the
    # driver's own cap. Assert against the TIGHTEST, which is what min() picks.
    assert gain_db + context.session_volume_db <= min(
        context.driver_caps_dbfs.values()
    )
    assert {s.gain_db for s in program.stimulus_segments()} == {gain_db}


@pytest.mark.parametrize(
    "gap_db,expected",
    [(0.0, math.inf), (10.0, 3.30), (33.0, 0.20)],
)
def test_the_branch_gap_bounds_the_depth(gap_db, expected):
    """Two branches ``gap`` apart cannot cancel deeper than this however right
    the delay is. 10 dB → ~3.3 dB and 33 dB → ~0.2 dB are the two figures the
    propose door and the composer already state in prose."""
    ceiling = branch_gap_null_depth_ceiling_db(gap_db)
    if math.isinf(expected):
        assert math.isinf(ceiling)
    else:
        assert ceiling == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(fc_hz=-1.0), "NULL_REFUSE_FC_INVALID"),
        (dict(fc_hz=12_000.0), "NULL_REFUSE_NO_SHOULDER_RUN_UP"),
        (
            dict(roles=_roles(woofer=(500.0, 1200.0), tweeter=(3000.0, 10_000.0))),
            "NULL_REFUSE_OVERLAP_EXCLUDES_FC",
        ),
        (
            dict(roles=_roles(woofer=(6000.0, 9000.0), tweeter=(6000.0, 9000.0))),
            "NULL_REFUSE_ROLE_BAND_DISJOINT",
        ),
        (dict(limits={"woofer": 6.0}), "NULL_REFUSE_LIMITS_INCOMPLETE"),
    ],
)
def test_each_compose_refusal_carries_its_own_reason(kwargs, expected):
    """Four distinct facts refuse a compose; a grader keys on the REASON.

    One slug for all of them makes an invalid corner, a corner with no run-up,
    a driver whose declared band misses the sweep, and an overlap that cannot
    bracket Fc look identical in the bank — and they call for four different
    actions. The mapping is asserted against `program`'s own constants so the
    door cannot drift from the module that decides.
    """
    from jasper.audio_measurement import program as program_mod

    fc = kwargs.get("fc_hz", FC_HZ)
    roles = kwargs.get("roles", _roles())
    limits = kwargs.get("limits", {"woofer": 6.0, "tweeter": 2.0})
    with pytest.raises(program_mod.NullConfirmUnavailable) as caught:
        build_null_confirm_program(
            fc, roles, gain_db=-20.0, sweep_duration_limits_s=limits,
        )
    assert caught.value.reason == getattr(program_mod, expected)


def test_a_rendered_capture_reads_a_depth_end_to_end(tmp_path):
    """The door must be able to bank a MEASURED row — verified by execution.

    This is the whole instrument's happy path in one test: compose, render the
    two branches, sum them the way the air does, and read a depth. It exists
    because an `ambient_duration_s` on the door's capture call selects the
    signal-located branch, whose guard needs the controlled quiet to reach back
    past the entire sweep — structurally impossible for this door, so every
    first row raised an uncaught ValueError. Nothing short of running it caught
    that.
    """
    import wave

    from jasper.audio_measurement.program import render_program_pcm

    program, plan = _program()
    pcm = render_program_pcm(program)
    # What the microphone hears: one acoustic sum of both branches.
    summed = np.asarray(pcm, dtype=np.float64).sum(axis=1)
    rng = np.random.default_rng(11)
    summed = summed + rng.normal(0.0, 1e-4, summed.shape)
    path = tmp_path / "summed_capture.wav"
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(4)
        fh.setframerate(48_000)
        fh.writeframes(
            (np.clip(summed, -1.0, 1.0) * (2**31 - 1)).astype("<i4").tobytes()
        )

    depth_db, span = null_door._depth(path, program, plan, FC_HZ)

    assert isinstance(depth_db, float) and math.isfinite(depth_db)
    assert span.used_hz[0] < FC_HZ < span.used_hz[1]


def test_the_gap_ceiling_bounds_the_UNMATCHED_case_not_the_matched_one():
    """The gap is what the level match REMOVED, not what it installed.

    Reading the installed trims inverts the fact twice: the well-matched
    speaker carries the biggest trims and would stamp every row with a false
    ceiling, while the box with no level evidence — the genuinely unmatched
    case a reader most needs warned about — would report unbounded. Both
    directions are asserted here, so a fix that swaps only one of them fails.
    """
    declared = {"woofer": 88.0, "tweeter": 98.0}

    # No trims: the branches run at their declared sensitivities, and the
    # 10 dB spread bounds the null at ~3.3 dB.
    assert null_door._gap_ceiling_db({}, declared) == pytest.approx(3.30, abs=0.01)

    # Trims installed: the branches were levelled, so the declared spread
    # bounds nothing. The row discloses `trims_db` for the reader instead.
    assert null_door._gap_ceiling_db(
        {"woofer": 0.0, "tweeter": -10.0}, declared
    ) == math.inf

    # No evidence either way — no claim.
    assert null_door._gap_ceiling_db({}, {}) == math.inf
    assert null_door._gap_ceiling_db({}, {"woofer": 88.0}) == math.inf


# --------------------------------------------------------------------------- #
# coordinates and rows
# --------------------------------------------------------------------------- #


def _spec():
    from jasper.active_speaker.delay_sweep import sweep_spec

    return sweep_spec(
        crossover_fc_hz=FC_HZ,
        upper_role="tweeter",
        lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
        step_us=None,
    )


def _args(polarity=null_door.POLARITY_BOTH):
    return SimpleNamespace(polarity=polarity, inverted_role="tweeter")


def test_polarity_both_plays_the_pair_at_zero():
    """§1b is a PAIR: the in-phase take is the reference the inverted one is read
    against. That reference is a property of the speaker, not of a candidate
    delay, so it is measured once."""
    coordinates = null_door._coordinates(_spec(), _args(), (0.0,))
    assert [inverted for _c, inverted in coordinates] == [False, True]
    assert {c.relative_delay_us for c, _ in coordinates} == {0.0}


def test_delayed_coordinates_are_measured_inverted_only():
    spec = _spec()
    coordinates = null_door._coordinates(spec, _args(), (0.0, 100.0, -100.0))
    in_phase = [c for c, inv in coordinates if not inv]
    inverted = [c for c, inv in coordinates if inv]
    assert [c.relative_delay_us for c in in_phase] == [0.0]
    assert len(inverted) == 3
    # The sign chooses the branch through the walk's own owner, never a second
    # opinion here: a positive coordinate delays the upper driver.
    signed = {c.relative_delay_us: c.delay_target for c in inverted}
    assert signed[0.0] is None
    assert set(signed.values()) == {None, "tweeter", "woofer"}


@pytest.mark.parametrize(
    "polarity,expected",
    [(null_door.POLARITY_KEEP, [False]), (null_door.POLARITY_INVERT, [True])],
)
def test_single_polarity_runs_one_take(polarity, expected):
    coordinates = null_door._coordinates(
        _spec(), _args(polarity=polarity), (0.0,)
    )
    assert [inverted for _c, inverted in coordinates] == expected


def _span(used=(1500.0, 3000.0)):
    return ShoulderSpan(
        crossover_fc_hz=FC_HZ,
        overlap_hz=used,
        used_hz=used,
        samples_below_fc=64,
        samples_above_fc=64,
    )


def _row(**kw):
    base = dict(
        fc_hz=FC_HZ,
        candidate=_spec().dsp_candidate(100.0),
        inverted=True,
        inverted_role="tweeter",
        position_deg=0,
        trims_db={"woofer": 0.0, "tweeter": -10.0},
        trims_source="banked_base_trim",
        gap_ceiling_db=3.3,
        graph_fingerprint="abc123",
        wav_sha256="0" * 64,
    )
    base.update(kw)
    return null_door._row(**base)


def test_a_measured_row_is_self_contained():
    """Grading is comparing rows, so a row must carry everything that decides
    whether its depth means anything — with no lookup that can go stale."""
    row = _row(depth_db=-14.237, span=_span())
    assert row["status"] == "measured"
    assert row["depth_db"] == -14.24
    assert row["shoulders_used"] == [1500.0, 3000.0]
    assert row["clamped_lo"] is True and row["clamped_hi"] is True
    for field in (
        "ts", "fc_hz", "delay_us", "delayed_role", "polarity", "inverted_role",
        "position_deg", "trims_db", "gap_ceiling_db", "graph_fingerprint",
        "wav_sha256", "calibrated",
    ):
        assert field in row, field
    # The mic response does not cancel across shoulders an octave apart, so a
    # row must say whether it was corrected. Rows that differ on this are not
    # comparable, and the flag is what lets a reader tell.
    assert row["calibrated"] is False


def test_an_inverted_row_names_the_flipped_branch():
    """C4: "inverted" without the flipped branch does not name the measurement —
    two speakers can both report an inverted take having flipped different
    drivers, and their depths are not comparable."""
    inverted = _row(depth_db=-14.0, span=_span())
    assert inverted["polarity"] == "inverted"
    assert inverted["inverted_role"] == "tweeter"

    in_phase = _row(depth_db=-1.0, span=_span(), inverted=False)
    assert in_phase["polarity"] == "in_phase"
    assert in_phase["inverted_role"] is None


def test_a_genuine_zero_depth_banks_as_zero():
    """0.0 dB is a REAL reading — "the branches did not cancel" — and the most
    important one the polarity proof can return. It must survive the guard
    below, which is why that guard tests for ``None`` and not falsiness."""
    assert _row(depth_db=0.0, span=_span())["depth_db"] == 0.0


def test_a_measured_row_cannot_be_banked_without_a_depth():
    """A missing depth must CRASH, never bank as 0.0.

    ``depth_db or 0.0`` would turn an absent measurement into a legal,
    plausible reading a grader cannot tell from a real one — the one failure
    mode that corrupts the bank silently rather than loudly. Unreachable today
    (the refusal arm returns first, `_depth` returns a float), so this pins the
    shape rather than a live path: the row builder's signature still admits
    ``None`` for the refusal arm's sake, and this is what stops that leaking
    into the measured arm.
    """
    with pytest.raises(ValueError):
        _row(depth_db=None, span=_span())
    with pytest.raises(ValueError):
        _row(depth_db=-14.0, span=None)


def test_a_refused_row_says_why_and_claims_no_depth():
    row = _row(
        refusal=null_door.NullDoorRefused(
            null_door.REFUSE_NO_SHOULDERS, "no shoulder either side of Fc"
        )
    )
    assert row["status"] == "refused"
    assert row["reason"] == null_door.REFUSE_NO_SHOULDERS
    assert row["depth_db"] is None
    assert row["shoulders_used"] is None
    # Still self-contained: a refusal is an output, and a reader must be able to
    # tell WHICH coordinate could not be measured.
    assert row["delay_us"] == 100.0
    assert row["graph_fingerprint"] == "abc123"


def test_an_unbounded_gap_ceiling_serializes():
    """``inf`` is not JSON. A matched pair has no ceiling to disclose, and null
    reads as exactly that."""
    import json

    row = _row(depth_db=-20.0, span=_span(), gap_ceiling_db=math.inf)
    assert row["gap_ceiling_db"] is None
    json.dumps(row)


@pytest.mark.parametrize(
    "module,expected_owner",
    [
        ("jasper.cli.null_door", "jasper-null"),
        ("jasper.cli.measure", "jasper-measure"),
    ],
)
def test_the_door_holds_the_fanin_gate_under_its_own_registered_owner(
    module, expected_owner,
):
    """Three facts per door, and all bite on-box only.

    ``mux.FANIN_TEST_OWNERS`` is a closed allowlist: an owner missing from it is
    refused the diagnostic gate, the correction lane never reaches the speaker,
    and the door measures silence with every daemon healthy. And the owner must
    be the door's OWN — borrowing ``correction-measurement`` (or a sibling
    door's name) would pass the allowlist while filing this door's hold under
    someone else in every lease and recovery read.
    """
    import importlib

    from jasper.measurement_window import MEASUREMENT_GATE_OWNER
    from jasper.mux import FANIN_TEST_OWNERS

    owner = importlib.import_module(module).DOOR_GATE_OWNER
    assert owner == expected_owner
    assert owner in FANIN_TEST_OWNERS
    assert owner != MEASUREMENT_GATE_OWNER


@pytest.mark.parametrize(
    "module,name,expected",
    [
        ("jasper.active_speaker.crossover_v2.session_graph", "SessionGraphError",
         null_door.REFUSE_GRAPH_LOST),
        ("jasper.measurement_window", "MeasurementWindowError",
         null_door.REFUSE_ISOLATION_LOST),
        ("jasper.active_speaker.session_volume_plan", "SessionVolumePlanError",
         null_door.REFUSE_VOLUME_LOST),
        ("jasper.audio_measurement.wired_capture", "WiredCaptureError",
         null_door.REFUSE_CAPTURE_FAILED),
    ],
)
def test_each_named_mid_run_failure_has_a_reason(module, name, expected):
    """B4: the three failures that land BETWEEN two coordinates are covered.

    Each can arrive with k rows already on disk. A class missing from the map
    tracebacks out of the walk and the operator gets no JSON while those rows
    sit in `null_runs/` unnamed — evidence they have no id for and the next run
    cannot find.
    """
    import importlib

    cls = getattr(importlib.import_module(module), name)
    failures = null_door._mid_run_failures()
    assert cls in failures
    assert null_door._mid_run_reason(failures, cls("lost")) == expected


def test_a_subclass_of_a_named_failure_still_resolves():
    """Scanned, not keyed on ``type(exc)`` — a subclass must not fall through
    to the bare traceback the partial payload exists to prevent."""
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError

    class _Narrower(SessionGraphError):
        pass

    failures = null_door._mid_run_failures()
    assert null_door._mid_run_reason(failures, _Narrower("x")) == (
        null_door.REFUSE_GRAPH_LOST
    )


def test_an_interrupted_run_reports_the_rows_it_banked():
    exc = null_door.NullRunInterrupted(
        null_door.REFUSE_GRAPH_LOST, "the graph could not be re-proven",
        ["a.json", "b.json"],
    )
    assert exc.reason == null_door.REFUSE_GRAPH_LOST
    assert exc.banked == ["a.json", "b.json"]


def test_a_missing_microphone_exits_as_json_not_a_traceback(tmp_path, monkeypatch, capsys):
    """EVERY exit from this door speaks JSON, refusals included.

    The door's own no-mic refusal is a `WiredCaptureError`, and it used to
    traceback out of `main` with an empty stdout — leaving an operator (or the
    LLM reading a run) unable to tell a refusal from a crash by anything but
    the absence of output. The sibling door renders the same case as refusal
    JSON; this matches it.
    """
    from jasper.audio_measurement.wired_capture import WiredCaptureError

    def _no_mic():
        raise WiredCaptureError("no measurement microphone answered")

    monkeypatch.setattr(null_door, "_context", lambda: _fake_context())
    monkeypatch.setattr(null_door, "_level_trims", lambda _c: ({}, "none"))
    monkeypatch.setattr(null_door, "_protection_sections", lambda _c: None)
    monkeypatch.setattr(null_door, "_resolve_mic", _no_mic)

    code = null_door.main(["--bundle-dir", str(tmp_path)])

    assert code == null_door.EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == null_door.REFUSE_CAPTURE_FAILED


def _fake_context():
    return SimpleNamespace(
        fc_hz=FC_HZ,
        roles_bands=_roles(),
        driver_caps_dbfs={"woofer": 0.0, "tweeter": -65.0},
        driver_sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        session_volume_db=-30.0,
        preset=object(),
        topology=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="plughw:CARD=Loopback,DEV=0",
        safety_profile={},
        role_targets={},
        declared_sensitivities={},
    )


def _hardware_free_walk(monkeypatch, *, depth_db: float = -20.0) -> None:
    """Everything one walk needs that a hardware-free box cannot have.

    The box declaration, the microphone, the emission and the depth read; the
    door is the caller's, because how it ends is what each test is about.
    """
    monkeypatch.setattr(null_door, "_context", lambda: _fake_context())
    monkeypatch.setattr(null_door, "_level_trims", lambda _c: ({}, "none"))
    monkeypatch.setattr(null_door, "_protection_sections", lambda _c: None)
    monkeypatch.setattr(
        null_door, "_resolve_mic", lambda: SimpleNamespace(pcm=None),
    )
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)

    async def _play(*_args, **_kwargs) -> bytes:
        return b"\x00" * 8

    monkeypatch.setattr(null_door, "_play_and_capture", _play)
    monkeypatch.setattr(null_door, "_depth", lambda *_a, **_k: (depth_db, _span()))


def _install_door(monkeypatch, *, restore_error: Exception | None = None) -> None:
    """The held speaker, optionally failing to give the entry graph back."""
    from jasper.active_speaker.crossover_v2 import door as door_mod

    class _Open:
        def __init__(self) -> None:
            self.plan = None
            self.graph = SimpleNamespace(install=self._install)

        async def _install(self, *_args, **_kwargs) -> str:
            return "fingerprint-1"

    class _Door:
        async def __aenter__(self) -> _Open:
            return _Open()

        async def __aexit__(self, exc_type, _exc, _tb) -> bool:
            if exc_type is None and restore_error is not None:
                raise restore_error
            return False

    monkeypatch.setattr(door_mod, "measurement_door", lambda **_kw: _Door())


def test_a_clean_walk_answers_with_the_scalars_and_the_path(
    tmp_path, monkeypatch, capsys,
):
    """stdout IS the answer: the ask, one depth per row, and where the rows are.

    A grid's shoulders, trims and fingerprints stay in the banked rows — the
    document names the directory holding them and the verb that grades them,
    so a reader pulls depth only when a number surprises it.
    """
    _hardware_free_walk(monkeypatch, depth_db=-18.5)
    _install_door(monkeypatch)

    code = null_door.main([
        "--bundle-dir", str(tmp_path), "--delays", "0", "--fc-hz", "2000",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == null_door.EXIT_OK
    assert "status" not in payload
    assert payload["fc_hz"] == 2000.0
    assert payload["delays_us"] == [0.0]
    assert payload["out"] == str(tmp_path / null_door.NULL_RUNS_DIR)
    # `--polarity both` is the default: the pair at zero IS the polarity proof.
    assert [row["polarity"] for row in payload["rows"]] == [
        "in_phase", "inverted",
    ]
    assert all(row["depth_db"] == -18.5 for row in payload["rows"])
    assert all(
        (tmp_path / null_door.NULL_RUNS_DIR / row["row"]).exists()
        for row in payload["rows"]
    )
    assert payload["next"].startswith("jasper-round-views delay-confirm")


def test_a_row_that_could_not_be_read_refuses_and_carries_the_rows(
    tmp_path, monkeypatch, capsys,
):
    """A walk that banked only unreadable coordinates is a refusal, not a bank.

    The row's own reason decides — the module that could not read the depth
    owns the word — and the rows ride under ``detail`` so a caller knows which
    coordinates it still owes without listing the directory.
    """
    _hardware_free_walk(monkeypatch)
    _install_door(monkeypatch)

    def _no_depth(*_args, **_kwargs):
        raise null_door.NullDoorRefused(
            null_door.REFUSE_UNUSABLE_CAPTURE, "the capture failed quality gating"
        )

    monkeypatch.setattr(null_door, "_depth", _no_depth)

    code = null_door.main([
        "--bundle-dir", str(tmp_path), "--polarity", null_door.POLARITY_KEEP,
        "--delays", "0",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == null_door.EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == null_door.REFUSE_UNUSABLE_CAPTURE
    rows = payload["detail"]["rows"]
    assert [row["status"] for row in rows] == ["refused"]
    assert rows[0]["depth_db"] is None
    assert (tmp_path / null_door.NULL_RUNS_DIR / rows[0]["row"]).exists()


def test_a_coordinate_off_the_grid_exits_as_an_input_error(
    tmp_path, monkeypatch, capsys,
):
    """Exit 2 with the shared document, not a sentence on stderr and nothing else.

    The grid is the one the proposal was computed on, so a coordinate off it
    names a graph nobody modelled — an input fault a caller branches on.
    """
    _hardware_free_walk(monkeypatch)
    _install_door(monkeypatch)

    code = null_door.main([
        "--bundle-dir", str(tmp_path), "--delays=999999",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == null_door.EXIT_UNREADABLE
    assert payload["status"] == "unreadable"
    assert payload["reason"] == null_door.REFUSE_DELAY_OFF_GRID
    assert payload["detail"]


def test_a_box_that_cannot_answer_refuses_as_a_document(monkeypatch, capsys):
    """The conductor's refusal used to leave `main` as a traceback with an
    empty stdout, which no caller can tell from a crash."""
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused

    def _refuse():
        raise CrossoverV2Refused("this speaker has no active crossover to measure")

    monkeypatch.setattr(null_door, "_context", _refuse)
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)

    code = null_door.main([])

    payload = json.loads(capsys.readouterr().out)
    assert code == null_door.EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == null_door.REFUSE_BOX_NOT_READY
    assert payload["detail"]


def test_a_refused_run_writes_no_stimulus(tmp_path, monkeypatch):
    """B2: a refused run must leave the bundle exactly as it found it.

    The stimulus WAV has a FIXED name, so a run that published it before the
    interlock would overwrite the bytes a LIVE run's artifact sha256 is bound
    to — the live run's verified-WAV check then fails mid-session. The refusal
    is what makes this observable: if the write still happens when the door
    says no, it happens before the door is consulted.
    """
    from jasper.active_speaker.crossover_v2 import door as door_mod

    context = SimpleNamespace(
        fc_hz=FC_HZ,
        roles_bands=_roles(),
        driver_caps_dbfs={"woofer": 0.0, "tweeter": -65.0},
        driver_sweep_duration_limits_s={"woofer": 6.0, "tweeter": 2.0},
        session_volume_db=-30.0,
        preset=object(),
        topology=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="plughw:CARD=Loopback,DEV=0",
        safety_profile={},
        role_targets={},
        declared_sensitivities={},
    )
    monkeypatch.setattr(null_door, "_context", lambda: context)
    monkeypatch.setattr(null_door, "_level_trims", lambda _c: ({}, "none"))
    monkeypatch.setattr(null_door, "_resolve_mic", lambda: object())
    monkeypatch.setattr(null_door, "_protection_sections", lambda _c: None)

    def _refuse(**_kw):
        raise door_mod.MeasurementDoorRefused(
            door_mod.REFUSE_SESSION_LIVE, "a measurement session is running"
        )

    monkeypatch.setattr(door_mod, "measurement_door", _refuse)

    args = null_door.build_parser().parse_args(["--bundle-dir", str(tmp_path)])
    with pytest.raises(door_mod.MeasurementDoorRefused):
        asyncio.run(null_door._run(args))

    assert not (tmp_path / "null_programs").exists(), (
        "a refused run published the stimulus; the write is reachable before "
        "the interlock and would overwrite a live run's bytes"
    )


def test_a_give_back_failure_after_a_clean_walk_still_renders_json(
    tmp_path, monkeypatch, capsys,
):
    """The verified gap: every coordinate measured and banked, then the door's
    own exit could not put the entry graph back. ``_give_back`` raises that
    OUTSIDE the loop's own mid-run catch (door.py's ``finally``,
    ``body_error=None``) — this pins that the row already on disk is still
    reported rather than lost to a bare traceback.
    """
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError

    _hardware_free_walk(monkeypatch)
    _install_door(monkeypatch, restore_error=SessionGraphError(
        "the measurement graph was played but the entry graph could not be "
        "restored"
    ))

    code = null_door.main([
        "--bundle-dir", str(tmp_path),
        "--polarity", null_door.POLARITY_KEEP,
        "--delays", "0",
    ])

    assert code == null_door.EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == "interrupted"
    detail = payload["detail"]
    assert detail["reason"] == null_door.REFUSE_GRAPH_LOST
    assert len(detail["banked_row_ids"]) == 1
    assert (tmp_path / "null_runs" / detail["banked_row_ids"][0]).exists()


def test_rows_land_beside_the_takes(tmp_path):
    path = null_door._write_row(tmp_path / "null_runs", _row(depth_db=-9.0, span=_span()))
    assert path.parent == tmp_path / "null_runs"
    assert path.suffix == ".json"
