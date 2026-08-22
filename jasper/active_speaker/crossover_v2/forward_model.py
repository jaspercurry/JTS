# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the speaker will measure, for a candidate nothing has played.

Offline simulated evaluation: measured per-driver responses plus one
:class:`XoverCandidate` give the predicted summed complex response, per angle,
with no build and no tone. Corners are DECLARED by the operator and this module
predicts what a variation of one would measure; nothing here ranks candidates
or recommends a corner.

``S(f, theta) = sum_role  plant_role(f, theta) * op_role(f)``

``op_role(f) = p_role * C_role(f) * K_role(f) * 10^(g_role/20)
* e^(-j*2*pi*f*tau_role*1e-6)``

**Same arithmetic as the shipped path, not a second one.** Every factor is
evaluated by the function the emitted graph is built from:
:func:`~jasper.active_speaker.branch_chain.crossover_response_complex` for the
crossover ``C`` (the DIGITAL Linkwitz-Riley sections CamillaDSP realizes, not
their analog prototype, which under-read by up to 1.58 dB in the loud
direction) and :func:`~jasper.active_speaker.branch_chain.chain_response` for
the correction biquads ``K`` — which is itself a caller of the one RBJ
evaluator this codebase has. There is no biquad math in this file, and a
re-derivation here would be a defect rather than a convenience.

**Why the plant is separated.** ``plant = M / P`` divides the emitted
protection out of the measurement. It is per driver, per angle, and
CANDIDATE-INDEPENDENT — so it is computed ONCE per capture instead of once per
candidate, and that is the entire reason evaluating a set of variations costs
no captures. It also puts the ill-conditioning rule in one place, evaluated
once, rather than in a loop that would have to re-decide it per candidate.

The conditioning policy is shared, not restated:
:data:`~jasper.audio_measurement.program_analysis.CONFIGURED_PATH_PROTECTION_FLOOR_DB`
is the same floor
:func:`~jasper.audio_measurement.program_analysis._compose_configured_path_ir`
refuses on, because both readers divide by the same ``P`` and must agree about
where that becomes dishonest.

