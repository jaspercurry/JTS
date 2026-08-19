# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Rungs 1 and 2 of the crossover forward model's validation ladder.

Pure math and fixtures; nothing here plays a tone or opens a session.

**Rung 1 — analytic.** The Linkwitz-Riley property the model has or is wrong
(an aligned in-phase pair sums to unity at its own corner), the polarity and
delay behaviours, and a MUTATION CONTROL: perturbing each axis has to move the
prediction, because a test that passes against a stubbed model guards nothing.

**Rung 2 — round trip against the shipped composition.** The strongest
available statement that this module and ``predicted_branch_sum`` are the same
arithmetic: at zero residual delay they agree BIT FOR BIT, and with a delay the
tolerance is a measured number rather than an asserted one.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    CrossoverSection, crossover_response_complex,
)
from jasper.active_speaker.crossover_v2.candidate_space import (
    CANDIDATE_REFUSAL_REASONS,
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_OUTSIDE_SEARCH_BAND,
    REJECT_DELAY_NOT_REALIZABLE,
    REJECT_DELAY_OUTSIDE_WINDOW,
    REJECT_GAIN_OUTSIDE_RANGE,
    REJECT_INVALID_POLARITY,
    REJECT_UNDECLARED_ROLE,
    REJECT_UNSUPPORTED_ORDER,
    CandidateBounds,
    XoverCandidate,
    bounds_from_declarations,
    refusal_for,
    residual_delay_us,
)
from jasper.active_speaker.crossover_v2.forward_model import (
    branch_operator, driver_plants, predict_sum,
)
from jasper.audio_measurement.program_analysis import (
    CONFIGURED_PATH_PROTECTION_FLOOR_DB,
    ConfiguredPathConditioningError,
    predicted_branch_sum,
    summed_model_residual_delay_us,
)

WOOFER = "woofer"
TWEETER = "tweeter"
FC_HZ = 2000.0


def _grid() -> np.ndarray:
    """1/48-octave, 20 Hz-20 kHz, with Fc unioned in.

    Fc has to be an exact grid point or the deep-null assertions read the
    shoulder of a null instead of its floor: an out-of-phase LR pair is
    infinitely deep AT the corner and merely tens of dB down a bin away.
    """
    return np.unique(np.append(np.geomspace(20.0, 20_000.0, 480), FC_HZ))


class _Measurement:
    """The duck-typed per-driver capture ``driver_plants`` consumes."""

    def __init__(
        self,
        role: str,
        freqs_hz: np.ndarray,
        complex_tf: np.ndarray,
        *,
        angle_deg: float = 0.0,
        band_hz: tuple[float, float] = (20.0, 20_000.0),
    ) -> None:
        self.role = role
        self.angle_deg = angle_deg
        self.freqs_hz = freqs_hz
        self.complex_tf = complex_tf
        self.band_hz = band_hz


def _ideal_plants(
    freqs_hz: np.ndarray, *, angles_deg: tuple[float, ...] = (0.0,),
) -> tuple:
    """Two perfectly flat, perfectly unprotected drivers.

    Deliberately featureless: rung 1 asks what the MODEL does, so the drivers
    must contribute nothing of their own. A real driver's response is rung 4's
    business.
    """
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)
    return driver_plants(
        [
            _Measurement(role, freqs_hz, ones.copy(), angle_deg=angle)
            for angle in angles_deg
            for role in (WOOFER, TWEETER)
        ],
        protection_by_role={},
    )


def _textured_plants(freqs_hz: np.ndarray) -> tuple:
    """Two drivers with responses of their OWN, so Fc and order are visible.

    A cone rolling off above its breakup and a dome rolling off below its
    resonance — the shapes that make a corner choice a real choice. Not a
    model of any particular driver; just enough departure from flat that the
    crossover has something to interact with.
    """
    woofer = 10.0 ** (-((np.log2(freqs_hz / 900.0)).clip(0.0) ** 2) * 1.5 / 20.0)
    tweeter = 10.0 ** (-((np.log2(3000.0 / freqs_hz)).clip(0.0) ** 2) * 2.0 / 20.0)
    return driver_plants(
        [
            _Measurement(WOOFER, freqs_hz, woofer.astype(np.complex128)),
            _Measurement(TWEETER, freqs_hz, tweeter.astype(np.complex128)),
        ],
        protection_by_role={},
    )


def _two_way(
    *,
    fc_hz: float = FC_HZ,
    order: int = 4,
    tweeter_polarity: int = 1,
    tweeter_delay_us: float = 0.0,
    gain_db_by_role: dict[str, float] | None = None,
) -> XoverCandidate:
    return XoverCandidate(
        sections_by_role={
            WOOFER: (CrossoverSection(fc_hz, order, False),),
            TWEETER: (CrossoverSection(fc_hz, order, True),),
        },
        polarity_by_role={WOOFER: 1, TWEETER: tweeter_polarity},
        delay_us_by_role={WOOFER: 0.0, TWEETER: tweeter_delay_us},
        gain_db_by_role=gain_db_by_role or {WOOFER: 0.0, TWEETER: 0.0},
    )


def _db(spectrum: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(spectrum), 1e-12))


# --- rung 1: the Linkwitz-Riley property ------------------------------------


