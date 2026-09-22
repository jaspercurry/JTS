# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from jasper.active_speaker.flat_spec import GradedSpec, evaluate_flat_spec, spec_flatness_gauge
from jasper.attribution.position_evidence import position_evidence_block
from jasper.audio_measurement.gating import (
    ENTANGLEMENT_SOURCE_DECLARED,
    ENTANGLEMENT_SOURCE_MEASURED,
    EntanglementFloor,
)
from jasper.audio_measurement.interference_nulls import identify_interference_nulls
from jasper.audio_measurement.room_limits import cloud_trusted_floor_hz
from jasper.audio_measurement.spatial_combine import (
    DEFAULT_ECHO_BAND_HZ,
    DEFAULT_ECHO_SEARCH_US,
    GEOMETRY_MIN_RESOLUTION_STEPS,
    PositionCapture,
    combine_positions,
    merged_true_intervals,
)

from ..verification import (
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
    _crossover_region_null_registry,
    _null_registry_to_dict,
)
from .carve_out_copy import _geometry_guidance_copy, carve_outs_by_band
from .records import POSITION_ROLE_ONAX, PositionGeometry, _DESIGN_AXIS_GEOMETRY


@dataclass(frozen=True)
class _CloudPosition:
    """One accepted position inside a group, retained for the group-end combine.

    ``response`` is the capture's ``ProgramAnalysis.summed_response``, carrying
    the calibrated, reflection-gated magnitude on a linear (rfftfreq) grid plus
    the matching complex TF. Held as the response rather than a pre-built
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` because
    the per-position work the null gate and the spec curve do needs the same
    object, and re-deriving it from a lossy intermediate would drift.
    """

    position_id: str
    index: int
    attempt: int
    prompt: str
    wide: bool
    captured_at: float
    response: Any
    sample_rate_hz: int
    # The named question this position answers (:data:`POSITION_ROLES`), copied
    # off the prompt the operator was given. Defaulted so construction sites
    # that predate roles stay valid.
    role: str = POSITION_ROLE_ONAX
    # WHERE the microphone was, carried off the SAME prompt ``role`` and
    # ``wide`` come from. Held on the position rather than re-derived at
    # retention: a geometry retake shows a different prompt than the table's,
    # and a derivation from the index would state the spot the operator was
    # told to abandon. Defaulted to the design axis, which is honest for a
    # fixture: a position built without a pose is one nobody moved.
    geometry: PositionGeometry = _DESIGN_AXIS_GEOMETRY
    # The contract-derived analysis bands this position's GROUP is
    # combined/searched with — ``spatial_combine.combine_positions``' own
    # kwargs. Every position in one group shares the same session-derived
    # values, so carrying them here lets :func:`combine_cloud_positions` derive
    # the bands from ``positions`` alone and no two call sites can drift.
    # ``None`` means the module defaults.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None


def cloud_position_capture(position: _CloudPosition) -> Any:
    """One retained position → a :class:`spatial_combine.PositionCapture`.

    Regime of the ``ir`` field, stated exactly because ``detect_echo``'s answer
    depends on it: it is the inverse rFFT of the response's GATED, CALIBRATED
    complex transfer function — the impulse response after
    ``deconv.direct_arrival_window`` and the adaptive reflection gate, not the
    raw deconvolved IR. The direct arrival is present and early secondary
    arrivals inside the gate survive; late room reflections beyond the gate are
    gone by construction. ``tests/test_crossover_v2_cloud_geometry_corpus.py``
    measures that this agrees with the ungated IR on the S0 corpus's verdict.
    """
    response = position.response
    freqs = np.asarray(response.freqs_hz, dtype=float)
    magnitude = np.asarray(response.magnitude_db, dtype=float)
    complex_tf = np.asarray(response.complex_tf)
    # ``program_analysis._n_fft_for`` always returns a power of two (>= 8192),
    # so the analysis grid is an even-length rfft and ``n = 2*(bins-1)`` inverts
    # it exactly.
    ir = np.fft.irfft(complex_tf, n=2 * (complex_tf.size - 1))
    return PositionCapture(
        position_id=position.position_id,
        freqs_hz=freqs,
        magnitude_db=magnitude,
        sample_rate=int(position.sample_rate_hz),
        ir=ir,
        # Carrying the role changes no combination (the reduction stays
        # unweighted) and is what lets the per-position residual say "on-axis"
        # rather than "position 3".
        role=str(position.role or ""),
    )