**THE LANDMINE: the delay is a RESIDUAL.** This module never accepts an applied
delay. ``_aligned_branch_tf`` references each branch to its OWN direct peak, so
the physical inter-driver gap is already out of the measured pair; phasing that
pair by the full applied delay counts the gap twice and, in the reverting
commit's own words, "injects a deep comb into the predicted sum on good
measurements and fails VERIFY". :attr:`XoverCandidate.delay_us_by_role` is
therefore a residual in the analysis frame, and the ONE derivation that builds
one is
:func:`~jasper.audio_measurement.program_analysis.summed_model_residual_delay_us`
— call it rather than subtracting by hand.
At the bare anchor the residual is exactly ``0.0``, and this model then equals
:func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum` at zero
residual bit for bit — pinned by test, because "the same arithmetic" is a claim
that deserves an exact number rather than a tolerance.

**Arrays in, arrays out.** No session object, no I/O, no scoring. Everything
here is fixture-testable, which is what lets the validation ladder grade the
model against banked hardware without a speaker.

Units are in every name: ``_hz``, ``_us``, ``_db``, ``_deg``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.program_analysis import (
    CONFIGURED_PATH_PROTECTION_FLOOR_DB, ConfiguredPathConditioningError,
)

from ..branch_chain import (
    CrossoverSection, chain_response, crossover_response_complex,
)

__all__ = [
    "DriverPlant",
    "XoverCandidate",
    "branch_operator",
    "driver_plants",
    "predict_sum",
]


@dataclass(frozen=True)
class XoverCandidate:
    """One crossover proposal, in the emitted graph's own vocabulary.

    Five axes over four fields — corner and order share ``sections_by_role`` —
    all per role, so a three-way is expressible:

    ``sections_by_role``
        The Linkwitz-Riley sections each branch runs through — corner,
        order and direction, exactly
        :func:`~jasper.active_speaker.branch_chain.sections_by_role`'s shape.
        Slope is ``6 * order`` dB/octave.
    ``polarity_by_role``
        ``+1`` or ``-1``, the branch's commanded polarity.
    ``delay_us_by_role``
        **RESIDUAL** delay in microseconds, signed, in the analysis frame —
        never an applied delay. See the module docstring's landmine and
        :func:`~jasper.audio_measurement.program_analysis.summed_model_residual_delay_us`,
        which is the one derivation that builds one.
    ``gain_db_by_role``
        The emitted per-driver trim in dB, at or below unity.

    Frozen, and EQUAL by content: a caller holding several variations has to be
    able to say two of them are the same proposal, and comparing mutable objects
    would make that a claim about object identity rather than about the
    crossover.

    Not *hashable* by content, and the distinction is worth stating because the
    docstring this text is descended from claimed it was. The mapping fields are
    ordinarily ``dict``, so ``frozen=True`` generates a ``__hash__`` that raises
    ``TypeError: unhashable type: 'dict'`` the moment it is called — measured,
    not assumed. Deduplicate variations with ``==`` or a list, never a ``set``
    or a dict key. Wrapping the fields in ``MappingProxyType`` does NOT buy the
    hash: the proxy delegates to the mapping underneath, so it raises the same
    way (measured on 3.12). Hashing would need field values that are themselves
    hashable — sorted tuples of pairs rather than mappings — and a test.
    """

    sections_by_role: Mapping[str, tuple[CrossoverSection, ...]]
    polarity_by_role: Mapping[str, int]
    delay_us_by_role: Mapping[str, float]
    gain_db_by_role: Mapping[str, float]

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role this candidate says anything about, sorted.

        The union rather than the intersection: a role carrying a delay but no
        sections is still a role this candidate asks something about, and
        :func:`predict_sum` requires every one of them to be present at a pose
        before it will answer that pose.
        """
        return tuple(sorted(
            set(self.sections_by_role)
            | set(self.polarity_by_role)
            | set(self.delay_us_by_role)
            | set(self.gain_db_by_role)
        ))


@dataclass(frozen=True)
class DriverPlant:
    """One driver's candidate-independent response at one angle: ``M / P``.

    ``plant`` is the measured complex transfer function with the emitted
    protection divided out — what this driver does BEFORE any crossover a
    candidate might propose. Multiply it by a candidate's branch operator and
    you have that candidate's predicted contribution at this angle.

    ``band_hz`` is the DRIVEN band: outside it the excitation put no energy
    into this driver, so the sample there is noise rather than a small
    response, and :func:`predict_sum` contributes exactly zero from it.

    ``trusted`` marks where ``P`` was well-conditioned across the WHOLE grid,
    including outside the driven band.

    **The guarantee, stated exactly, because a looser version of this sentence
    went false once already.** :func:`driver_plants` refuses when any bin in
    the REQUIRED band is untrusted, and the required band defaults to the whole
    driven band. So a plant that exists has a trusted driven band by
    construction — UNLESS its caller narrowed the required band with
    ``required_band_hz_by_role``, in which case the guarantee holds over that
    narrower span and the mask is the only answer outside it. An earlier
    revision narrowed the default to "required if supplied, else nothing" and
    left this sentence unchanged; the measured cost was 43 untrusted driven
    bins accepted with the guarantee still claimed.

    **No PRODUCTION code reads ``trusted``: not this module, and not any
    consumer anywhere in the tree** — said plainly so a reader does not have to
    work out whether it is dead. Its contract is held by test instead
    (``test_the_trusted_guarantee_holds_over_the_band_that_was_required``), and
    it is published rather than dropped because it is the honest record of a
    division this function already performed: where a caller narrows the
    required band, anything grading the predicted sum has to mask by ``trusted``
    outside that span rather than assume it, and a grade that assumed it would
    be reading noise as response. That answer is a property of the CAPTURE
    rather than of any candidate, so it is derived once, here, where the
    division that produced it happens, instead of per candidate by whoever
    grades one.
    """

    role: str
    angle_deg: float
    freqs_hz: np.ndarray
    plant: np.ndarray
    band_hz: tuple[float, float]
    trusted: np.ndarray

    @property
    def driven(self) -> np.ndarray:
        """Boolean mask of the bins this driver was actually excited at."""
        lo_hz, hi_hz = self.band_hz
        return (self.freqs_hz >= float(lo_hz)) & (self.freqs_hz <= float(hi_hz))


def driver_plants(
    measurements: Iterable[Any],
    *,
    protection_by_role: Mapping[str, Callable[[np.ndarray], Any]],
    required_band_hz_by_role: Mapping[str, tuple[float, float]] | None = None,
    protection_floor_db: float = CONFIGURED_PATH_PROTECTION_FLOOR_DB,
) -> tuple[DriverPlant, ...]:
    """Divide the emitted protection out of every measured response, once.

    ``measurements`` are duck-typed on ``role`` / ``angle_deg`` / ``freqs_hz`` /
    ``complex_tf`` / ``band_hz`` — the shape a per-driver capture already has,
    plus the angle it was taken at. Duck-typed rather than imported because
    this module has no business holding the session's dataclass: this is the
    boundary where a session value enters, and it converts to this module's own
    :class:`DriverPlant`, which is what everything downstream takes.

    ``protection_by_role`` maps a role to ``freqs -> complex response`` — the
    callable :func:`~jasper.active_speaker.crossover_v2.priors.role_transfers`
    builds. A CALLABLE and not a section list, because the measurement kernel
    may not import this package and the same shape crosses that boundary in
    both directions.

    **Refuses, never saturates — on the REQUIRED band.** A protection response
    below ``protection_floor_db`` is one the division cannot invert honestly:
    dividing it out amplifies whatever noise the capture has there faster than
    it recovers signal. Raising is the same answer
    ``_compose_configured_path_ir`` gives, with the same
    :class:`ConfiguredPathConditioningError` and the same ``protection_floor``
    flag — which tells a household WHICH lever it has, since ``|P| <
    floor`` does not involve the crossover and no Fc can clear it.

    ``required_band_hz_by_role`` is where the de-embedding must be honest, and
    it is intersected with the driven band exactly as the kernel intersects
    ``candidate_required_band_hz_by_role`` with ``radiated_band_hz`` — including
    when it is absent: the kernel's ``band is None`` path leaves the whole
    radiated band required, and so does this one. Mirroring the kernel is the
    rule this module claims to follow, so it follows it here too rather than
    inventing a permissive default that would quietly hollow out the guarantee
    on :attr:`DriverPlant.trusted`.

    Why the parameter exists at all — a measured lesson, not a hypothetical.
    The whole-band check refuses a capture whenever ``P`` contains a crossover:
    a sweep necessarily runs past the corner, and an LR4 low-pass at 1648.7 Hz
    is about -30 dB at 4 kHz, inside a woofer's 150-4000 Hz sweep. Every banked
    armrun candidate refused that way, for a de-embedding that provably cancels.
    **The narrowing is the caller's to declare, not this function's to assume**,
    because which band an evaluation depends on is a property of the candidate
    — and a ``P`` that contains a crossover is the misuse named below, whose
    honest answer really is a refusal until the caller says which span it needs.

    ``P`` is the EMITTED protection — what the graph actually ran during this
    capture. On a MEASURE capture that is the per-driver protective high-pass,
    not the full crossover; handing this function a crossover it did not run is
    the misuse the paragraph above was found by.

    A role with no protection entry is a role running full range in the emitted
    graph, and full range means ``P = 1``: nothing to divide out. That is the
    honest reading rather than a refusal, and it matches
    ``branch_chain.sections_by_role``'s treatment of a role with no region.
    """
    minimum = 10.0 ** (float(protection_floor_db) / 20.0)
    plants: list[DriverPlant] = []
    for measurement in measurements:
        role = str(measurement.role)
        freqs_hz = np.asarray(measurement.freqs_hz, dtype=np.float64)
        measured = np.asarray(measurement.complex_tf, dtype=np.complex128)
        if measured.shape != freqs_hz.shape:
            # A plain ValueError, deliberately: ``ConfiguredPathConditioningError``
            # carries a slug that names an ill-conditioned de-embedding and a
            # flag that routes household-facing copy. A response and its own
            # grid disagreeing in shape is neither — it is a caller handing in
            # mismatched arrays, and dressing it as a conditioning refusal
            # would put a measurement verdict on a programming error.
            raise ValueError(f"{role} response and grid disagree in shape")
        lo_hz, hi_hz = (float(edge) for edge in measurement.band_hz)

        transfer = protection_by_role.get(role)
        if transfer is None:
            protection = np.ones(freqs_hz.shape, dtype=np.complex128)
        else:
            protection = np.asarray(transfer(freqs_hz), dtype=np.complex128)
        trusted = np.abs(protection) >= minimum

        driven = (freqs_hz >= lo_hz) & (freqs_hz <= hi_hz)
        if not np.any(driven):
            raise ConfiguredPathConditioningError(f"no driven bins for {role}")
        if not np.all(np.isfinite(protection[driven])):
            raise ConfiguredPathConditioningError(f"non-finite P for {role}")

        band = (required_band_hz_by_role or {}).get(role)
        required = driven if band is None else driven & (
            (freqs_hz >= float(band[0])) & (freqs_hz <= float(band[1]))
        )
        if not np.any(required):
            raise ConfiguredPathConditioningError(f"no required bins for {role}")
        if not np.all(trusted[required]):
            raise ConfiguredPathConditioningError(
                f"P below {protection_floor_db:g} dB for {role}",
                protection_floor=True,
            )
        plants.append(DriverPlant(
            role=role,
            angle_deg=float(measurement.angle_deg),
            freqs_hz=freqs_hz,
            # ``where=`` guards an exact zero only; the POLICY floor above has
            # already refused anything ill-conditioned on the driven band, and
            # outside it an untrusted bin is marked rather than repaired.
            plant=np.divide(
                measured, protection,
                out=np.zeros_like(measured), where=protection != 0,
            ),
            band_hz=(lo_hz, hi_hz),
            trusted=trusted,
        ))
    return tuple(plants)


def branch_operator(
    candidate: XoverCandidate,
    role: str,
    freqs_hz: np.ndarray,
    *,
    linearization_by_role: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> np.ndarray:
    """What this candidate does to one branch, independent of the angle.

    ``p * C * K * 10^(g/20) * e^(-j*2*pi*f*tau*1e-6)`` — the corner sweep's own
    operator with the crossover made a PARAMETER and the ``1/P`` term moved
    into the plant, which is what decouples it from a per-candidate build.

    ``linearization_by_role`` carries each role's correction biquads as the
    plain ``{biquad_type, freq, q, gain}`` records
    :func:`~jasper.active_speaker.branch_chain.chain_response` speaks. Absent
    or empty means ``K = 1``: an uncorrected branch. **Held FIXED across a set
    of variations** — the shipped sweep re-fits it per candidate, and a re-fit
    per candidate is exactly what would put a build back into an evaluation
    that is meant to cost none. The bias that introduces is systematic and
    named: the model predicts "this crossover with the CURRENT linearization"
    while an applied round emits "this crossover with a linearization re-fit
    THROUGH it", and the two differ most near Fc, where the crossover moved
    most. Predicting a candidate both ways — fitted ``K`` and ``K = 1`` — is
    what tells a reader whether a difference survives the term the model is
    holding still.

    The multiplication order is load-bearing at the one place it can be:
    ``polarity`` and the gain are applied so that a unity candidate at zero
    residual delay reduces to ``predicted_branch_sum``'s own expression bit for
    bit. With a non-zero delay the two associate differently and agree to
    floating-point rounding instead; the test states that bound as a measured
    number rather than asserting exactness it does not have.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    sections = candidate.sections_by_role.get(role, ())
    crossover = (
        np.asarray(crossover_response_complex(freqs, sections), dtype=np.complex128)
        if sections else np.ones(freqs.shape, dtype=np.complex128)
    )
    filters = (linearization_by_role or {}).get(role, ())
    correction = (
        np.asarray(chain_response(filters, freqs), dtype=np.complex128)
        if filters else np.ones(freqs.shape, dtype=np.complex128)
    )
    polarity = int(candidate.polarity_by_role.get(role, 1))
    gain = 10.0 ** (float(candidate.gain_db_by_role.get(role, 0.0)) / 20.0)
    operator = crossover * correction * (polarity * gain)

    delay_us = float(candidate.delay_us_by_role.get(role, 0.0))
    if delay_us != 0.0:
        # Guarded exactly as ``predicted_branch_sum`` guards its own: at zero
        # residual there is no phase term at all, which is what makes the
        # anchor frame the frame the measured pair is already in — and what
        # makes the bit-for-bit claim above true rather than approximate.
        operator = operator * np.exp(-1j * 2.0 * np.pi * freqs * delay_us * 1e-6)
    return operator


def predict_sum(
    candidate: XoverCandidate,
    plants: Sequence[DriverPlant],
    *,
    linearization_by_role: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[float, np.ndarray]:
    """This candidate's predicted complex sum at every angle in ``plants``.

    ``{angle_deg: S(f)}``, complex and on each angle's own grid. Complex rather
    than dB because the caller decides what to do with it — a flatness reading
    takes a magnitude, a null-depth check wants the phase, and converting here
    would throw half of it away.

    **A pose is answered only when EVERY role the candidate names is present at
    it.** A half-measured angle is dropped rather than summed, because every
    question this prediction is asked is a comparison between the branches,
    which a pose missing one of them cannot answer. Dropped angles are simply
    absent from the result — the caller sees which angles it got.

    A role named with NO sections is still required, and that is the honest
    reading rather than an oversight: a role with no crossover runs full range
    in the emitted graph, so it contributes everywhere and a pose missing it is
    missing a real contributor.

    Outside a driver's driven band its contribution is exactly zero: there was
    no stimulus there, so the measurement is noise and summing it would let
    noise reach the grade.

    A pose whose plants disagree about their frequency grid RAISES rather than
    being dropped. An absent angle means "this pose could not be answered",
    which is ordinary; plants from one pose on two different grids means they
    came from different captures, which is a caller defect, and silently
    folding it into the same absence would hide it.

    **Each branch operator is evaluated once per grid, not once per angle**,
    which is the point of :func:`branch_operator` taking no angle. Six poses on
    one grid cost two operator evaluations rather than twelve, and evaluating a
    set of variations over those poses pays the difference every time. Keyed on
    the grid's own bytes rather than on object identity, so two
    equal-but-distinct arrays share correctly and two different grids never
    collide.
    """
    by_angle: dict[float, list[DriverPlant]] = {}
    for plant in plants:
        by_angle.setdefault(float(plant.angle_deg), []).append(plant)

    named = set(candidate.roles)
    operators: dict[tuple[str, bytes], np.ndarray] = {}
    predicted: dict[float, np.ndarray] = {}
    for angle_deg, angle_plants in sorted(by_angle.items()):
        present = {plant.role: plant for plant in angle_plants}
        if not named.issubset(present):
            continue
        grid = angle_plants[0].freqs_hz
        if any(
            not np.array_equal(plant.freqs_hz, grid) for plant in angle_plants
        ):
            # VALUES, not shape. Two grids of equal length over different
            # abscissae are the dangerous case precisely because a shape check
            # waves them through: the sum would then add bins that are not the
            # same frequency, and the result looks like a spectrum.
            raise ValueError(
                f"plants at {angle_deg} deg disagree about their frequency grid"
            )
        total = np.zeros(grid.shape, dtype=np.complex128)
        for role in sorted(named):
            plant = present[role]
            key = (role, plant.freqs_hz.tobytes())
            operator = operators.get(key)
            if operator is None:
                operator = branch_operator(
                    candidate, role, plant.freqs_hz,
                    linearization_by_role=linearization_by_role,
                )
                operators[key] = operator
            total = total + np.where(plant.driven, plant.plant * operator, 0.0)
        predicted[angle_deg] = total
    return predicted