@pytest.mark.parametrize(
    "order, summing_polarity",
    # LR2's summing polarity is INVERTED and that is textbook, not a defect:
    # an even-multiple-of-90-degree pair sums in phase (LR4, LR8) while LR2's
    # two branches are 180 degrees apart at every frequency, so one has to be
    # flipped to sum flat. The parameterization carries the physics rather
    # than hiding it behind a single "in phase" claim that is false for a
    # third of the supported orders.
    [(2, -1), (4, 1), (8, 1)],
)
def test_an_aligned_lr_pair_sums_to_unity_at_its_own_corner(order, summing_polarity):
    """The defining LR property: the model has it or the model is wrong.

    A Linkwitz-Riley pair is two cascaded Butterworth passes precisely so both
    branches are -6 dB at Fc and sum back to unity at the summing polarity for
    that order. If the model got the crossover, the polarity or the summation
    wrong, this is the number that moves.

    Checked at all three supported orders, because the odd-Butterworth path
    (LR2 runs two FIRST-ORDER passes, which no biquad spells) is a different
    code path in ``crossover_response_complex`` from LR4 and LR8.
    """
    freqs_hz = _grid()
    candidate = _two_way(order=order, tweeter_polarity=summing_polarity)
    magnitude_db = _db(predict_sum(candidate, _ideal_plants(freqs_hz))[0.0])

    at_fc = int(np.argmin(np.abs(freqs_hz - FC_HZ)))
    assert abs(magnitude_db[at_fc]) <= 0.05, (
        f"LR{order} at Fc summed to {magnitude_db[at_fc]:.4f} dB"
    )

    # Flat well inside each branch's own passband, where the other contributes
    # nothing: an LR sum that is right at the corner and wrong an octave out
    # would be a coincidence rather than a model.
    passband = (freqs_hz <= FC_HZ / 4.0) | (freqs_hz >= FC_HZ * 4.0)
    assert np.max(np.abs(magnitude_db[passband])) <= 0.05


def test_flipping_tweeter_polarity_nulls_at_fc_and_flipping_back_is_exact():
    """Polarity is a sign, and a sign is exactly invertible.

    Two claims in one test because they are the same claim: the flip has to
    produce the deep null an out-of-phase LR pair is famous for, AND flipping
    twice has to return the ORIGINAL array bit for bit. An implementation that
    reached the null by any route other than negating the branch would pass the
    first half and fail the second.
    """
    freqs_hz = _grid()
    plants = _ideal_plants(freqs_hz)

    kept = predict_sum(_two_way(tweeter_polarity=1), plants)[0.0]
    flipped = predict_sum(_two_way(tweeter_polarity=-1), plants)[0.0]

    at_fc = int(np.argmin(np.abs(freqs_hz - FC_HZ)))
    assert _db(flipped)[at_fc] < -60.0, (
        f"an out-of-phase LR pair should null at Fc, saw {_db(flipped)[at_fc]:.2f} dB"
    )
    assert np.array_equal(
        predict_sum(_two_way(tweeter_polarity=1), plants)[0.0], kept
    )
    # The flip IS a negation of that branch, so the two sums differ by exactly
    # twice the tweeter's contribution — asserted on the operator rather than
    # the sum so the claim is about polarity and not about the drivers.
    operator = branch_operator(_two_way(), TWEETER, freqs_hz)
    negated = branch_operator(_two_way(tweeter_polarity=-1), TWEETER, freqs_hz)
    assert np.array_equal(negated, -operator)


def test_a_delay_is_a_linear_phase_ramp_and_nothing_else():
    """``e^(-j*2*pi*f*tau)``: unity magnitude, phase linear in frequency.

    A delay must not change what a branch does to MAGNITUDE — if it did, the
    model would be attributing level to a timing choice. The slope is checked
    against the delay that produced it, in the sign convention of record:
    a POSITIVE residual delay retards the branch, so its phase goes NEGATIVE
    with frequency.
    """
    freqs_hz = _grid()
    tau_us = 250.0
    undelayed = branch_operator(_two_way(), TWEETER, freqs_hz)
    delayed = branch_operator(
        _two_way(tweeter_delay_us=tau_us), TWEETER, freqs_hz
    )

    np.testing.assert_allclose(np.abs(delayed), np.abs(undelayed), rtol=0, atol=1e-15)

    ratio = delayed / undelayed
    phase = np.unwrap(np.angle(ratio))
    slope, intercept = np.polyfit(freqs_hz, phase, 1)
    assert intercept == pytest.approx(0.0, abs=1e-9)
    assert slope == pytest.approx(-2.0 * np.pi * tau_us * 1e-6, rel=1e-9)


# --- rung 2: the same arithmetic as the shipped composition -----------------


