# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the analyzer is told about each capture (#2291 Phase 5a-iii).

The sibling of :mod:`.programs`.  That module answers "what does this phase
play, and how loud"; this one answers "what is the analyzer told about the
capture that comes back".  They are the same question asked twice per phase, and
in ``consume_capture`` they are literally adjacent lines.

**Every function here is a decision about what to WITHHOLD, and that is why they
are worth their own module.**  A :class:`MeasurementPriors` is not a bag of
context to fill in as far as it will go: each field it carries licenses a claim
the analyzer will then make, so handing one over where the claim cannot be
supported is how a capture gets graded against a model it has nothing to do
with.  The docstrings below are the record of which withholding is deliberate
and why — the entry baseline's dropped ``predicted_sum`` (nothing is applied
yet, so there is no prediction to track), the cloud's (the mic is off-axis by
construction, so divergence is the spatial variation being sampled), the lateral
pose's dropped composition maps (§4.2 composes per candidate, offline).  Every
one of those is a sentence that must survive refactoring, which is exactly what
having one owner is for.

**Inputs are stated, never reached for.**  Where the conductor's methods read
accumulated session evidence off ``self`` — CHECK's ambient report, the composed
MEASURE program, the stashed predicted sum — these take them as arguments.  That
is the whole behavioural difference between this module and the methods it
replaced, and it is what makes each function answerable from its call.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ..branch_chain import crossover_response_complex, radiating_band_hz, sections_by_role
from jasper.audio_measurement.program_analysis import MeasurementPriors, overlap_band_hz

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program import ExcitationProgram

__all__ = [
    "role_transfers",
    "configured_crossover_transfers",
    "measure_sweep_bounds",
    "check_priors",
    "measure_priors",
    "lateral_priors",
    "verify_priors",
    "cloud_priors",
    "entry_baseline_priors",
]


def role_transfers(
    sections_by_role_map: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Per-role ``freqs -> complex response``, evaluated HOST-side.

    The kernel may not import this package
    (``tests/test_correction_boundary_ssot.py``), so it gets a callable, never
    the ``CrossoverSection`` behind it.
    """
    if sections_by_role_map is None:
        return None
    return {
        role: functools.partial(crossover_response_complex, sections=tuple(sections))
        for role, sections in sections_by_role_map.items()
    }


def configured_crossover_transfers(
    source_preset: Any,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """``(response_by_role, polarity_sign_by_role)`` for the committed crossover.

    ONE derivation, two phases. MEASURE consumes it as §4.2's ``C_c``; VERIFY as
    the candidate's design target for the summed response (R18, #1868). They
    must be the same filters, or "the measurement matches the design" is about a
    design nothing emitted.
    """
    # Lazy, and not because of a cycle — ``camilla_yaml`` names this package
    # only in a docstring. It is the package-shallowness discipline
    # ``round_evidence`` and ``verification`` are reached under: the emitter
    # pulls yaml, camilla_emit, graph_safety, profile and test_signal_plan, and
    # a module whose other six functions need none of that should not make every
    # importer of this package pay for them.
    from ..camilla_yaml import role_polarity

    return (
        role_transfers(sections_by_role(source_preset.crossover_regions)),
        {role: -1 if inverted else 1
         for role, inverted in role_polarity(source_preset).items()},
    )


def measure_sweep_bounds(
    measure_program: "ExcitationProgram | None",
) -> tuple[float | None, float | None]:
    """MEASURE's ACTUAL ``(tweeter sweep lo, woofer sweep hi)``, or ``None``s.

    Read off the COMPOSED MEASURE program rather than derived from Fc, so every
    consumer of :func:`overlap_band_hz`'s clamp — VERIFY's tracking comparison
    and R17's per-candidate scoring band — bounds itself by the frequencies both
    branches were actually excited at (§5.6). One reader, because a second would
    be a second answer to "what did this session sweep".
    """
    if measure_program is None:
        return None, None
    try:
        return (
            measure_program.segment("sweep_t").f1_hz,
            measure_program.segment("sweep_w").f2_hz,
        )
    except KeyError:
        return None, None


def check_priors(*, fc_hz: float) -> MeasurementPriors:
    """CHECK's priors — Fc only, for the MEASURE level solve (#1825).

    CHECK used to run on bare defaults. Its gain solve now scopes each band's
    SNR requirement by whether that band lies inside the crossover overlap
    window (an alignment-class decision needs materially more SNR than a
    magnitude one — see ``program_analysis._band_required_snr_db``), and that
    window is derived from Fc. Withholding it is not neutral: the solve then
    applies the ALIGNMENT requirement everywhere, i.e. solves louder, so this
    prior can only make MEASURE quieter, never louder. Nothing else in
    ``_analyze_check`` reads priors.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)