def _geometry_verdict_from_combined(
    combined: Any, n_positions: int,
) -> dict[str, Any]:
    """The geometry-verdict dict from an ALREADY-COMBINED result.

    Separate from :func:`cloud_geometry_verdict` so a caller can combine a
    group exactly ONCE and derive both the retry gate and the pipeline from that
    one object. A plain JSON-native dict, because the host persists it verbatim.
    ``locked`` is ``False`` on every degraded path, and ``reason`` says which.
    """
    if combined is None:
        return {
            "locked": False,
            "reason": "combine_failed",
            "n_positions": n_positions,
        }
    geometry = combined.geometry
    return {
        "locked": bool(geometry.locked),
        "reason": str(geometry.reason),
        "n_confident": int(geometry.n_confident),
        "n_positions": int(geometry.n_positions),
        "median_tau_us": float(geometry.median_tau_us),
        "clustered_fraction": float(geometry.clustered_fraction),
        "thin_evidence": bool(geometry.thin_evidence),
    }


@dataclass(frozen=True)
class CloudCombine:
    """:func:`combine_cloud_positions`'s answer, plus the line a failure earns.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_cloud_combine_failed``, ``None`` when there
    was nothing to say, as data rather than a log call because this module is
    side-effect-free.
    """

    combined: Any | None
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class CloudVerdict:
    """:func:`cloud_geometry_verdict`'s answer, carrying the same line.

    ``verdict`` is the plain JSON-native dict the host persists verbatim into
    the durable v2 state; ``diagnostics`` is whatever the combine underneath
    it would have journalled.
    """

    verdict: dict[str, Any]
    diagnostics: dict[str, Any] | None = None


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> CloudCombine:
    """Assemble a closed group and combine it.

    ``CloudCombine.combined`` is a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`, or
    ``None`` when the group cannot be combined. Call it exactly ONCE per
    group-close event and derive both the geometry verdict and the pipeline from
    that one object: the combine is 3-6 s across runs and hosts on the S0
    ten-position corpus (interpreter-bound ``smooth_fractional_octave``, worse
    on a Pi 5), and :data:`GEOMETRY_RETRY_POSITIONS` allows up to three close
    attempts per group.

    Never raises: a group's captures are already-accepted evidence, so an
    unusable cloud is a ``None`` the caller turns into an honest "unknown"
    rather than an exception that would strand the session.
    """
    if not positions:
        return CloudCombine(None)
    # Every position in one group carries the SAME session-derived bands, so
    # reading them off the first is reading the group's own. ``None`` (a caller
    # that declared no driver contract) falls back to the module default.
    echo_band_hz = positions[0].echo_band_hz or DEFAULT_ECHO_BAND_HZ
    signal_band_hz = positions[0].signal_band_hz
    try:
        return CloudCombine(combine_positions(
            [cloud_position_capture(p) for p in positions],
            echo_band_hz=echo_band_hz,
            signal_band_hz=signal_band_hz,
        ))
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return CloudCombine(
            None, {"positions": len(positions), "error": str(exc)},
        )


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> CloudVerdict:
    """Combine, then read ``.geometry``.

    A convenience wrapper for callers that only have ``positions``; the session
    does NOT call this, because it combines once and derives both answers.

    Reason-string divergence, disclosed: an empty ``positions`` short-circuits
    here with ``reason="no_positions"``, while
    :func:`_geometry_verdict_from_combined` called directly with
    ``combined=None`` and ``n_positions=0`` reports ``combine_failed`` for the
    same fact.
    """
    if not positions:
        return CloudVerdict(
            {"locked": False, "reason": "no_positions", "n_positions": 0}
        )
    result = combine_cloud_positions(positions)
    return CloudVerdict(
        _geometry_verdict_from_combined(result.combined, len(positions)),
        result.diagnostics,
    )