def test_at_zero_residual_delay_the_model_is_predicted_branch_sum_bit_for_bit():
    """The strongest statement available without hardware, and it is exact.

    ``predicted_branch_sum`` is what VERIFY already grades against. With a
    unity candidate — no crossover, no correction, no protection — this model
    reduces to the same expression, and "the same arithmetic" deserves an exact
    number rather than a tolerance. Bit-for-bit is achievable here because the
    only factors are a real gain and an exact +/-1, both of which multiply
    without rounding.
    """
    rng = np.random.default_rng(20260819)
    freqs_hz = _grid()
    woofer = rng.normal(size=freqs_hz.size) + 1j * rng.normal(size=freqs_hz.size)
    tweeter = rng.normal(size=freqs_hz.size) + 1j * rng.normal(size=freqs_hz.size)

    for sign in (1, -1):
        for trim_w_db, trim_t_db in ((0.0, 0.0), (-3.5, -1.25)):
            plants = driver_plants(
                [
                    _Measurement(WOOFER, freqs_hz, woofer.copy()),
                    _Measurement(TWEETER, freqs_hz, tweeter.copy()),
                ],
                protection_by_role={},
            )
            candidate = XoverCandidate(
                sections_by_role={},
                polarity_by_role={WOOFER: 1, TWEETER: sign},
                delay_us_by_role={WOOFER: 0.0, TWEETER: 0.0},
                gain_db_by_role={WOOFER: trim_w_db, TWEETER: trim_t_db},
            )
            mine = predict_sum(candidate, plants)[0.0]
            theirs = predicted_branch_sum(
                woofer, tweeter, trim_w_db, trim_t_db, sign,
                freqs_hz=freqs_hz, residual_delay_us=0.0,
            )
            assert np.array_equal(mine, theirs), (
                f"sign={sign} trims=({trim_w_db}, {trim_t_db}) diverged"
            )


def test_with_a_residual_delay_the_two_agree_to_a_stated_numerical_bound():
    """Not bit-for-bit, and the honest thing is to say why and how close.

    ``predicted_branch_sum`` associates as ``sign * ((T * g) * e)``; this model
    builds a candidate-only operator — which is the entire reason a search can
    reuse it across angles — so it associates as ``T * ((sign * g) * e)``.
    Floating-point multiplication is not associative, so the two differ by
    rounding. The bound below is MEASURED, not chosen: it is the observed
    worst case with a decade of headroom, so a real regression moves it and
    ordinary rounding does not.
    """
    rng = np.random.default_rng(20260819)
    freqs_hz = _grid()
    woofer = rng.normal(size=freqs_hz.size) + 1j * rng.normal(size=freqs_hz.size)
    tweeter = rng.normal(size=freqs_hz.size) + 1j * rng.normal(size=freqs_hz.size)
    plants = driver_plants(
        [
            _Measurement(WOOFER, freqs_hz, woofer.copy()),
            _Measurement(TWEETER, freqs_hz, tweeter.copy()),
        ],
        protection_by_role={},
    )

    worst = 0.0
    for tau_us in (-550.0, -350.0, -250.0, 12.2, 96.0, 405.7):
        candidate = XoverCandidate(
            sections_by_role={},
            polarity_by_role={WOOFER: 1, TWEETER: 1},
            delay_us_by_role={WOOFER: 0.0, TWEETER: tau_us},
            gain_db_by_role={WOOFER: 0.0, TWEETER: 0.0},
        )
        mine = predict_sum(candidate, plants)[0.0]
        theirs = predicted_branch_sum(
            woofer, tweeter, 0.0, 0.0, 1,
            freqs_hz=freqs_hz, residual_delay_us=tau_us,
        )
        worst = max(worst, float(np.max(np.abs(mine - theirs))))
    assert worst <= 1e-14, f"worst absolute divergence {worst:.3e}"


def test_the_residual_delay_helper_is_the_one_owner_not_a_second_derivation():
    """``candidate_space.residual_delay_us`` must BE the kernel's function.

    The fix-2 failure mode is what happens when a caller derives the residual
    itself, so the re-export exists to make the right thing the easy thing.
    A re-export that had drifted into its own arithmetic would be worse than no
    re-export at all.
    """
    for anchor_us, applied_us in ((None, 300.0), (-405.7, -55.7), (0.0, 0.0)):
        assert residual_delay_us(anchor_us, applied_us) == (
            summed_model_residual_delay_us(anchor_us, applied_us)
        )
    # At the bare anchor the residual is exactly zero — the property that makes
    # the anchor the frame the measured pair is already in.
    assert residual_delay_us(-405.7, -405.7) == 0.0


# --- rung 1: the mutation control -------------------------------------------


@pytest.mark.parametrize(
    "label, mutated",
    [
        ("fc +1%", _two_way(fc_hz=FC_HZ * 1.01)),
        ("order 4 -> 8", _two_way(order=8)),
        ("tau +10 us", _two_way(tweeter_delay_us=10.0)),
        ("gain -0.1 dB", _two_way(gain_db_by_role={WOOFER: 0.0, TWEETER: -0.1})),
        ("polarity flip", _two_way(tweeter_polarity=-1)),
    ],
)
def test_perturbing_any_axis_moves_the_prediction(label, mutated):
    """The control every model test needs, and the reason is not theoretical.

    A forward model that ignored one of its inputs would pass every assertion
    above: the LR property, the polarity flip and the delay ramp are each
    checked through the axis they exercise, and none of them would notice a
    candidate field that was silently dropped on the floor. This asserts each
    of the five axes reaches the prediction.

    1 % of Fc, 10 us and 0.1 dB are deliberately SMALL — the smallest
    perturbation a search would ever propose. A control that only fired on a
    large change would still pass against a model with a rounded-away
    parameter.

    **On TEXTURED drivers, and the reason is a finding rather than a fixture
    convenience.** On the ideal flat pair the rest of this file uses, moving Fc
    or changing order does not move the sum AT ALL — that is exactly what the
    Linkwitz-Riley property guarantees, so a perfect speaker is one where the
    corner choice is free. The Fc and order axes are only observable through
    the drivers' OWN departures from flat, which is the whole reason the corner
    is chosen by measurement rather than by arithmetic. A mutation control run
    on flat drivers would have failed here and looked like a broken model.
    """
    freqs_hz = _grid()
    plants = _textured_plants(freqs_hz)
    baseline = predict_sum(_two_way(), plants)[0.0]
    moved = predict_sum(mutated, plants)[0.0]
    delta_db = float(np.max(np.abs(_db(moved) - _db(baseline))))
    assert delta_db > 1e-6, f"{label} left the prediction unchanged"