def measure_priors(
    *,
    fc_hz: float,
    source_preset: Any,
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None,
    ambient_report: Any,
    alignment_delay_bounds_us: tuple[float, float] | None,
) -> MeasurementPriors:
    """MEASURE's priors — the widest set, and the only §4.2 de-embedding.

    ``alignment_delay_bounds_us`` arrives as an argument rather than being
    derived here: its producer shares a helper with the plausibility gate, which
    is not a priors concern, and splitting that helper across a module boundary
    would put two readers of one declaration in two places.

    ``ambient_report`` is CHECK's measured room floor, carried forward so MEASURE
    can grade its own per-driver SNR (issue #1830). Without it
    ``program_analysis._driver_response`` skips the verdict entirely and
    ``DriverResponse.snr`` is None on every v2 session — a shipped instrument
    reading nothing while the evidence to compute it sat in the same session's
    check.json. ``None`` only when CHECK never accepted (no MEASURE can run
    then) or produced no ambient report, in which case the verdict stays
    honestly absent rather than guessed.

    The three configured-path fields are gated on ``protection_sections_by_role``
    together, and that grouping is load-bearing:
    ``_compose_configured_path_ir`` RAISES on a partial prior set, so a
    half-filled set refuses the composition outright instead of producing a
    candidate.
    """
    overlap = overlap_band_hz(fc_hz)
    configured_response, configured_polarity = configured_crossover_transfers(
        source_preset
    )
    return MeasurementPriors(
        crossover_fc_hz=fc_hz,
        alignment_delay_bounds_us=alignment_delay_bounds_us,
        ambient_report=ambient_report,
        measurement_protection_response_by_role=role_transfers(
            protection_sections_by_role
        ),
        configured_crossover_response_by_role=(
            configured_response if protection_sections_by_role is not None else None
        ),
        configured_polarity_sign_by_role=(
            configured_polarity if protection_sections_by_role is not None else None
        ),
        # §4.2's candidate-required bins: radiating span (what the fit masks to)
        # union the trim/alignment overlap band, which together bound everything
        # a candidate consumes. Overlap deliberately UNCLAMPED — the superset is
        # the safe side for this mask.
        candidate_required_band_hz_by_role=(
            None if protection_sections_by_role is None else {
                role: (min(radiating_band_hz(sec)[0], overlap[0]),
                       max(radiating_band_hz(sec)[1], overlap[1]))
                for role, sec in sections_by_role(
                    source_preset.crossover_regions).items()
            }
        ),
    )