# --------------------------------------------------------------------------- #
# THE GROUP CLOSE — what a closed cloud is worth, once
# --------------------------------------------------------------------------- #
#
# Everything above answers "is this ONE take evidence". This section answers
# what the group close asks next: given every retained position, what did the
# cloud measure, what did the honesty instruments carve out of it, and what is
# the resulting spec verdict.
#
# The "no household vocabulary" rule is about REFUSALS and is intact. What this
# section carries is DISCLOSURE copy on a group result — sentences about what an
# instrument carved out, which have no refusal code to route through.
#
# The echo/detector band and ``signal_band_hz`` derive from the declared
# contract: the summed system's swept band for the passband, the tweeter's
# measurement band for the upper echo band.

# Point limit for smoothed cloud disclosure curves.
CLOUD_CURVE_MAX_JSON_POINTS = 512


@dataclass(frozen=True)
class _CloudEchoBand:
    """The echo/null analysis band the pipeline will APPLY, plus how it was
    derived — one value, so band and provenance cannot be carried apart.

    ``band_hz`` is what the detector runs on. ``derived_lo_hz`` is the lower
    edge the declared contract produced BEFORE the HF-regime clamp, so a reader
    can tell a contract-derived band from a clamped one without the journal
    (#1763). ``source`` names which derivation path produced the band:

    * ``declared`` — the tweeter's ``measurement_band_hz``, possibly narrowed by
      the passband containment clamp or raised by the HF-regime clamp
      (``hf_regime_clamped`` tells which).
    * ``undeclared_default`` — no measurement band was threaded through, so
      ``DEFAULT_ECHO_BAND_HZ`` stands in.
    * ``clamp_degenerate_default`` — the HF clamp would have left a band too
      narrow to resolve anything in (:func:`_min_clamped_echo_band_width_hz`).
    * ``passband_fallback`` — the declared band sits entirely outside the
      composed passband, so the passband stands in.

    ``diagnostics`` carries the journal fields the flow emits, ``None`` when
    there is nothing to say, as data because this module is side-effect-free.
    """

    band_hz: tuple[float, float]
    source: str
    hf_regime_clamped: bool
    derived_lo_hz: float
    diagnostics: dict[str, Any] | None = None

    def disclosure(self) -> dict[str, Any]:
        """The JSON-native provenance block the pipeline payload carries.

        Deliberately does NOT repeat ``band_hz``: the payload already publishes
        the applied band as ``echo_band_hz``.
        """
        return {
            "source": self.source,
            "hf_regime_clamped": self.hf_regime_clamped,
            "derived_lo_hz": float(self.derived_lo_hz),
            "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        }


def _min_clamped_echo_band_width_hz() -> float:
    """The narrowest band the HF-regime clamp may hand the detector, derived
    from the DETECTOR's own constants rather than picked.

    ``detect_echo``'s quefrency step is ``resolution_us = 1e6 / bandwidth``, and
    ``assess_geometry`` refuses to cluster any estimate whose ``tau_us`` is
    below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. Once that floor
    reaches the TOP of the searched window no delay can be clustered at all:

        GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / DEFAULT_ECHO_SEARCH_US[1]
          = 3.0 * 1e6 / 800 us = 3750 Hz

    The searched window's own edge margin bounds at ~1470 Hz, i.e. slacker, and
    ``MIN_ECHO_BAND_BINS`` needs only ~176 Hz at 48 kHz, so this floor is the
    binding one and clearing it clears both.
    """
    return GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / float(DEFAULT_ECHO_SEARCH_US[1])


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _CloudEchoBand:
    """The contract-derived echo/null analysis band: the tweeter's declared
    ``measurement_band_hz``, returned WITH its provenance
    (:class:`_CloudEchoBand`).

    Falls back to ``DEFAULT_ECHO_BAND_HZ`` when no tweeter measurement band was
    threaded through — the band every corpus test validated
    ``identify_interference_nulls`` at.

    Containment: clamped to sit INSIDE ``signal_band_hz``, never wider. A band
    that neither contains nor sits clear of the analysis band leaves
    ``detect_echo``'s signal-presence screen uncalibrated
    (``spatial_combine.BAND_BELOW_PASSBAND_MARGIN_DB``). Since
    ``signal_band_hz`` is the union of both roles' excitation bands, the clamp
    is a no-op for any well-formed 2-way contract.

    HF regime (#1763): a contained lower edge below
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ` is RAISED to that floor and the clamp
    disclosed, in the provenance and in the WARNING event the flow emits from
    ``diagnostics``. The contract's upper edge is kept: the floor says where the
    detector's calibrations hold, not how wide the driver's window is.

    When the clamp cannot produce a usable band — surviving width below
    :func:`_min_clamped_echo_band_width_hz` — the band falls back to
    ``DEFAULT_ECHO_BAND_HZ`` with its own disclosure. That fallback is NOT
    re-clamped into the passband, so it can leave the signal-presence screen
    uncalibrated; that is the lesser loss against a band too narrow to resolve
    any delay, and it needs ``min(declared_upper, passband_upper)`` below
    7750 Hz to reach at all.
    """
    declared = tweeter_measurement_band_hz is not None
    band = tweeter_measurement_band_hz or DEFAULT_ECHO_BAND_HZ
    lo = max(float(band[0]), float(signal_band_hz[0]))
    hi = min(float(band[1]), float(signal_band_hz[1]))
    if lo >= hi:
        # A malformed declared contract: the tweeter's measurement band sits
        # entirely outside the composed passband. Fall back to the passband
        # rather than hand back an inverted pair that would raise deep inside
        # combine_positions with no context.
        return _CloudEchoBand(
            band_hz=(float(signal_band_hz[0]), float(signal_band_hz[1])),
            source="passband_fallback",
            hf_regime_clamped=False,
            derived_lo_hz=lo,
            diagnostics={
                "declared_measurement_band_hz": list(band),
                "signal_band_hz": list(signal_band_hz),
            },
        )
    if lo < ECHO_BAND_HF_REGIME_FLOOR_HZ:
        min_width_hz = _min_clamped_echo_band_width_hz()
        if hi - ECHO_BAND_HF_REGIME_FLOOR_HZ < min_width_hz:
            return _CloudEchoBand(
                band_hz=(float(DEFAULT_ECHO_BAND_HZ[0]), float(DEFAULT_ECHO_BAND_HZ[1])),
                source="clamp_degenerate_default",
                hf_regime_clamped=False,
                derived_lo_hz=lo,
                diagnostics={
                    "derived_lo_hz": lo, "upper_hz": hi,
                    "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
                    "min_width_hz": min_width_hz,
                    "fallback_band_hz": list(DEFAULT_ECHO_BAND_HZ),
                },
            )
        # ``clamped_lo_hz`` equals ``floor_hz`` by construction; both are
        # logged so a journal reader need not know that.
        return _CloudEchoBand(
            band_hz=(ECHO_BAND_HF_REGIME_FLOOR_HZ, hi),
            source="declared" if declared else "undeclared_default",
            hf_regime_clamped=True,
            derived_lo_hz=lo,
            diagnostics={
                "derived_lo_hz": lo,
                "clamped_lo_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
                "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ, "upper_hz": hi,
            },
        )
    return _CloudEchoBand(
        band_hz=(lo, hi),
        source="declared" if declared else "undeclared_default",
        hf_regime_clamped=False,
        derived_lo_hz=lo,
    )


def _decimate_curve_for_json(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> dict[str, list[float]]:
    """Stride-decimate one combined curve to at most
    :data:`CLOUD_CURVE_MAX_JSON_POINTS`, for disclosure only.

    A plain stride is safe here, unlike in ``durable_state._decimate_sum``,
    because this input (``combined.power_mean_spec_db``) has already been
    through ``smooth_fractional_octave`` inside :func:`combine_positions`; a
    stride over a raw unsmoothed prediction aliases below ~500 Hz (#1858).
    """
    n = len(freqs_hz)
    step = max(1, (n + CLOUD_CURVE_MAX_JSON_POINTS - 1) // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }


def cloud_validity_floor_hz(positions: Sequence[_CloudPosition]) -> float | None:
    """The group's own gated validity floor — the WORST (highest) of its
    positions' floors, or ``None`` when no position reported a usable one.

    The worst rather than a mean: the combined curve is a power mean ACROSS
    these positions, so a bin below any one position's reflection-gate floor is
    contaminated by that position's truncated-window artifact. The highest floor
    is the only choice under which every graded bin is inside every contributing
    capture's validity.

    ``None`` means the lower edge could not be verified, NOT that it is zero;
    callers disclose it as unknown and clamp nothing.
    """
    floors = [
        float(getattr(p.response, "validity_floor_hz", None) or 0.0)
        for p in positions
    ]
    usable = [f for f in floors if math.isfinite(f) and f > 0.0]
    return max(usable) if usable else None


def cloud_entanglement_floor_hz(
    per_position: Sequence[tuple[Any, Any]],
) -> EntanglementFloor:
    """The group's ROOM floor and its provenance — the WORST of its positions'.

    :func:`cloud_trusted_floor_hz`'s argument applied to the floor no window
    choice can lower (#3495): the MAX is the only floor under which every marked
    bin is marked at every contributing capture.

    One position that does not know its floor un-knows the group's — a max over
    the seats that DID know would claim the silent seat is cleaner. Empty in,
    unknown out.

    The source is the WEAKEST of the pooled provenances: a group is ``measured``
    only when every seat's floor was timed off its own reflection, and one
    declared seat makes the aggregate ``declared``. Anything outside
    :data:`~jasper.audio_measurement.gating.ENTANGLEMENT_SOURCES` is unknown.
    Each seat is read through the lenient door, because a seat's pair comes off
    a persisted position row.
    """
    seats = [
        EntanglementFloor.coerce(floor_hz, source) for floor_hz, source in per_position
    ]
    known = [seat.hz for seat in seats if seat.hz is not None]
    if not seats or len(known) != len(seats):
        return EntanglementFloor.unknown()
    return EntanglementFloor(
        max(known),
        ENTANGLEMENT_SOURCE_MEASURED
        if all(seat.source == ENTANGLEMENT_SOURCE_MEASURED for seat in seats)
        else ENTANGLEMENT_SOURCE_DECLARED,
    )


@dataclass(frozen=True)
class CloudGroupResult:
    """:func:`assemble_cloud_group_result`'s payload, plus the line a failure earns.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_cloud_pipeline_failed``, ``None`` when there
    was nothing to say, as data because this module is side-effect-free.
    """

    result: dict[str, Any]
    diagnostics: dict[str, Any] | None = None


def assemble_cloud_group_result(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    echo_band_provenance: Mapping[str, Any] | None = None,
    validity_floor_hz: float | None = None,
    trusted_ceiling_hz: float | None = None,
    position_records: Sequence[Mapping[str, Any]] = (),
    crossover_region_hz: tuple[float, float] | None = None,
    graded_spec_sink: Callable[[Any], None] | None = None,
) -> CloudGroupResult:
    """THE single function that consumes the exclusion mask,
    ``geometry.locked`` and the null registry TOGETHER.

    No other code may read ``combined.excluded`` / ``combined.geometry.locked``
    and treat that as the honesty verdict: the mask alone is a hole. This runs
    :func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    at ``echo_band_hz``, unions its excluded bins with the combiner's own
    power-vs-median screen, and evaluates
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` against the
    merged mask. ``combined`` may be ``None`` — the group could not be combined.

    The ``spec`` report built here is the SSOT: every spec-facing surface renders
    :func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge` of this report
    rather than deriving a number, and nothing downstream re-evaluates the
    curve. ``carve_outs`` is a third reading of that same evaluation, never a
    second one — the bins are already gone from ``spec`` and no verdict here can
    move. The tolerance table is untouched: the decision was to disclose the
    carve-out, not to re-spec the band.

    ``echo_band_provenance`` is how a payload reader tells a contract-derived
    band from a clamped one (#1763), since the published ``echo_band_hz`` alone
    cannot say which. :meth:`_CloudEchoBand.disclosure` supplies the block;
    ``None`` means "not stated", never "not clamped".

    The spec is graded above the group's TRUSTED floor, not its validity floor
    (#2551): :func:`cloud_trusted_floor_hz` turns the group's ``1/T`` into the
    ``2.5/T`` the gate's delta probe already refuses to grade below, and
    ``evaluate_flat_spec`` intersects every band's lower edge with it — the
    reference band included, since a bin the gate cannot support must not
    re-centre the target. Both floors are published. Three properties this
    keeps:

    * The intersection is a band EDGE, not a mask entry, so ``spec.n_excluded``
      stays exactly the honesty instruments' count and a gate artifact cannot
      inflate it. Each band discloses ``graded_lo_hz``/``graded_hi_hz`` beside
      its nominal row.
    * A band left entirely outside the trusted range is ``evaluable=False``,
      never ``within_target=False`` — there is no evidence there, which is not
      a failure. ``overall_within_target`` still treats unevaluable as not-
      within-target.
    * A ``None`` floor or ceiling clamps NOTHING and is reported as ``None``,
      rather than withholding the evidence above an unverified edge.

    Clamping is not free and moves the headline in the FLATTERING direction on
    a corpus whose sub-floor region is loud — on S0 it re-centres the reference
    by -4.55 dB and flips the 250 Hz-2 kHz band verdict
    (``test_the_trusted_floor_clamp_costs_the_low_band`` pins the figures). The
    direction is response-shape dependent, not a property of the clamp: do not
    generalize the sign. None of it is the speaker improving — it is the same
    speaker graded on fewer bins, which is what ``n_bins`` keeps visible. One
    short gate in a group is therefore expensive by design, since the group
    takes the WORST position's floor; per-position per-bin masking inside
    ``combine_positions`` would be strictly better and is a
    ``spatial_combine`` estimator change rather than a wiring one.

    Fail-soft over a NAMED family: exactly
    ``(ValueError, TypeError, IndexError, AttributeError)``, the documented
    raise surface of every callee. A downstream DSP failure here is
    diagnostic machinery, never a capture-accept gate, so it is reported as
    ``available: False``. Any other exception propagates uncaught.
    """
    if combined is None:
        return CloudGroupResult({"available": False, "reason": "combine_failed"})
    try:
        null_report = identify_interference_nulls(combined, band_hz=echo_band_hz)
        crossover_registry = _crossover_region_null_registry(
            combined, echo_band_hz=echo_band_hz,
            crossover_region_hz=crossover_region_hz,
            identify=identify_interference_nulls,
        )
        merged_mask = np.asarray(combined.excluded, dtype=bool) | np.asarray(
            null_report.excluded, dtype=bool
        )
        # ``crossover_registry`` is deliberately absent from this union: see
        # its builder for why classification there may never become gating. The
        # mask handed to the evaluator is EXACTLY what the honesty instruments
        # found; the gate's floor rides beside it as a band-edge intersection
        # instead (#2551).
        trusted_floor_hz = cloud_trusted_floor_hz(validity_floor_hz)
        # The ROOM's floor is pooled from the same seats the curve was pooled
        # from (#3502). It CLAMPS NOTHING and changes no grade — it only lets
        # every band say which of its bins no window could have separated from
        # the room.
        entanglement = cloud_entanglement_floor_hz(
            [
                (
                    row.get("gate_entanglement_floor_hz"),
                    row.get("gate_entanglement_floor_source"),
                )
                for row in position_records
            ]
        )
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
            trusted_floor_hz=trusted_floor_hz,
            trusted_ceiling_hz=trusted_ceiling_hz,
            entanglement_floor_hz=entanglement.hz,
            entanglement_floor_source=entanglement.source,
        )
        # Hand the LIVE report to a caller that needs the object rather than
        # the serialized copy below: ``to_dict`` flattens away
        # ``overall_within_target`` and each band's ``evaluable``/``within_target``, and
        # re-evaluating from ``combined`` would be a second owner of the merged
        # honesty mask. A sink rather than a second return value, because every
        # other caller reads the dict.
        if graded_spec_sink is not None:
            # The curve, the mask and the verdict as ONE record: this is the
            # only place all three exist together, and handing them over
            # separately would let a consumer pair a curve with a mask from a
            # different evaluation.
            graded_spec_sink(GradedSpec(
                combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
                spec_report,
            ))
        geometry_dict = {
            "locked": bool(combined.geometry.locked),
            "reason": str(combined.geometry.reason),
            "n_confident": int(combined.geometry.n_confident),
            "n_positions": int(combined.geometry.n_positions),
            "median_tau_us": float(combined.geometry.median_tau_us),
            "clustered_fraction": float(combined.geometry.clustered_fraction),
            "thin_evidence": bool(combined.geometry.thin_evidence),
        }
        return CloudGroupResult({
            "available": True,
            "geometry": geometry_dict,
            "geometry_guidance": _geometry_guidance_copy(geometry_dict),
            "screen_excluded_bands_hz": [
                list(b) for b in combined.excluded_bands_hz
            ],
            "merged_excluded_bands_hz": [
                list(b) for b in merged_true_intervals(combined.freqs_hz, merged_mask)
            ],
            "null_registry": _null_registry_to_dict(null_report),
            # The crossover region, ASKED. Classification only, never unioned
            # into any mask above. ``None`` when there is no committed crossover
            # to name a region with, or when the gating band already reached it.
            "null_registry_crossover_region": crossover_registry,
            "spec": spec_report.to_dict(),
            # The SAME registry and spec report above, re-read per band. Not a
            # second evaluation: those bins are already gone.
            "carve_outs": carve_outs_by_band(
                spec_report, null_report, combined.excluded_bands_hz,
            ),
            # A pure reduction of the SAME ``spec`` report above, carried here
            # so no downstream surface derives its own.
            "flatness": spec_flatness_gauge(spec_report).to_dict(),
            "validity_floor_hz": (
                float(validity_floor_hz)
                if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
                else None
            ),
            # The floor the spec was graded above — 2.5x the one directly
            # above. Published beside its input so a reader sees both the
            # window's resolution limit and its trust limit (#2551).
            "trusted_floor_hz": trusted_floor_hz,
            # The floor is always available; the ceiling is read off the bound
            # candidate's mic tier and is ``None`` on a pre-apply close with no
            # candidate. A session can therefore grade its MEASURE group and its
            # VERIFY group over different spans, which publishing this here
            # makes visible.
            "trusted_ceiling_hz": spec_report.trusted_ceiling_hz,
            "echo_band_hz": list(echo_band_hz),
            "echo_band_provenance": (
                dict(echo_band_provenance)
                if isinstance(echo_band_provenance, Mapping)
                else None
            ),
            "curve": _decimate_curve_for_json(
                combined.freqs_hz, combined.power_mean_spec_db,
            ),
            # The MEMBERS behind every aggregate above — serialization only:
            # no new signal, no threshold, no verdict. Never raises, so it
            # cannot turn a good group into a failed one.
            "positions": position_evidence_block(
                combined,
                position_records=position_records,
                validity_floor_hz=validity_floor_hz,
            ),
        })
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return CloudGroupResult(
            {"available": False, "reason": "pipeline_failed"},
            {"error": str(exc)},
        )