def test_on_ideal_drivers_the_corner_choice_is_free_and_the_model_says_so():
    """The companion claim the mutation control leans on, asserted directly.

    Stated positively so it is a pinned property rather than an aside in
    another test's docstring: with two perfectly flat drivers an LR pair sums
    to unity at ANY corner and ANY supported order, so Fc is unobservable.
    Every real Fc argument is therefore an argument about the DRIVERS, and a
    model that showed a preference here would be inventing one.
    """
    freqs_hz = _grid()
    plants = _ideal_plants(freqs_hz)
    reference = _db(predict_sum(_two_way(), plants)[0.0])
    for fc_hz, order in ((1200.0, 4), (2000.0, 8), (3500.0, 4)):
        moved = _db(predict_sum(_two_way(fc_hz=fc_hz, order=order), plants)[0.0])
        assert np.max(np.abs(moved - reference)) <= 0.05


# --- the plant: what is divided out, and what is refused --------------------


def test_the_plant_is_the_measurement_with_the_emitted_protection_divided_out():
    """``plant = M / P``, computed once because it cannot depend on a candidate.

    Also the claim that makes the offline search possible at all: the plant is
    a property of the CAPTURE, so two different candidates read the same plant.
    """
    freqs_hz = _grid()
    rng = np.random.default_rng(7)
    measured = rng.normal(size=freqs_hz.size) + 1j * rng.normal(size=freqs_hz.size)
    protection = crossover_response_complex(
        freqs_hz, (CrossoverSection(120.0, 4, True),)
    )

    (plant,) = driver_plants(
        [_Measurement(TWEETER, freqs_hz, measured, band_hz=(200.0, 20_000.0))],
        protection_by_role={TWEETER: lambda f: crossover_response_complex(
            f, (CrossoverSection(120.0, 4, True),)
        )},
    )
    np.testing.assert_allclose(
        plant.plant, measured / protection, rtol=1e-12, atol=0.0
    )
    assert plant.role == TWEETER
    assert plant.band_hz == (200.0, 20_000.0)


def test_a_role_with_no_emitted_protection_runs_full_range_so_p_is_unity():
    """No region means no protection filter in the emitted graph.

    The same reading ``branch_chain.sections_by_role`` takes: a role without a
    region runs FULL RANGE, so there is nothing to divide out. Inventing a
    protection filter the graph does not contain would put a phase rotation
    into the plant that the speaker never applies.
    """
    freqs_hz = _grid()
    measured = np.exp(1j * freqs_hz / 1000.0)
    (plant,) = driver_plants(
        [_Measurement(WOOFER, freqs_hz, measured)], protection_by_role={},
    )
    assert np.array_equal(plant.plant, measured)
    assert bool(np.all(plant.trusted))


def test_protection_below_the_shared_floor_refuses_on_the_required_band():
    """Refuse, never saturate — and with the flag that names the lever.

    ``|P| < floor`` does not involve the crossover, so no Fc can clear it; the
    ``protection_floor`` flag is what routes a household to the right copy.
    The floor itself is the kernel's constant, not a number this module chose,
    which is the whole point of naming it.
    """
    freqs_hz = _grid()
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)
    deep = 10.0 ** ((CONFIGURED_PATH_PROTECTION_FLOOR_DB - 1.0) / 20.0)

    with pytest.raises(ConfiguredPathConditioningError) as excinfo:
        driver_plants(
            [_Measurement(TWEETER, freqs_hz, ones)],
            protection_by_role={TWEETER: lambda f: np.full(f.shape, deep, complex)},
            required_band_hz_by_role={TWEETER: (1000.0, 8000.0)},
        )
    assert excinfo.value.protection_floor is True
    assert "-12" in str(excinfo.value)


def test_a_crossover_shaped_protection_does_not_refuse_the_whole_sweep():
    """The gap the banked armrun found: 6 of 6 arms refused, wrongly.

    A sweep necessarily runs past the corner, so when ``P`` contains a
    crossover it is far below the floor inside the DRIVEN band by construction
    — an LR4 low-pass at 1648.7 Hz is about -30 dB at 4 kHz, inside a woofer's
    150-4000 Hz sweep. Checking the floor over the driven band therefore
    refused every real two-way capture, including the incumbent round trip
    where the de-embedding provably cancels.

    The required band is what the kernel intersects for exactly this reason,
    and it is the caller's to declare because it is a property of the
    candidate. Undeclared refuses nothing and the mask carries the answer.
    """
    freqs_hz = _grid()
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)

    def woofer_lowpass(f: np.ndarray) -> np.ndarray:
        return crossover_response_complex(f, (CrossoverSection(1648.7, 4, False),))

    measurement = _Measurement(
        WOOFER, freqs_hz, ones, band_hz=(150.0, 4000.0),
    )
    # The floor is genuinely violated deep in the sweep — this is not a fixture
    # that dodges the question.
    assert np.min(np.abs(woofer_lowpass(np.array([4000.0])))) < 10.0 ** (
        CONFIGURED_PATH_PROTECTION_FLOOR_DB / 20.0
    )

    (plant,) = driver_plants(
        [measurement], protection_by_role={WOOFER: woofer_lowpass},
    )
    assert not bool(np.all(plant.trusted)), "the mask must still record it"

    # Declaring a band the branch actually owns is honest and passes...
    (narrow,) = driver_plants(
        [measurement],
        protection_by_role={WOOFER: woofer_lowpass},
        required_band_hz_by_role={WOOFER: (150.0, 1200.0)},
    )
    assert bool(np.all(narrow.trusted[narrow.driven & (freqs_hz <= 1200.0)]))

    # ...and declaring one that reaches into the stopband still refuses.
    with pytest.raises(ConfiguredPathConditioningError) as excinfo:
        driver_plants(
            [measurement],
            protection_by_role={WOOFER: woofer_lowpass},
            required_band_hz_by_role={WOOFER: (150.0, 4000.0)},
        )
    assert excinfo.value.protection_floor is True


