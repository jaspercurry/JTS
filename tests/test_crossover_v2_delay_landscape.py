# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compute-then-confirm: the delay landscape, and how a confirmation grades it."""

import math

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.delay_landscape import (
    MODEL_AGREEMENT_DB,
    REFUSAL_FC_OUTSIDE_OVERLAP,
    REFUSAL_SHOULDER_RUN_UP,
    VERDICT_MODEL_BROKE,
    VERDICT_NO_EVIDENCE,
    DelayLandscapeError,
    compute_landscape,
    confirmation_verdict,
    predicted_null_depth_db,
)
from jasper.active_speaker.delay_sweep import (
    ROBUST_NULL_DEPTH_DB,
    USABLE_NULL_DEPTH_DB,
    VERDICT_AXIS_LIMITED,
    VERDICT_ROBUST,
    VERDICT_WEAK,
    sweep_spec,
)

FC_HZ = 1800.0


def _lr4(freqs, *, highpass: bool, fc_hz: float = FC_HZ):
    """A Linkwitz-Riley 4th-order branch — Butterworth 2nd order, squared.

    The shape the reverse-null test assumes: an LR4 pair sums FLAT in phase and
    CANCELS at Fc when one branch is inverted, and away from Fc each branch owns
    its own shoulder. Two unshaped flat branches would cancel at every frequency
    equally, which is a null with no shoulders to measure it against — not what
    a crossover does.
    """
    s = 1j * (np.asarray(freqs, dtype=float) / fc_hz)
    butter2 = (s**2 if highpass else 1.0) / (s**2 + math.sqrt(2.0) * s + 1.0)
    return butter2**2


def _curve(
    role: str, *, arrival_us: float, freqs=None, gain_db: float = 0.0,
    fc_hz: float = FC_HZ,
):
    """One banked curve for a crossover branch arriving at `arrival_us`.

    Serialized exactly as `spatial.pose_curve_record` does, so the reader under
    test reconstructs it the way it reconstructs a real bank.
    """
    freqs = np.linspace(200.0, 12000.0, 512) if freqs is None else np.asarray(freqs)
    shape = _lr4(freqs, highpass=(role == "tweeter"), fc_hz=fc_hz)
    tf = (
        shape
        * 10.0 ** (gain_db / 20.0)
        * np.exp(-2j * np.pi * freqs * arrival_us * 1e-6)
    )
    return {
        "role": role,
        "band_hz": [float(freqs[0]), float(freqs[-1])],
        "freqs_hz": [float(hz) for hz in freqs],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(np.abs(tf))],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
    }


def _spec(seed_m: float = 0.0):
    return sweep_spec(
        crossover_fc_hz=FC_HZ,
        upper_role="tweeter",
        lower_role="woofer",
        signed_acoustic_path_difference_m=seed_m,
    )