def lateral_priors(*, fc_hz: float, ambient_report: Any) -> MeasurementPriors:
    """Priors for one lateral pose — MEASURE-shaped, deliberately NEUTRAL.

    Everything the anchor gets EXCEPT the configured-path composition maps. That
    omission is the point of the round: §4.2's composition is
    ``S_c = sign_c * M * C_c / P`` for ONE candidate, and baking the configured
    ``C`` in here would make the retained evidence answer for 2 kHz alone. So a
    pose is analyzed as ``M`` and the composition stays where §4.2 puts it —
    offline, per candidate, in the consumer.

    ``_compose_configured_path_ir`` returns its input untouched iff ALL THREE
    maps are ``None`` and raises if only some are, so leaving them out is an
    exact, checked no-op, and the analysis stamps
    ``configured_path_composed=False`` accordingly. The fitter's own
    uncomposed-capture rail keeps that from reaching a prescription: a pose is
    never fitted.

    No ``predicted_sum`` and no alignment bounds, for the cloud's reason plus
    §4.4's: the anchor solution is HELD FIXED at the sides, so nothing here may
    be read as a per-pose trim/delay/polarity solve.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz, ambient_report=ambient_report)


def verify_priors(
    *,
    fc_hz: float,
    source_preset: Any,
    predicted_sum: Any,
    sweep_bounds: tuple[float | None, float | None],
) -> MeasurementPriors:
    """VERIFY's priors — the tracking comparison, and the absolute claim.

    The candidate's own crossover rides along for the ABSOLUTE claim (R18,
    #1868), UNGUARDED by ``measurement_protection`` unlike
    :func:`measure_priors` — which needs it only as the ``C_c`` half of a
    de-embedding that cannot run without ``P``. Here it IS the design target.
    Safe because that de-embedding (``_compose_configured_path_ir``, which
    RAISES on a partial prior set) is reachable only from ``_analyze_measure``;
    keep it that way.
    """
    tweeter_sweep_lo_hz, woofer_sweep_hi_hz = sweep_bounds
    configured_response, configured_polarity = configured_crossover_transfers(
        source_preset
    )
    return MeasurementPriors(
        crossover_fc_hz=fc_hz,
        predicted_sum=predicted_sum,
        measure_tweeter_sweep_lo_hz=tweeter_sweep_lo_hz,
        measure_woofer_sweep_hi_hz=woofer_sweep_hi_hz,
        configured_crossover_response_by_role=configured_response,
        configured_polarity_sign_by_role=configured_polarity,
    )


def cloud_priors(*, fc_hz: float) -> MeasurementPriors:
    """Priors for a position-group capture — deliberately WITHOUT ``predicted_sum``.

    VERIFY's priors carry the MEASURE-derived prediction so ``_analyze_verify``
    can compute the tracking comparator ("did apply do what the model
    predicted"). A cloud position must not: the mic is OFF the design axis by
    construction, so measured-vs-predicted divergence there is the spatial
    variation the cloud exists to sample, not a tracking error. Withholding the
    prior leaves ``analysis.verify_tracking`` ``None``, so no tracking claim can
    be made from a capture that cannot support one. The flatness/spec claim
    needs no prior at all — since PR-5 it is made ONCE per group, on the
    combined cloud (``assemble_cloud_group_result``), never per position.

    Withholding the candidate's crossover transfers (R18, #1868) is the same
    decision for the same reason, and is deliberate: a crossover-region ABSOLUTE
    claim off the design axis would grade the crossover's own lobing — which
    moves with angle BY DESIGN — as a realization defect (#1868's forensics
    measured one design-axis defect at a different depth at every cloud
    position). So the kernel records that claim not-evaluated at every cloud
    position and the design-axis capture stays its only judge. Do not "fix" this
    by threading them here.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)


def entry_baseline_priors(*, fc_hz: float) -> MeasurementPriors:
    """Priors for #2291's pre-apply capture — the SAME two withholdings
    :func:`cloud_priors` makes, for a different reason.

    Field by field, against :func:`verify_priors` (the other consumer of the
    identical program):

    * ``crossover_fc_hz`` — **kept**. It is the session's declared crossover,
      not a claim about this capture, and the analyzer uses it to place its
      bands.
    * ``predicted_sum`` — **dropped**. VERIFY carries it so ``_analyze_verify``
      can compute ``verify_tracking`` ("did apply do what the model
      predicted"). Nothing has been applied when this capture is taken, so there
      is no prediction it could be tracking: the prediction describes the graph
      the household is ABOUT to choose. Passing it would make the analyzer grade
      the *entry* graph against the *candidate's* model and report the whole
      intended correction as a realization error. Withholding it leaves
      ``analysis.verify_tracking`` ``None``, which is what
      :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_realization`
      already reads as UNAVAILABLE rather than as a pass.
    * ``measure_tweeter_sweep_lo_hz`` / ``measure_woofer_sweep_hi_hz`` —
      **dropped**. They exist only to clamp the tracking comparison's graded
      band, and there is no tracking comparison here.
    * ``configured_crossover_response_by_role`` /
      ``configured_polarity_sign_by_role`` — **dropped** (R18, #1868). They let
      ``_analyze_verify`` make the crossover-region ABSOLUTE claim against the
      *configured* design. That design is not on the speaker yet, so the claim
      would grade the entry graph for not being the candidate. The benefit
      comparison needs no such claim: it reads this capture's own summed
      response and differences it against the post-apply one.

    What is left is a plain summed-response analysis, which is exactly the input
    :func:`~jasper.active_speaker.crossover_v2.round_evidence.measured_response_from_analysis`
    reduces. Do not thread the withheld priors back in: this capture cannot
    support the claims they license.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)