def test_an_untrusted_bin_outside_the_driven_band_is_marked_not_refused():
    """The band is where there was stimulus; outside it there is no claim.

    A protective high-pass is BELOW its floor far under its own corner by
    construction, which is not ill-conditioning — it is a region the excitation
    never drove. Refusing there would refuse every real tweeter capture.
    """
    freqs_hz = _grid()
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)

    def protection(f: np.ndarray) -> np.ndarray:
        return crossover_response_complex(f, (CrossoverSection(300.0, 8, True),))

    (plant,) = driver_plants(
        [_Measurement(TWEETER, freqs_hz, ones, band_hz=(1000.0, 20_000.0))],
        protection_by_role={TWEETER: protection},
    )
    assert bool(np.all(plant.trusted[plant.driven]))
    assert not bool(np.all(plant.trusted)), (
        "an LR8 high-pass at 300 Hz must fall below the floor somewhere under it"
    )


def test_outside_the_driven_band_a_driver_contributes_exactly_zero():
    """No stimulus means the sample is noise, and noise must not reach a grade.

    Exactly zero rather than small: the assertion is that the band mask is
    applied, not that the contribution happens to be quiet.
    """
    freqs_hz = _grid()
    huge = np.full(freqs_hz.shape, 1e6 + 0j)
    plants = driver_plants(
        [
            _Measurement(WOOFER, freqs_hz, huge.copy(), band_hz=(20.0, 500.0)),
            _Measurement(TWEETER, freqs_hz, huge.copy(), band_hz=(20.0, 500.0)),
        ],
        protection_by_role={},
    )
    predicted = predict_sum(_two_way(), plants)[0.0]
    assert np.all(predicted[freqs_hz > 500.0] == 0.0)
    assert np.any(predicted[freqs_hz <= 500.0] != 0.0)


def test_a_pose_missing_one_commanded_role_is_dropped_rather_than_summed():
    """Half a pose cannot answer a question that compares the two branches."""
    freqs_hz = _grid()
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)
    plants = driver_plants(
        [
            _Measurement(WOOFER, freqs_hz, ones.copy(), angle_deg=0.0),
            _Measurement(TWEETER, freqs_hz, ones.copy(), angle_deg=0.0),
            _Measurement(WOOFER, freqs_hz, ones.copy(), angle_deg=30.0),
        ],
        protection_by_role={},
    )
    predicted = predict_sum(_two_way(), plants)
    assert sorted(predicted) == [0.0]


def test_a_caller_bug_raises_plainly_rather_than_as_a_measurement_verdict():
    """Mismatched arrays are a programming error, not an ill-conditioned path.

    ``ConfiguredPathConditioningError`` carries a slug naming an ill-conditioned
    de-embedding and a flag that routes household-facing copy. Reusing it for a
    caller handing in a response and a grid of different lengths would put a
    measurement verdict on a bug, so it stays a plain ``ValueError`` — and the
    genuine conditioning refusals keep the shared type, asserted beside it so
    the distinction is pinned rather than described.
    """
    freqs_hz = _grid()
    with pytest.raises(ValueError, match="disagree in shape") as excinfo:
        driver_plants(
            [_Measurement(WOOFER, freqs_hz, np.ones(7, dtype=np.complex128))],
            protection_by_role={},
        )
    assert not isinstance(excinfo.value, ConfiguredPathConditioningError)

    with pytest.raises(ConfiguredPathConditioningError):
        driver_plants(
            [_Measurement(
                WOOFER, freqs_hz, np.ones(freqs_hz.shape, dtype=np.complex128),
                band_hz=(90_000.0, 95_000.0),
            )],
            protection_by_role={},
        )


def test_plants_from_one_pose_on_two_grids_raise_rather_than_vanish():
    """A caller defect must not disguise itself as an unanswerable pose.

    Absence from the result means "this angle could not be answered", which is
    ordinary. Two grids at one angle means the plants came from different
    captures, which is not.
    """
    freqs_hz = _grid()
    other_hz = np.geomspace(20.0, 20_000.0, 240)
    plants = (
        driver_plants(
            [_Measurement(
                WOOFER, freqs_hz, np.ones(freqs_hz.shape, dtype=np.complex128),
            )],
            protection_by_role={},
        )
        + driver_plants(
            [_Measurement(
                TWEETER, other_hz, np.ones(other_hz.shape, dtype=np.complex128),
            )],
            protection_by_role={},
        )
    )
    with pytest.raises(ValueError, match="frequency grid"):
        predict_sum(_two_way(), plants)