def _landscape(offset_us: float, **kwargs):
    # Lower arrives `offset_us` LATER than upper, so delaying the upper branch
    # by exactly that much aligns them.
    return compute_landscape(
        _curve("woofer", arrival_us=offset_us),
        _curve("tweeter", arrival_us=0.0),
        spec=_spec(),
        inverted_role="tweeter",
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# PROPOSE — the landscape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("offset_us", [-200.0, -100.0, 0.0, 100.0, 200.0])
def test_the_computed_optimum_cancels_the_offset_in_the_curves(offset_us):
    landscape = _landscape(offset_us)
    assert landscape.best_coordinate_us == pytest.approx(offset_us)
    # Two identical branches, one inverted and perfectly timed, cancel hard.
    assert landscape.best_predicted_null_depth_db > ROBUST_NULL_DEPTH_DB


def test_the_landscape_covers_the_whole_fine_grid_not_the_audible_budget():
    spec = _spec()
    landscape = _landscape(100.0)
    expected = tuple(
        spec.fine_grid_coordinate(i)
        for i in range(spec.fine_grid_index_min, spec.fine_grid_index_max + 1)
    )
    assert landscape.coordinates_us == expected
    assert len(landscape.predicted_null_depth_db) == len(expected)


def test_the_confirmation_set_is_the_optimum_and_its_neighbours():
    landscape = _landscape(0.0)
    assert landscape.best_coordinate_us in landscape.confirmation_coordinates_us
    assert len(landscape.confirmation_coordinates_us) <= 3
    step = _spec().step_us
    for coordinate in landscape.confirmation_coordinates_us:
        assert abs(coordinate - landscape.best_coordinate_us) <= step + 1e-6


def test_the_null_falls_away_either_side_of_the_optimum():
    landscape = _landscape(0.0)
    depths = dict(zip(landscape.coordinates_us, landscape.predicted_null_depth_db))
    best = landscape.best_coordinate_us
    step = _spec().step_us
    for neighbour in (best - step, best + step):
        if neighbour in depths:
            assert depths[neighbour] < depths[best]


def test_inverting_the_other_branch_finds_the_same_optimum():
    # Which branch carries the sign flip is a wiring choice, not a timing one.
    flipped = compute_landscape(
        _curve("woofer", arrival_us=100.0),
        _curve("tweeter", arrival_us=0.0),
        spec=_spec(),
        inverted_role="woofer",
    )
    assert flipped.best_coordinate_us == pytest.approx(100.0)


def test_a_level_mismatch_between_branches_still_locates_the_optimum():
    # A trim error makes the null shallower; it must not move it.
    landscape = compute_landscape(
        _curve("woofer", arrival_us=100.0, gain_db=-4.0),
        _curve("tweeter", arrival_us=0.0),
        spec=_spec(),
        inverted_role="tweeter",
    )
    assert landscape.best_coordinate_us == pytest.approx(100.0)
    assert landscape.best_predicted_null_depth_db < ROBUST_NULL_DEPTH_DB


@pytest.mark.parametrize(
    "curve",
    [
        pytest.param({"role": "woofer"}, id="missing_arrays"),
        pytest.param(
            {"role": "w", "freqs_hz": [1.0, 2.0], "magnitude_db": [0.0],
             "phase_deg": [0.0, 0.0]},
            id="ragged_arrays",
        ),
    ],
)
def test_an_unreadable_curve_refuses_rather_than_inventing_a_landscape(curve):
    with pytest.raises(DelayLandscapeError):
        predicted_null_depth_db(
            curve, _curve("tweeter", arrival_us=0.0),
            crossover_fc_hz=FC_HZ, relative_delay_us=0.0,
            inverted_role="tweeter", lower_role="woofer", upper_role="tweeter",
        )


def _banded(
    role: str, band, *, fc_hz: float, grid_points: int = 1024,
    arrival_us: float = 0.0,
):
    """One curve banked on a wide shared grid but DECLARING a narrower sweep.

    The shape a real bank has: `lateral_pose_curve` resamples every curve onto
    one evidence grid and keeps the band it actually swept in `band_hz`.
    """

    curve = _curve(
        role,
        arrival_us=arrival_us,
        freqs=np.linspace(150.0, 20000.0, grid_points),
        fc_hz=fc_hz,
    )
    curve["band_hz"] = [float(band[0]), float(band[1])]
    return curve


# The reference speaker: a woofer declared to 4 kHz against a tweeter declared
# from 1.6 kHz, so the shared overlap is 1.32 octaves where the canonical
# shoulders want 2.00. Neither edge is tunable — both are declared driver caps.
JTS3_BANDS = ((150.0, 4000.0), (1600.0, 20000.0))


@pytest.mark.parametrize(
    ("bands", "fc_hz", "grid_points", "refusal", "used_hz", "clamped"),
    [
        # F-3: the blind run's own coordinate. The old rule wanted
        # `fc > 3200 and fc < 2000` here — empty for every fc, so PROPOSE
        # never ran once on this speaker.
        pytest.param(JTS3_BANDS, 2500.0, 1024, None, (1600.0, 4000.0),
                     (True, True), id="blind_run_f3_both_shoulders_clamped"),
        # Same speaker, a corner low enough that only the lower shoulder is
        # short: the upper one reaches its canonical 2*Fc untouched.
        pytest.param(JTS3_BANDS, 1800.0, 1024, None, (1600.0, 3600.0),
                     (True, False), id="jts3_lower_shoulder_clamped_only"),
        pytest.param(((150.0, 20000.0), (150.0, 20000.0)), 2500.0, 1024, None,
                     (1250.0, 5000.0), (False, False), id="wide_overlap_canonical"),
        # Genuinely unreadable: no null at Fc lives in a band that excludes it.
        pytest.param(JTS3_BANDS, 1000.0, 1024, REFUSAL_FC_OUTSIDE_OVERLAP, None,
                     None, id="fc_below_the_overlap"),
        # Brackets Fc, but the banked grid puts fewer than two points either
        # side of it — nothing to interpolate a shoulder from at any span.
        pytest.param(JTS3_BANDS, 2500.0, 8, REFUSAL_SHOULDER_RUN_UP, None, None,
                     id="overlap_too_coarse_for_a_shoulder"),
    ],
)
def test_shoulders_clamp_into_the_measured_overlap_instead_of_refusing(
    bands, fc_hz, grid_points, refusal, used_hz, clamped,
):
    """The canonical span is a preference; the overlap is the evidence.

    Where the two meet, the shoulders are the canonical span clamped into the
    overlap and the clamp is DISCLOSED. Refused is only what no span can read.
    """

    lower = _banded("woofer", bands[0], fc_hz=fc_hz, grid_points=grid_points,
                    arrival_us=100.0)
    upper = _banded("tweeter", bands[1], fc_hz=fc_hz, grid_points=grid_points)
    spec = sweep_spec(
        crossover_fc_hz=fc_hz, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )

    if refusal is not None:
        with pytest.raises(DelayLandscapeError) as refused:
            compute_landscape(lower, upper, spec=spec, inverted_role="tweeter")
        assert refused.value.refusal_reason == refusal
        # The numbers an operator needs travel as fields, not as prose.
        assert refused.value.detail["crossover_fc_hz"] == fc_hz
        assert refused.value.detail["overlap_lo_hz"] == max(bands[0][0], bands[1][0])
        assert refused.value.detail["overlap_hi_hz"] == min(bands[0][1], bands[1][1])
        return

    landscape = compute_landscape(lower, upper, spec=spec, inverted_role="tweeter")
    # Produced, not refused: every coordinate on the grid carries a depth, and
    # narrower shoulders still locate the fixture's 100 us offset — what the
    # model claims is WHERE the null is, and clamping does not move it.
    assert len(landscape.predicted_null_depth_db) == len(landscape.coordinates_us) > 0
    assert landscape.best_coordinate_us == pytest.approx(100.0, abs=spec.step_us)

    span = landscape.shoulders
    assert span.used_hz == pytest.approx(used_hz)
    assert (span.lower_clamped, span.upper_clamped) == clamped
    assert span.canonical_hz == pytest.approx((fc_hz / 2.0, fc_hz * 2.0))
    # And the disclosure reaches whoever reads the landscape, not just its caller.
    assert landscape.to_dict()["shoulders"] == span.to_dict()


# --------------------------------------------------------------------------- #
# DISPOSE — the acoustic confirmation
# --------------------------------------------------------------------------- #


def _measured(landscape, *, at_optimum, falloff=8.0):
    best = landscape.best_coordinate_us
    return {
        coordinate: (at_optimum if coordinate == best else at_optimum - falloff)
        for coordinate in landscape.confirmation_coordinates_us
    }


def test_a_deep_measured_null_where_the_model_put_it_grades_robust():
    landscape = _landscape(100.0)
    verdict = confirmation_verdict(landscape, _measured(landscape, at_optimum=26.0))
    assert verdict["verdict"] == VERDICT_ROBUST
    assert verdict["model_agrees"] is True
    assert verdict["prescribable_delay_us"] == pytest.approx(100.0)


def test_agreement_at_a_shallow_null_grades_weak_and_still_prescribes():
    # Between the usable floor and the robustness bar, located where the model
    # put it: a real answer, handed over with its shallowness stated.
    landscape = _landscape(100.0)
    verdict = confirmation_verdict(landscape, _measured(landscape, at_optimum=17.0))
    assert verdict["verdict"] == VERDICT_WEAK
    assert verdict["model_agrees"] is True
    assert verdict["prescribable_delay_us"] is not None


def test_a_measured_null_far_shallower_than_an_ideal_model_is_not_a_break():
    # The model's cancellation is near-perfect; the room's floors on noise.
    # That gap is physics, not disagreement -- only WHERE the null sits is a
    # claim the model makes.
    landscape = _landscape(100.0)
    verdict = confirmation_verdict(landscape, _measured(landscape, at_optimum=26.0))
    assert landscape.best_predicted_null_depth_db > 35.0
    assert verdict["measured_minus_predicted_db"] < -2 * MODEL_AGREEMENT_DB
    assert verdict["model_agrees"] is True
    assert verdict["verdict"] == VERDICT_ROBUST


def test_a_promised_null_the_room_did_not_produce_is_a_model_break():
    landscape = _landscape(100.0)
    # The model promised a deep null; the acoustic sum barely dipped.
    verdict = confirmation_verdict(landscape, _measured(landscape, at_optimum=4.0))
    assert verdict["verdict"] == VERDICT_MODEL_BROKE
    assert verdict["model_agrees"] is False
    # No delay is prescribed on the strength of a computation the room refused.
    assert verdict["prescribable_delay_us"] is None
    assert verdict["measured_minus_predicted_db"] < -MODEL_AGREEMENT_DB


def test_the_measured_minus_computed_delta_is_always_banked():
    landscape = _landscape(0.0)
    for measured_db in (2.0, 12.0, 30.0):
        verdict = confirmation_verdict(
            landscape, _measured(landscape, at_optimum=measured_db)
        )
        assert verdict["measured_null_depth_db"] == pytest.approx(measured_db)
        assert verdict["measured_minus_predicted_db"] == pytest.approx(
            measured_db - landscape.best_predicted_null_depth_db
        )


def test_a_deeper_null_at_a_neighbour_than_at_the_optimum_is_a_model_break():
    landscape = _landscape(0.0)
    best = landscape.best_coordinate_us
    measured = {c: 6.0 for c in landscape.confirmation_coordinates_us}
    neighbour = next(c for c in landscape.confirmation_coordinates_us if c != best)
    measured[neighbour] = 30.0  # the real null is not where the model put it
    verdict = confirmation_verdict(landscape, measured)
    assert verdict["verdict"] == VERDICT_MODEL_BROKE
    assert verdict["prescribable_delay_us"] is None


def test_agreement_on_a_null_nobody_can_use_reads_axis_limited():
    # The branches are 6 dB apart, so the complex sum CANNOT cancel deeply and
    # the model itself predicts an unusable null. That is the only shape this
    # verdict is reachable on: where the model promises a usable null and the
    # room refuses, `promised_unkept` fires and the answer is a model break.
    landscape = compute_landscape(
        _curve("woofer", arrival_us=0.0, gain_db=-6.0),
        _curve("tweeter", arrival_us=0.0),
        spec=_spec(),
        inverted_role="tweeter",
    )
    assert landscape.best_predicted_null_depth_db < USABLE_NULL_DEPTH_DB
    verdict = confirmation_verdict(
        landscape,
        {c: 3.0 for c in landscape.confirmation_coordinates_us},
    )
    assert verdict["model_agrees"] is True
    assert verdict["verdict"] == VERDICT_AXIS_LIMITED
    assert verdict["prescribable_delay_us"] is None


def test_a_confirmation_that_missed_the_optimum_says_so():
    landscape = _landscape(100.0)
    verdict = confirmation_verdict(landscape, {landscape.best_coordinate_us + 1e4: 30.0})
    assert verdict["verdict"] == VERDICT_NO_EVIDENCE
    assert verdict["measured_null_depth_db"] is None
    assert verdict["prescribable_delay_us"] is None


def test_the_landscape_serializes_every_coordinate_it_scored():
    landscape = _landscape(100.0)
    payload = landscape.to_dict()
    assert payload["kind"] == "jts_inter_driver_delay_landscape"
    assert len(payload["coordinates_us"]) == len(payload["predicted_null_depth_db"])
    assert payload["best_coordinate_us"] == pytest.approx(100.0)
    assert math.isfinite(payload["best_predicted_null_depth_db"])


# --------------------------------------------------------------------------- #
# the spec's delay pair
# --------------------------------------------------------------------------- #


def test_a_spec_states_both_halves_of_its_delay_or_neither():
    from jasper.active_speaker.crossover_v2.measure_spec import (
        MeasureSpec,
        measurement_delays_for,
    )

    paired = MeasureSpec(kind="baseline", delayed_role="tweeter", delay_us=250.0)
    assert measurement_delays_for(paired) == {"tweeter": 250.0}
    assert measurement_delays_for(MeasureSpec(kind="baseline")) == {}

    # One half without the other is a spec that means two things.
    for half in ({"delayed_role": "tweeter"}, {"delay_us": 250.0}):
        with pytest.raises(ValueError):
            MeasureSpec(kind="baseline", **half)


def test_the_spec_states_WHETHER_to_level_match_and_the_session_states_by_how_much():
    """The values never travel on the spec. ``level_trims_for`` is the one
    translation from the boolean into the graph's vocabulary, and it applies
    what the SESSION resolved on the box — so a spec cannot carry one
    cabinet's level match onto another's measurement."""
    from jasper.active_speaker.crossover_v2.measure_spec import (
        MeasureSpec,
        level_trims_for,
    )

    resolved = {"tweeter": -9.5}
    matched = MeasureSpec(kind="baseline", level_matched=True)
    assert level_trims_for(matched, resolved) == resolved
    # Asking for none applies none, however much the session is holding.
    assert level_trims_for(MeasureSpec(kind="baseline"), resolved) == {}
    # And asking for one the session has nothing for is empty here, not a
    # raise: that pairing is refused at open, where an operator can act on it.
    assert level_trims_for(matched, None) == {}


def test_a_delay_beyond_the_dsp_ceiling_is_refused_at_the_spec():
    from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    for bad in (-1.0, MAX_DSP_DELAY_US + 1.0):
        with pytest.raises(ValueError):
            MeasureSpec(kind="baseline", delayed_role="tweeter", delay_us=bad)


def test_the_verify_stage_graph_refuses_a_delay_instead_of_dropping_it():
    """Stage 2 measures through the APPLIED graph and has no per-driver branch
    to delay. Silently ignoring the coordinate would bank a record naming a
    delay it never played — the S12 lie this slot already refuses for polarity.
    """
    import asyncio

    from jasper.active_speaker.crossover_v2.composition import NoRoutedPhasesGraph

    graph = NoRoutedPhasesGraph()
    assert asyncio.run(graph.install()) == ""
    with pytest.raises(ValueError):
        asyncio.run(graph.install((), {"tweeter": 250.0}))
    with pytest.raises(ValueError):
        asyncio.run(graph.install(("tweeter",)))


def test_the_grid_a_curve_was_banked_on_does_not_change_the_answer():
    """The two drivers sweep their own bands, so the sum is taken after a
    resample — and a real capture carries the whole flight time to the
    microphone (~3 ms at 1 m), so its phasor turns once every ~330 Hz.

    On a realistic null — one floored by a level mismatch rather than a perfect
    analytic cancellation — neither the coordinate nor the depth may depend on
    how densely the curve happened to be sampled.
    """
    flight_us = 2900.0
    coarse = np.linspace(200.0, 12000.0, 97)   # ~122 Hz apart: a third of a turn
    fine = np.linspace(200.0, 12000.0, 1024)

    def _landscape(woofer_grid):
        return compute_landscape(
            _curve("woofer", arrival_us=flight_us + 100.0, freqs=woofer_grid,
                   gain_db=-4.0),
            _curve("tweeter", arrival_us=flight_us, freqs=fine),
            spec=_spec(),
            inverted_role="tweeter",
        )

    sparse, dense = _landscape(coarse), _landscape(fine)
    assert sparse.best_coordinate_us == dense.best_coordinate_us == pytest.approx(100.0)
    assert sparse.best_predicted_null_depth_db == pytest.approx(
        dense.best_predicted_null_depth_db, abs=1.0
    )


# --------------------------------------------------------------------------- #
# the staging thread — R-1's DISPOSE half, operator to graph
# --------------------------------------------------------------------------- #


def _walk(**kwargs):
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop

    return AngleCaptureRequest(
        stops=(AngleStop(angle_deg=0, regime="summed"),), **kwargs
    )


def test_a_staged_walk_carries_the_confirmation_coordinate():
    request = _walk(delayed_role="tweeter", delay_us=250.0)
    assert (request.delayed_role, request.delay_us) == ("tweeter", 250.0)
    # Absent by default, so an ordinary walk stages exactly what it always did.
    assert (_walk().delayed_role, _walk().delay_us) == ("", 0.0)


def test_a_staged_walk_carries_whether_to_level_match():
    assert _walk(level_matched=True).level_matched is True
    assert _walk().level_matched is False


def test_the_coordinate_survives_the_spool_round_trip(tmp_path):
    from jasper.active_speaker import angle_capture_spool as spool

    spool.set_angle_request_spool_path_for_tests(tmp_path / "walk.json")
    try:
        spool.stage_angle_request(_walk(delayed_role="tweeter", delay_us=250.0))
        taken = spool.take_staged_angle_request()
    finally:
        spool.set_angle_request_spool_path_for_tests(None)

    assert taken is not None
    assert (taken.delayed_role, taken.delay_us) == ("tweeter", 250.0)


def test_the_level_match_survives_the_spool_round_trip(tmp_path):
    from jasper.active_speaker import angle_capture_spool as spool

    spool.set_angle_request_spool_path_for_tests(tmp_path / "walk.json")
    try:
        spool.stage_angle_request(_walk(level_matched=True))
        taken = spool.take_staged_angle_request()
    finally:
        spool.set_angle_request_spool_path_for_tests(None)

    assert taken is not None and taken.level_matched is True


def test_a_document_spooled_before_the_coordinate_existed_still_reads(tmp_path):
    """The pair is ADDITIVE: a walk spooled by an older build reads back as an
    undelayed one rather than refusing."""
    import json

    from jasper.active_speaker import angle_capture_spool as spool

    path = tmp_path / "walk.json"
    spool.set_angle_request_spool_path_for_tests(path)
    try:
        spool.stage_angle_request(_walk())
        doc = json.loads(path.read_text())
        doc.pop("delayed_role", None)
        doc.pop("delay_us", None)
        path.write_text(json.dumps(doc))
        taken = spool.take_staged_angle_request()
    finally:
        spool.set_angle_request_spool_path_for_tests(None)

    assert taken is not None
    assert (taken.delayed_role, taken.delay_us) == ("", 0.0)


def test_a_curve_passed_as_the_wrong_branch_is_refused():
    """The two curves reach the reader positionally. Swapped, the model would
    delay and invert the wrong branches and say nothing about it — so the
    banked ``role`` is checked against the slot it was passed in."""
    with pytest.raises(DelayLandscapeError):
        compute_landscape(
            _curve("tweeter", arrival_us=0.0),   # tweeter in the lower slot
            _curve("woofer", arrival_us=100.0),
            spec=_spec(),
            inverted_role="tweeter",
        )


def test_the_shared_band_comes_from_each_curve_s_declared_sweep_not_its_grid():
    """Real banks resample every curve onto ONE evidence grid and keep the band
    actually swept in ``band_hz``. Read from the grid, the overlap would be the
    same for both drivers, the shoulders would read as canonical however narrow
    the real sweep was, and the sum would include bins neither driver was swept
    over."""
    grid = np.linspace(200.0, 12000.0, 512)
    woofer = _curve("woofer", arrival_us=100.0, freqs=grid)
    tweeter = _curve("tweeter", arrival_us=0.0, freqs=grid)
    # Both on one grid, but the tweeter was only swept from above Fc/2 upward —
    # so the lower shoulder can only be read as far down as the tweeter went.
    tweeter["band_hz"] = [FC_HZ * 0.75, 12000.0]

    span = compute_landscape(
        woofer, tweeter, spec=_spec(), inverted_role="tweeter",
    ).shoulders
    assert span.used_hz[0] == pytest.approx(FC_HZ * 0.75)
    assert span.lower_clamped is True
    # Read from the grid instead, the overlap would start at 200 Hz and nothing
    # would be clamped at all.
    assert span.overlap_hz[0] == pytest.approx(FC_HZ * 0.75)
