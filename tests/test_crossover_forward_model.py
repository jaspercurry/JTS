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
from jasper.active_speaker.crossover_v2.forward_model import (
    XoverCandidate, branch_operator, driver_plants, predict_sum,
)
from jasper.audio_measurement.program_analysis import (
    CONFIGURED_PATH_PROTECTION_FLOOR_DB,
    ConfiguredPathConditioningError,
    predicted_branch_sum,
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


# --- the candidate value itself ---------------------------------------------


def test_a_candidate_compares_by_content_and_refuses_to_hash():
    """Equal by content, and NOT hashable — the docstring's claim, measured.

    Two separately-built candidates describing the same crossover must compare
    equal, which is what lets a caller say two variations are the same proposal
    without reasoning about object identity.

    Hashing is the half worth pinning, because the docstring this class is
    descended from asserted the opposite. ``frozen=True`` generates a
    ``__hash__``, but the mapping fields are ``dict``, so calling it raises.
    A caller that reached for a ``set`` of candidates would meet that
    ``TypeError`` at runtime; this test is where they meet it instead.
    """
    one, two = _two_way(), _two_way()

    assert one is not two
    assert one == two
    assert one != _two_way(fc_hz=FC_HZ * 1.01)

    with pytest.raises(TypeError, match="unhashable"):
        hash(one)
    with pytest.raises(TypeError, match="unhashable"):
        {one}


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
    """The two expressions agree exactly where they can be made comparable.

    **Scope, stated honestly, because "the same arithmetic" is easy to
    overclaim.** What this pins is the SUMMATION ALGEBRA — polarity, per-branch
    gain and the multiply-add — at unity ``C`` and unity ``K``. It does not
    exercise the crossover or correction terms, which have no counterpart in
    ``predicted_branch_sum`` at all; those are covered by the analytic rung
    above and by ``branch_chain``'s own suite. The claim earned here is that
    this model does not reassemble the branch sum differently, not that every
    factor in it has been cross-checked.

    Bit-for-bit is achievable in this scope because the only factors are a real
    gain and an exact +/-1, both of which multiply without rounding.
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
    builds a candidate-only operator — which is the entire reason one evaluation
    can reuse it across angles — so it associates as ``T * ((sign * g) * e)``.
    Floating-point multiplication is not associative, so the two differ by
    rounding.

    The bound is MEASURED, not chosen, and it is worth reading as what it is:
    a few ULP on operands of order 1, which is the floor any reassociation of
    this expression has. It is not evidence about the physics — only that no
    step was added or dropped between the two spellings. Stated with a decade
    of headroom so a real regression moves it and ordinary rounding does not.
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
    # Both polarities, because the inverted branch is not a corner case here:
    # 3 of the banked armrun's arms committed an inverted tweeter, and sign is
    # the factor whose association with the delay term differs between the two
    # expressions.
    for sign in (1, -1):
        for tau_us in (-550.0, -350.0, -250.0, 12.2, 96.0, 405.7):
            candidate = XoverCandidate(
                sections_by_role={},
                polarity_by_role={WOOFER: 1, TWEETER: sign},
                delay_us_by_role={WOOFER: 0.0, TWEETER: tau_us},
                gain_db_by_role={WOOFER: 0.0, TWEETER: 0.0},
            )
            mine = predict_sum(candidate, plants)[0.0]
            theirs = predicted_branch_sum(
                woofer, tweeter, 0.0, 0.0, sign,
                freqs_hz=freqs_hz, residual_delay_us=tau_us,
            )
            worst = max(worst, float(np.max(np.abs(mine - theirs))))
    assert worst <= 1e-14, f"worst absolute divergence {worst:.3e}"


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
    perturbation a candidate variation would ever carry. A control that only
    fired on a large change would still pass against a model with a rounded-away
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

    Also the claim that makes zero-capture-cost evaluation possible at all: the
    plant is a property of the CAPTURE, so two different candidates read the
    same plant.
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


def test_an_unsupplied_required_band_means_the_whole_driven_band():
    """Mirror the kernel, including its ``band is None`` path.

    ``_compose_configured_path_ir`` leaves the whole radiated band required
    when no candidate band is supplied, so this does too. The alternative —
    "required if supplied, else check nothing" — is what silently hollowed out
    ``DriverPlant.trusted``'s by-construction guarantee: 43 untrusted driven
    bins were accepted while the docstring still promised none.

    The consequence is real and is the honest one: a ``P`` that contains a
    crossover IS ill-conditioned across a sweep that runs past the corner, so
    it refuses until the caller declares the span it actually needs.
    """
    freqs_hz = _grid()
    ones = np.ones(freqs_hz.shape, dtype=np.complex128)

    def woofer_lowpass(f: np.ndarray) -> np.ndarray:
        return crossover_response_complex(f, (CrossoverSection(1648.7, 4, False),))

    measurement = _Measurement(WOOFER, freqs_hz, ones, band_hz=(150.0, 4000.0))
    # The floor is genuinely violated deep in the sweep — not a fixture that
    # dodges the question.
    assert np.min(np.abs(woofer_lowpass(np.array([4000.0])))) < 10.0 ** (
        CONFIGURED_PATH_PROTECTION_FLOOR_DB / 20.0
    )

    with pytest.raises(ConfiguredPathConditioningError) as excinfo:
        driver_plants([measurement], protection_by_role={WOOFER: woofer_lowpass})
    assert excinfo.value.protection_floor is True

    # Declaring the span the branch actually owns is honest, passes, and the
    # guarantee holds over exactly that span.
    (narrow,) = driver_plants(
        [measurement],
        protection_by_role={WOOFER: woofer_lowpass},
        required_band_hz_by_role={WOOFER: (150.0, 1200.0)},
    )
    required = narrow.driven & (freqs_hz <= 1200.0)
    assert bool(np.all(narrow.trusted[required]))
    assert not bool(np.all(narrow.trusted)), "the mask still records the rest"


def test_the_trusted_guarantee_holds_over_the_band_that_was_required():
    """The contract ``trusted``'s docstring now states, pinned both ways.

    Unnarrowed: every driven bin is trusted, by construction. Narrowed: the
    guarantee holds over the narrowed span only, and the mask is the answer
    outside it. A test that only checked the first case is what let the
    guarantee go false unnoticed.
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

    # The half a SHAPE check waves through, and the dangerous one: two grids of
    # equal length over different frequencies. The sum would add bins that are
    # not the same frequency and the result would still look like a spectrum.
    shifted = freqs_hz * 1.05
    same_length = driver_plants(
        [_Measurement(
            WOOFER, freqs_hz, np.ones(freqs_hz.shape, dtype=np.complex128),
        )],
        protection_by_role={},
    ) + driver_plants(
        [_Measurement(
            TWEETER, shifted, np.ones(shifted.shape, dtype=np.complex128),
        )],
        protection_by_role={},
    )
    assert same_length[0].freqs_hz.shape == same_length[1].freqs_hz.shape
    with pytest.raises(ValueError, match="frequency grid"):
        predict_sum(_two_way(), same_length)


def test_every_angle_is_predicted_and_keyed_by_its_own_degrees():
    freqs_hz = _grid()
    plants = _ideal_plants(freqs_hz, angles_deg=(-30.0, 0.0, 30.0))
    predicted = predict_sum(_two_way(), plants)
    assert sorted(predicted) == [-30.0, 0.0, 30.0]
    assert all(value.shape == freqs_hz.shape for value in predicted.values())


def test_a_branch_operator_is_evaluated_once_per_grid_not_once_per_angle(
    monkeypatch,
):
    """The reuse that makes evaluating many variations affordable, counted.

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