def test_every_angle_is_predicted_and_keyed_by_its_own_degrees():
    freqs_hz = _grid()
    plants = _ideal_plants(freqs_hz, angles_deg=(-30.0, 0.0, 30.0))
    predicted = predict_sum(_two_way(), plants)
    assert sorted(predicted) == [-30.0, 0.0, 30.0]
    assert all(value.shape == freqs_hz.shape for value in predicted.values())


def test_a_branch_operator_is_evaluated_once_per_grid_not_once_per_angle(
    monkeypatch,
):
    """The reuse that makes an offline search affordable, counted.

    ``branch_operator`` takes no angle precisely so a pose sweep does not pay
    for it repeatedly. Six poses on one grid must cost TWO evaluations, not
    twelve — and a claim about cost is worth a count rather than a comment.

    Also the correctness half: caching keyed on the grid must not change any
    answer, so the cached run is asserted equal to an uncached one.
    """
    import jasper.active_speaker.crossover_v2.forward_model as fm

    freqs_hz = _grid()
    angles = (-22.0, -7.0, 0.0, 7.0, 22.0, 45.0)
    plants = _ideal_plants(freqs_hz, angles_deg=angles)
    uncached = predict_sum(_two_way(), plants)

    calls: list[str] = []
    real = fm.branch_operator

    def counted(candidate, role, grid, **kwargs):
        calls.append(role)
        return real(candidate, role, grid, **kwargs)

    monkeypatch.setattr(fm, "branch_operator", counted)
    cached = fm.predict_sum(_two_way(), plants)

    assert len(calls) == 2, f"expected one per role, saw {calls}"
    assert sorted(calls) == [TWEETER, WOOFER]
    assert sorted(cached) == sorted(uncached) == sorted(angles)
    for angle_deg in angles:
        assert np.array_equal(cached[angle_deg], uncached[angle_deg])


# --- candidate legality: the walls, by name ---------------------------------


def _bounds(**overrides) -> CandidateBounds:
    base = dict(
        fc_band_hz=(1600.0, 3000.0),
        fc_lo_role=TWEETER,
        fc_hi_role=WOOFER,
        declared_floor_hz_by_role={TWEETER: 1600.0},
        beaming_ceiling_hz=1915.4,
        legal_orders=frozenset({2, 4, 8}),
        delay_window_us=(-500.0, 500.0),
        delay_step_us=0.0,
        gain_db_range=(-30.0, 0.0),
        undeclared_roles=(),
    )
    base.update(overrides)
    return CandidateBounds(**base)  # type: ignore[arg-type]


def test_a_corner_below_the_tweeters_declared_floor_is_refused_by_name():
    """H1's wall, in the proposer.

    The safety finding this plan opened with: the routine apply transaction
    never compares a chosen corner against the declared minimum. The proposer
    must, and it must do so through the SHARED predicate rather than its own
    comparison, so the proposer and the boundary check cannot drift.
    """
    assert refusal_for(_two_way(fc_hz=1500.0), _bounds()) == (
        FC_REJECT_BELOW_DECLARED_FLOOR
    )


def test_a_corner_exactly_at_the_declared_floor_is_legal():
    """Owner ruling, 2026-08-17: "exact is legal ... no nannies."

    The manufacturer's recommended crossover point is a sanctioned operating
    point, not a boundary to keep a margin from. Only STRICTLY below is
    refused, and this is the boundary case that says so.
    """
    assert refusal_for(_two_way(fc_hz=1600.0), _bounds()) is None


def test_the_floor_binds_high_pass_corners_only():
    """A low-pass corner never asks a driver to work below its floor.

    The generalization of the two-way rule that keeps it physically exact: in a
    two-way the single corner IS the HF role's high-pass corner, which is why
    the corner sweep could get away with one number. Applying that number to a
    LOW-pass would refuse a woofer for a wall that says nothing about it.
    """
    below_floor = XoverCandidate(
        sections_by_role={WOOFER: (CrossoverSection(1500.0, 4, False),)},
        polarity_by_role={WOOFER: 1},
        delay_us_by_role={WOOFER: 0.0},
        gain_db_by_role={WOOFER: 0.0},
    )
    assert refusal_for(
        below_floor, _bounds(declared_floor_hz_by_role={WOOFER: 1600.0})
    ) == FC_REJECT_OUTSIDE_SEARCH_BAND  # the band, not the floor


def test_an_absent_floor_is_satisfied_and_never_read_as_zero():
    """No declaration means unchanged behaviour, never a floor of zero.

    Inventing a floor where the operator declared none is the nanny behaviour
    the 2026-08-14 ruling excludes; reading absence as zero would be the
    opposite failure. Both are wrong; ``protection_highpass_floor_satisfied``
    owns the answer and this asserts we asked it rather than guessing.
    """
    assert refusal_for(
        _two_way(fc_hz=1650.0), _bounds(declared_floor_hz_by_role={})
    ) is None


@pytest.mark.parametrize(
    "candidate, expected",
    [
        (_two_way(fc_hz=3500.0), FC_REJECT_ABOVE_LOWER_DRIVER_BAND),
        (_two_way(fc_hz=1610.0, order=3), REJECT_UNSUPPORTED_ORDER),
        (_two_way(tweeter_polarity=0), REJECT_INVALID_POLARITY),
        (_two_way(tweeter_delay_us=9_000.0), REJECT_DELAY_OUTSIDE_WINDOW),
        (
            _two_way(gain_db_by_role={WOOFER: 0.0, TWEETER: 1.5}),
            REJECT_GAIN_OUTSIDE_RANGE,
        ),
    ],
)
def test_each_wall_refuses_by_its_own_name(candidate, expected):
    assert refusal_for(candidate, _bounds()) == expected


def test_a_delay_finer_than_the_chain_can_realize_is_refused():
    """R2's wall: #2710 quantizes per-role alignment at +/-20.833 us at 48 kHz.

    A search that proposed a finer delay would be proposing a number the
    emitted graph rounds away — the model would predict a sum the speaker
    cannot produce. The step is DECLARED by the caller from the chain it is
    proposing into, never assumed here.
    """
    step_us = 20.833333
    bounds = _bounds(delay_step_us=step_us)
    assert refusal_for(_two_way(tweeter_delay_us=step_us * 4.0), bounds) is None
    assert refusal_for(_two_way(tweeter_delay_us=step_us * 4.0 + 7.0), bounds) == (
        REJECT_DELAY_NOT_REALIZABLE
    )


def test_with_no_declared_step_no_delay_is_refused_as_unrealizable():
    """This module will not invent a timing bound the caller did not state.

    The emitter's own formatter lattice was considered as a second wall and
    rejected on arithmetic: it carries 0.1 us, so a check against it either
    never fires or refuses nearly everything, and 0.05 us is 0.36 degrees of
    phase at 20 kHz. A gate that cannot fire reads as coverage, which is worse
    than no gate — so an undeclared step refuses nothing, and this pins that
    rather than leaving it to a comment.
    """
    bounds = _bounds(delay_step_us=0.0)
    for delay_us in (100.02, 0.001, -333.333_333, 499.999):
        assert refusal_for(_two_way(tweeter_delay_us=delay_us), bounds) is None


def test_an_undeclared_role_is_named_as_the_cause_not_as_a_missing_band():
    """The cause, not the symptom.

    An undeclared role is what PRODUCES an absent search band, so reporting
    "outside the declared search band" would send the operator to edit a band
    that is not the problem. Asserted for both shapes: a role carrying sections
    and a full-range role carrying only a delay.
    """
    bounds = _bounds(fc_band_hz=None, undeclared_roles=(TWEETER,))
    assert refusal_for(_two_way(), bounds) == REJECT_UNDECLARED_ROLE

    full_range = XoverCandidate(
        sections_by_role={},
        polarity_by_role={TWEETER: 1},
        delay_us_by_role={TWEETER: 0.0},
        gain_db_by_role={TWEETER: 0.0},
    )
    assert refusal_for(full_range, bounds) == REJECT_UNDECLARED_ROLE


def test_an_empty_intersection_reports_the_band_with_both_owners_still_named():
    """Every role declared, no overlap: a real band conflict, not an anomaly."""
    bounds = _bounds(fc_band_hz=None)
    assert refusal_for(_two_way(), bounds) == FC_REJECT_OUTSIDE_SEARCH_BAND
    assert bounds.fc_lo_role == TWEETER and bounds.fc_hi_role == WOOFER
    assert bounds.proposable is False


def test_the_beaming_ceiling_is_guidance_and_never_refuses():
    """#1675 defines the ka prior as something to warn on, not a fence.

    The live jts3 case is the argument: the ceiling is 1915.4 Hz and the
    configured corner is 2000 Hz, so a bound that refused above the ceiling
    would refuse the speaker's own running crossover. It is published for
    disclosure and does not appear in any refusal.
    """
    bounds = _bounds(beaming_ceiling_hz=1915.4)
    assert refusal_for(_two_way(fc_hz=2000.0), bounds) is None


def test_a_refusal_never_depends_on_mapping_order():
    """An operator has to be able to reproduce a refusal.

    Two violations in one candidate must always name the same wall, whichever
    order the roles happen to be stored in.
    """
    forward = XoverCandidate(
        sections_by_role={
            WOOFER: (CrossoverSection(1500.0, 4, False),),
            TWEETER: (CrossoverSection(1500.0, 4, True),),
        },
        polarity_by_role={WOOFER: 1, TWEETER: 7},
        delay_us_by_role={WOOFER: 0.0, TWEETER: 0.0},
        gain_db_by_role={WOOFER: 0.0, TWEETER: 0.0},
    )
    reverse = XoverCandidate(
        sections_by_role=dict(reversed(list(forward.sections_by_role.items()))),
        polarity_by_role=dict(reversed(list(forward.polarity_by_role.items()))),
        delay_us_by_role=forward.delay_us_by_role,
        gain_db_by_role=forward.gain_db_by_role,
    )
    assert refusal_for(forward, _bounds()) == refusal_for(reverse, _bounds())
    assert refusal_for(forward, _bounds()) == FC_REJECT_BELOW_DECLARED_FLOOR


def test_every_slug_refusal_for_can_return_is_in_the_closed_set():
    """A typo'd slug should fail here rather than reach a page as mystery text.

    Walks a battery covering each wall and asserts membership, which is the
    half a frozenset alone cannot give: a constant can be in the set and still
    never be the thing the function returns.
    """
    seen = {
        refusal_for(candidate, bounds)
        for candidate, bounds in (
            (_two_way(fc_hz=1500.0), _bounds()),
            (_two_way(fc_hz=3500.0), _bounds()),
            (_two_way(fc_hz=1610.0, order=3), _bounds()),
            (_two_way(tweeter_polarity=0), _bounds()),
            (_two_way(tweeter_delay_us=9_000.0), _bounds()),
            (_two_way(tweeter_delay_us=27.0), _bounds(delay_step_us=20.833333)),
            (_two_way(gain_db_by_role={WOOFER: 0.0, TWEETER: 1.5}), _bounds()),
            (_two_way(), _bounds(fc_band_hz=None, undeclared_roles=(TWEETER,))),
            (_two_way(), _bounds(fc_band_hz=None)),
        )
    }
    assert None not in seen, "the battery must exercise refusals, not passes"
    assert seen == CANDIDATE_REFUSAL_REASONS, (
        "every declared slug must be reachable and every reachable slug "
        f"declared; battery saw {sorted(seen)}"
    )


# --- bounds come from declarations, resolved by their owners ----------------


def test_bounds_read_the_declared_minimum_recommended_crossover_frequency():
    """The floor's provenance is the owner, not a number this module holds.

    ``resolve_driver_low_limit`` is the 2026-08-17 ruling's single owner of "a
    driver's bottom allowed frequency". A published manufacturer figure wins
    outright, including below the style default — so a bounds object that
    quietly substituted a policy default would be the nanny behaviour the same
    ruling forbids.
    """
    bounds = bounds_from_declarations(
        driver_by_role={
            TWEETER: {"recommended_highpass_hz": 1400.0},
            WOOFER: {},
        },
        search_band_hz_by_role={TWEETER: (1300.0, 4000.0), WOOFER: (200.0, 3000.0)},
        excitation_ceiling_hz_by_role={WOOFER: 2800.0},
        lower_driver_diameter_mm=114.0,
        incumbent_fc_hz=2000.0,
        geometry_seed_us=-405.7,
        delay_step_us=20.833333,
    )
    assert bounds.declared_floor_hz_by_role[TWEETER] == 1400.0
    assert bounds.fc_band_hz == (1300.0, 2800.0)  # ceiling narrowed the band
    assert bounds.fc_lo_role == TWEETER and bounds.fc_hi_role == WOOFER
    assert bounds.beaming_ceiling_hz == pytest.approx(1915.4, abs=0.1)
    assert bounds.legal_orders == frozenset({2, 4, 8})
    # geometry seed +/- one half-period at the incumbent corner
    assert bounds.delay_window_us == pytest.approx((-655.7, -155.7), abs=1e-9)


def test_a_role_with_no_declared_search_band_makes_nothing_proposable():
    """Fail-closed: silence about where a driver may be crossed is not consent.

    ``crossover_search_band_hz`` is a required declaration, so absence is an
    anomaly — and the safe reading of an anomaly is "this role has told us
    nothing", never "this role permits everything".
    """
    bounds = bounds_from_declarations(
        driver_by_role={TWEETER: {"recommended_highpass_hz": 1600.0}},
        search_band_hz_by_role={TWEETER: (1600.0, 4000.0), WOOFER: None},
        excitation_ceiling_hz_by_role={WOOFER: 2800.0},
        incumbent_fc_hz=2000.0,
        geometry_seed_us=0.0,
        delay_step_us=0.0,
    )
    assert bounds.proposable is False
    assert bounds.undeclared_roles == (WOOFER,)
    assert refusal_for(_two_way(), bounds) == REJECT_UNDECLARED_ROLE


def test_an_undeclared_driver_falls_back_to_its_style_anchor_not_to_no_floor():
    """``resolve_driver_low_limit``'s third arm, and it is ORDINARY.

    "Absent" is a legitimate research answer, so a tweeter that publishes no
    minimum recommended crossover still gets its style's anchor — labelled as a
    code default rather than as datasheet data. Asserting this rather than "no
    floor" matters because the two differ in the direction that hurts: reading
    the empty payload as "no floor" would let the search propose a corner an
    undeclared tweeter has no evidence it survives.

    A role whose style has no anchor at all (a low-frequency role) is the one
    case that genuinely yields no floor, and it is asserted here beside it so
    the two are never conflated.
    """
    bounds = bounds_from_declarations(
        driver_by_role={TWEETER: {}, WOOFER: {}},
        search_band_hz_by_role={TWEETER: (1000.0, 4000.0), WOOFER: (200.0, 4000.0)},
        excitation_ceiling_hz_by_role={},
        incumbent_fc_hz=2000.0,
        geometry_seed_us=0.0,
        delay_step_us=0.0,
    )
    assert bounds.declared_floor_hz_by_role[TWEETER] > 0.0
    assert WOOFER not in bounds.declared_floor_hz_by_role

    # ...and a PUBLISHED figure wins outright over that anchor, including well
    # below it: the 2026-08-17 ruling's whole point.
    declared = bounds_from_declarations(
        driver_by_role={TWEETER: {"recommended_highpass_hz": 1400.0}},
        search_band_hz_by_role={TWEETER: (1000.0, 4000.0)},
        excitation_ceiling_hz_by_role={},
        incumbent_fc_hz=2000.0,
        geometry_seed_us=0.0,
        delay_step_us=0.0,
    )
    assert declared.declared_floor_hz_by_role[TWEETER] == 1400.0
    assert declared.declared_floor_hz_by_role[TWEETER] < (
        bounds.declared_floor_hz_by_role[TWEETER]
    )
