# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Flat-linearization plan PR-5: the spec-curve single source of truth.

The failure class this module exists to prevent is the one the plan's "S0
executed" § c documents: two spec-facing numbers for one session, derived by
two code paths from two curves, disagreeing — and nobody able to say which is
"the measurement". PR-5 answers that by construction: ``combine_positions``'
power-mean spec curve, evaluated once by ``evaluate_flat_spec`` against the
merged honesty mask, reduced once by ``spec_flatness_gauge``, and COPIED to
every surface.

Two layers, mirroring ``test_crossover_v2_cloud_pipeline.py``:

* **Synthetic (always runs).** The gauge's own lifted-from-the-report
  contract; the WIRING half of the trusted-floor clamp's contract (which
  floor the assembler derives and publishes, and what it does to the payload
  — the evaluator's own arithmetic is ``tests/test_flat_spec.py``'s); and the
  frame-consistency walk — pipeline result → durable ``cloud`` block →
  ``compact_cloud_status`` → ``/state`` → the envelope's rendered ledger line
  — asserted byte-identical at every hop.
* **Corpus-gated.** The same walk on the real S0 main-leg cloud, so the
  contract is pinned against hardware data and the S0 session's own measured
  regime — including what its own ``2.5/T`` floor costs the low band — is
  stated with numbers rather than assumed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
)
from jasper.active_speaker.crossover_v2.spatial import (
    cloud_entanglement_floor_hz,
    cloud_trusted_floor_hz,
    cloud_validity_floor_hz,
)
from jasper.active_speaker.crossover_v2_flow import assemble_cloud_group_result
from jasper.active_speaker.flat_spec import (
    REFERENCE_BAND_HZ,
    SPEC_BANDS,
    evaluate_flat_spec,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.spatial_combine import PositionCapture, combine_positions
from jasper.active_speaker.crossover_envelope_v2 import (
    chart_cloud_status,
    compact_cloud_status,
)

from tests import _flat_lin_corpus as corpus

SAMPLE_RATE = 48_000
N_FFT = 4096
SYNTHETIC_BAND_HZ = (4000.0, 19_000.0)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _two_path_ir(delay_samples: int, r: float, *, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ir = rng.normal(0.0, 1e-7, N_FFT)
    ir[100] += 1.0
    if r != 0.0:
        ir[100 + delay_samples] += r
    return ir


def _locked_cloud(n: int = 6) -> list[PositionCapture]:
    """A position-invariant two-path cloud — the same shape
    ``test_crossover_v2_cloud_pipeline.py`` builds, so the two modules
    exercise one pipeline on one fixture family."""
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    captures = []
    for k in range(n):
        ir = _two_path_ir(15, 0.37, seed=2000 + k)
        magnitude = 20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(ir, N_FFT)), 1e-12))
        captures.append(
            PositionCapture(
                position_id=f"p{k:02d}", freqs_hz=freqs, magnitude_db=magnitude,
                sample_rate=SAMPLE_RATE, ir=ir,
            )
        )
    return captures


def _report(curve_db: np.ndarray, freqs_hz: np.ndarray, mask=None):
    return evaluate_flat_spec(freqs_hz, curve_db, mask)


def _tilted_report(*, slope_db: float = -9.0, mask=None):
    """A synthetic report with a real, locatable worst bin per band."""
    freqs = np.geomspace(100.0, 20_000.0, 2000)
    curve = slope_db * np.log10(freqs / 100.0) / np.log10(200.0)
    return _report(curve, freqs, mask)


class _FakeResponse:
    def __init__(self, floor):
        self.validity_floor_hz = floor


class _FakePosition:
    def __init__(self, floor):
        self.response = _FakeResponse(floor)


# --------------------------------------------------------------------------- #
# the gauge lifts, it never recomputes
# --------------------------------------------------------------------------- #


def test_every_gauge_figure_is_a_figure_from_the_report():
    """The SSOT property at its smallest: no field on the gauge is a new
    computation. Each one is traced back to the exact report field it came
    from, so a future edit that starts deriving a number here fails."""
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)
    residual = spec_convergence_residual(report)

    worst = max(
        (b for b in report.bands if b.evaluable),
        key=lambda b: abs(b.max_deviation_db),
    )
    assert gauge.max_db == worst.max_deviation_db
    assert gauge.max_hz == worst.max_deviation_hz
    assert gauge.max_band_hz == (worst.f_lo_hz, worst.f_hi_hz)
    assert gauge.tolerance_db == worst.tolerance_db
    assert gauge.rms_db == residual.rms_db
    assert gauge.n_bins == residual.n_bins
    assert gauge.n_excluded == residual.n_excluded
    assert gauge.passed == report.overall_passed
    assert gauge.evaluable is True


def test_the_gauge_names_the_frame_its_worst_band_is_stated_against():
    """#1857 — a worst-band pointer without its reference frame is half a
    claim, and the missing half is the one that decides which driver gets
    blamed.

    Every deviation on every spec surface is ``curve - reference_db``, where
    ``reference_db`` is a power mean pooled over ``REFERENCE_BAND_HZ``
    (250 Hz-8 kHz). On the 2026-07-30 corpus a dark tweeter pulls that
    full-range mean ~2.7 dB below a woofer-anchored one, and the SAME
    persisted curve's 250-2000 Hz pointer reads +5.44 dB @ 428 Hz in the
    shipped frame but -5.86 dB @ 1901 Hz woofer-anchored — a sign flip and a
    different band. The gauge carried the pointer and not the frame, so no
    reader downstream could tell which of those two readings they had.

    WHICH frame should win is a separate, deliberately open question (Q-E).
    This pins only that the gauge states the one it used.
    """
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)

    assert gauge.reference_band_hz == REFERENCE_BAND_HZ
    assert gauge.to_dict()["reference_band_hz"] == list(REFERENCE_BAND_HZ)
    # It is not the band the worst bin lives in — conflating the two is the
    # mistake the issue is about.
    assert gauge.reference_band_hz != gauge.max_band_hz


def test_the_frame_is_named_even_when_no_band_could_be_graded():
    """#1857 — which frame WOULD have been used is knowable even when the
    gauge is unevaluable, and a reader comparing two sessions needs it
    either way."""
    from dataclasses import replace

    report = _tilted_report()
    # Same hand-built every-band-lost-its-bins shape the unevaluable-gauge
    # test above uses — evaluate_flat_spec cannot produce it directly.
    blanked = replace(
        report,
        bands=tuple(
            replace(
                b, evaluable=False, passed=None, max_deviation_db=None,
                max_deviation_hz=None, rms_deviation_db=None,
                n_excluded=b.n_bins,
            )
            for b in report.bands
        ),
        overall_passed=False,
    )
    gauge = spec_flatness_gauge(blanked)

    assert gauge.evaluable is False
    assert gauge.max_band_hz is None
    assert gauge.reference_band_hz == REFERENCE_BAND_HZ


def test_the_gauge_keeps_the_sign_of_the_worst_bin():
    """``BandResult.max_deviation_db``'s own rule — "2.4 dB too loud" and
    "2.4 dB too quiet" call for opposite corrections — survives the
    reduction. A gauge that took an absolute value here would hide which.

    The two slopes are NOT mirror images of each other in magnitude, and the
    test deliberately does not claim they are: the reference level is a POWER
    mean over the tight bands, which is not symmetric under a sign flip of a
    log-frequency tilt. Only the sign is the contract."""
    for slope, expect_negative in ((-9.0, True), (+9.0, False)):
        report = _tilted_report(slope_db=slope)
        gauge = spec_flatness_gauge(report)
        assert (gauge.max_db < 0.0) is expect_negative
        worst = max(
            (b for b in report.bands if b.evaluable),
            key=lambda b: abs(b.max_deviation_db),
        )
        assert gauge.max_db == worst.max_deviation_db


def test_the_worst_band_is_chosen_by_absolute_dB_not_tolerance_headroom():
    """Deliberately NOT "the band that failed by the widest margin relative to
    its own tolerance": the rendered claim is a dB reading of how far from
    flat the speaker measured. Here the 8-16 kHz band is worst in dB while
    every band is out of spec, so a tolerance-relative ranking could pick a
    different one; the gauge must report the dB-worst."""
    report = _tilted_report()
    gauge = spec_flatness_gauge(report)
    worst_by_db = max(
        (b for b in report.bands if b.evaluable), key=lambda b: abs(b.max_deviation_db)
    )
    assert gauge.max_band_hz == (worst_by_db.f_lo_hz, worst_by_db.f_hi_hz)


def test_an_exact_tie_between_bands_resolves_to_the_lowest_band():
    """Determinism, not dict order: two bands equally far from flat must pick
    the same one every run. ``SPEC_BANDS`` is ordered low-to-high and the scan
    uses a strict ``>``, so the lowest wins."""
    from dataclasses import replace

    report = _tilted_report()
    tied = replace(
        report,
        bands=tuple(
            replace(b, max_deviation_db=-3.0, max_deviation_hz=b.f_lo_hz + 1.0)
            if b.evaluable else b
            for b in report.bands
        ),
    )
    gauge = spec_flatness_gauge(tied)
    assert gauge.max_band_hz == (SPEC_BANDS[0][0], SPEC_BANDS[0][1])
    assert gauge.max_db == -3.0


def test_the_gauge_is_unevaluable_never_a_fabricated_zero_when_all_bins_are_masked():
    """Every spec-band bin excluded ⇒ ``evaluable=False`` and ``None`` metrics.
    ``passed`` is False there too (``FlatSpecReport.overall_passed``'s own
    "will not report a clean bill of health" rule), which is exactly why a
    renderer must read the two together — pinned so a future reader does not
    mistake this state for a failing speaker."""
    freqs = np.geomspace(100.0, 20_000.0, 2000)
    curve = np.zeros_like(freqs)
    # Mask every spec-band bin but leave the reference band a foothold below
    # its own edge is impossible (the reference band IS two spec bands), so
    # mask everything at or above 2 kHz and let 250 Hz-2 kHz carry the
    # reference — then assert the two upper bands are unevaluable.
    mask = freqs >= SPEC_BANDS[1][0]
    gauge = spec_flatness_gauge(_report(curve, freqs, mask))
    assert gauge.evaluable is True  # band 1 survived
    assert gauge.max_band_hz == (SPEC_BANDS[0][0], SPEC_BANDS[0][1])

    # Now the genuinely-unevaluable case, built directly on a report whose
    # every band lost its bins: an axis that never reaches the spec bands is
    # rejected by evaluate_flat_spec (no reference), so this is constructed
    # from a hand-built report instead — the same corner
    # ``spec_convergence_residual``'s own docstring calls unreachable from
    # ``evaluate_flat_spec`` and guards anyway.
    from dataclasses import replace

    report = _report(curve, freqs)
    blanked = replace(
        report,
        bands=tuple(
            replace(
                b, evaluable=False, passed=None, max_deviation_db=None,
                max_deviation_hz=None, rms_deviation_db=None,
                n_excluded=b.n_bins,
            )
            for b in report.bands
        ),
        overall_passed=False,
    )
    blank_gauge = spec_flatness_gauge(blanked)
    assert blank_gauge.evaluable is False
    assert blank_gauge.max_db is None
    assert blank_gauge.max_hz is None
    assert blank_gauge.max_band_hz is None
    assert blank_gauge.tolerance_db is None
    assert blank_gauge.rms_db is None
    assert blank_gauge.n_bins == 0
    assert blank_gauge.passed is False  # read WITH evaluable, never alone


# --------------------------------------------------------------------------- #
# the validity-floor clamp
# --------------------------------------------------------------------------- #


def test_cloud_validity_floor_is_the_worst_position_not_an_average():
    """One collapsed gate contaminates the power mean at every bin below its
    own floor, so the honest clamp is the HIGHEST floor in the group — the
    same "worse (higher) of the two" rule ``_measure_validity_floor_hz``
    already applies to the two driver branches."""
    assert cloud_validity_floor_hz(
        [_FakePosition(142.9), _FakePosition(1777.8), _FakePosition(142.9)]
    ) == pytest.approx(1777.8)


def test_cloud_validity_floor_is_none_when_no_position_reports_one():
    assert cloud_validity_floor_hz([_FakePosition(None), _FakePosition(0.0)]) is None
    assert cloud_validity_floor_hz([]) is None


def test_the_trusted_floor_not_the_validity_floor_is_what_grades():
    """#2551: the spec is intersected at ``2.5/T``, not ``1/T``.

    The defect this pins out: the evaluator used to be handed the group's
    VALIDITY floor, which on every real JTS3 gate sits below the spec
    table's own 250 Hz edge and therefore clamped nothing, while the same
    session's gate disclosure and delta probe both refused to grade below
    ``2.5/T``. One capture, two graders, two honesty floors.

    A 142.86 Hz validity floor — the exact number a 7 ms gate produces, and
    the one every S0 position reports — is below 250 Hz, so under the old
    rule it changed nothing. Its trusted floor is 357.14 Hz, which is ABOVE
    the table's edge and does move the low band."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=142.857,
    )
    assert clamped["validity_floor_hz"] == pytest.approx(142.857)
    assert clamped["trusted_floor_hz"] == pytest.approx(357.1425)
    # The floor the old code clamped at is below the table edge; the one this
    # test asserts on is above it. That gap IS the defect.
    assert 142.857 < SPEC_BANDS[0][0] < clamped["trusted_floor_hz"]

    low_before = unclamped["spec"]["bands"][0]
    low_after = clamped["spec"]["bands"][0]
    assert low_before["graded_lo_hz"] == SPEC_BANDS[0][0]
    assert low_after["graded_lo_hz"] == pytest.approx(357.1425)
    # Fewer bins IN the band — a band-edge move, not a mask entry, so the
    # interference count is untouched (see the carve-out separation).
    assert low_after["n_bins"] < low_before["n_bins"]
    assert low_after["n_excluded"] == low_before["n_excluded"]
    # ...and the reference re-centres with it (item 2 of the fix shape).
    assert clamped["spec"]["reference_db"] != unclamped["spec"]["reference_db"]
    assert clamped["spec"]["reference_band_hz"] == [
        pytest.approx(357.1425), REFERENCE_BAND_HZ[1],
    ]


def test_a_band_wholly_outside_the_trusted_range_is_unevaluable_never_failed():
    """#2551 fix-shape item 1, the honesty half, through the wiring layer. A
    band with nothing inside the trusted range has NO EVIDENCE — which is not
    a failure and not a pass. The tell that separates it from "the axis never
    reached this band" is ``graded_lo_hz >= graded_hi_hz``.

    Emptied by the CEILING: the reference band is ``SPEC_BANDS[0]`` exactly,
    so a floor high enough to swallow the low band swallows the frame with it
    and the pipeline reports ``available: False`` rather than a report with an
    unevaluable band. Which clamp emptied it is not the claim.

    ``overall_passed`` still reads False, by ``FlatSpecReport``'s own "will
    not report a clean bill of health for a spectrum it could not fully
    measure" rule, so nothing is flattered by the distinction."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    # A ceiling at the top band's own lower edge takes that band whole, and
    # not one bin more.
    clamped = assemble_cloud_group_result(
        combined,
        echo_band_hz=SYNTHETIC_BAND_HZ,
        trusted_ceiling_hz=SPEC_BANDS[2][0],
    )
    top = clamped["spec"]["bands"][2]
    assert top["f_hi_hz"] == SPEC_BANDS[2][1]  # the NOMINAL row is still named
    assert top["graded_hi_hz"] == pytest.approx(SPEC_BANDS[2][0])
    assert top["graded_lo_hz"] >= top["graded_hi_hz"]
    assert top["evaluable"] is False
    assert top["passed"] is None  # never False
    assert top["n_bins"] == 0
    assert top["max_deviation_db"] is None
    assert clamped["spec"]["overall_passed"] is False
    # The bands INSIDE the trusted range are graded normally.
    assert clamped["spec"]["bands"][1]["evaluable"] is True
    # ...and the report says on its face where grading stopped.
    assert clamped["spec"]["best_effort_above_hz"] == pytest.approx(SPEC_BANDS[2][0])


def test_a_floor_below_the_spec_edge_changes_no_graded_number():
    """The clamp is an intersection, so a trusted floor beneath the table's
    own 250 Hz edge is a genuine no-op: every band figure, the reference
    level, the verdict, and the whole gauge are byte-identical.

    Since #2551 that is true of ``excluded_intervals`` too. The clamp raises
    a band EDGE rather than adding mask entries, so a sub-floor bin is never
    reported as "excluded" — the report-wide interference disclosure and the
    per-band ``n_excluded`` both stay the honesty instruments' own count, and
    the floor is disclosed as ``graded_lo_hz``/``trusted_floor_hz``
    instead."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    # 99 Hz validity => 247.5 Hz trusted, just under the 250 Hz table edge.
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=99.0,
    )
    assert clamped["trusted_floor_hz"] == pytest.approx(247.5)
    assert clamped["trusted_floor_hz"] < SPEC_BANDS[0][0]
    assert json.dumps(clamped["flatness"], sort_keys=True) == json.dumps(
        unclamped["flatness"], sort_keys=True
    )
    for key in ("bands", "reference_db", "overall_passed", "excluded_intervals"):
        assert json.dumps(clamped["spec"][key], sort_keys=True) == json.dumps(
            unclamped["spec"][key], sort_keys=True
        ), key
    assert clamped["validity_floor_hz"] == pytest.approx(99.0)


def test_the_cloud_report_publishes_its_seats_pooled_room_floor_and_grades_the_same(
):
    """#3502 — the aggregate reaches the graded report, and moves no verdict.

    The seats' floors are pooled by ``cloud_entanglement_floor_hz`` and echoed
    on the report with the provenance they were pooled under. The floor MARKS
    and never clamps, so every band, the reference level and the verdict are
    byte-identical to the same combine with no declaration at all — which is
    what makes wiring it safe to land on a rig mid-campaign.
    """
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    rows = [
        {"gate_entanglement_floor_hz": 400.0,
         "gate_entanglement_floor_source": _DECLARED},
        {"gate_entanglement_floor_hz": 610.0,
         "gate_entanglement_floor_source": _DECLARED},
    ]
    declared = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, position_records=rows,
    )
    # A round banked before the writers existed carries neither key.
    undeclared = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, position_records=[{}, {}],
    )

    assert declared["spec"]["entanglement_floor_hz"] == pytest.approx(610.0)
    assert declared["spec"]["entanglement_floor_source"] == _DECLARED
    assert undeclared["spec"]["entanglement_floor_hz"] is None
    assert undeclared["spec"]["entanglement_floor_source"] == _UNKNOWN
    for key in ("bands", "reference_db", "overall_passed", "excluded_intervals"):
        assert json.dumps(
            _without_room_marks(declared["spec"][key]), sort_keys=True
        ) == json.dumps(
            _without_room_marks(undeclared["spec"][key]), sort_keys=True
        ), key
    assert json.dumps(declared["flatness"], sort_keys=True) == json.dumps(
        undeclared["flatness"], sort_keys=True
    )


def _without_room_marks(value):
    """Strip the one field the room floor is ALLOWED to move, so the rest of
    the report can be compared for equality."""
    if isinstance(value, list):
        return [_without_room_marks(v) for v in value]
    if isinstance(value, dict):
        return {
            k: _without_room_marks(v)
            for k, v in value.items()
            if k != "room_entangled_below_hz"
        }
    return value


def test_no_floor_clamps_nothing_and_is_disclosed_as_unknown():
    """``None`` is "the lower edge could not be verified", not zero and not a
    withheld gauge — the whole 2-16 kHz evidence would otherwise be thrown
    away over an unknown floor. Both published floors say so."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result["validity_floor_hz"] is None
    assert result["trusted_floor_hz"] is None
    assert result["spec"]["trusted_floor_hz"] is None
    assert result["flatness"]["evaluable"] is True
    assert result["spec"]["bands"][0]["graded_lo_hz"] == SPEC_BANDS[0][0]


def test_the_trusted_floor_is_two_and_a_half_over_T_and_unknown_stays_unknown():
    """``cloud_trusted_floor_hz`` is ``2.5 * f_valid``, the same multiply
    :func:`jasper.audio_measurement.gating.f_trusted_floor_hz` performs, read
    from that module so the two cannot drift.

    "No floor" propagates as ``None`` — never as zero, which would read as a
    clamp at DC and silently grade everything."""
    assert cloud_trusted_floor_hz(142.857) == pytest.approx(
        TRUSTED_FLOOR_MULTIPLIER * 142.857
    )
    assert cloud_trusted_floor_hz(142.857) == pytest.approx(357.1425)
    assert cloud_trusted_floor_hz(None) is None
    assert cloud_trusted_floor_hz(0.0) is None
    assert cloud_trusted_floor_hz(-5.0) is None
    assert cloud_trusted_floor_hz(float("inf")) is None
    assert cloud_trusted_floor_hz(float("nan")) is None


_MEASURED = "measured_reflection"
_DECLARED = "declared_geometry"
_UNKNOWN = "unknown"


@pytest.mark.parametrize(
    ("per_position", "expected"),
    [
        pytest.param(
            [(400.0, _DECLARED), (610.0, _DECLARED)], (610.0, _DECLARED),
            id="all-declared-takes-the-worst",
        ),
        pytest.param(
            [(400.0, _MEASURED), (610.0, _MEASURED)], (610.0, _MEASURED),
            id="all-measured-stays-measured",
        ),
        pytest.param(
            [(610.0, _MEASURED), (400.0, _DECLARED)], (610.0, _DECLARED),
            id="one-declared-seat-makes-the-pool-declared",
        ),
        pytest.param(
            [(400.0, _DECLARED), (None, _UNKNOWN)], (None, _UNKNOWN),
            id="one-unknown-seat-un-knows-the-group",
        ),
        pytest.param([], (None, _UNKNOWN), id="no-seats-know-nothing"),
        pytest.param(
            [(400.0, _DECLARED), (610.0, "surveyed")], (None, _UNKNOWN),
            id="a-seat-whose-pair-cannot-be-true-is-unknown",
        ),
    ],
)
def test_the_groups_room_floor_is_the_worst_seats_and_the_weakest_provenance(
    per_position, expected
):
    """``cloud_entanglement_floor_hz`` — ``cloud_trusted_floor_hz``'s "worst of
    the positions" argument applied to the floor no window choice can lower.

    The combined curve is a power mean ACROSS these seats, so a bin below any
    one seat's floor is room-entangled in the average: the MAX is the only
    floor under which every marked bin is marked everywhere. One seat that does
    not know its floor un-knows the group's, because a max over the seats that
    DID know would claim the silent one is cleaner than the rest. And the
    source is the WEAKEST of the pool: one declared seat makes the aggregate
    declared, since that is what a reader would have to assume about it.

    Each seat is read through ``gating.EntanglementFloor.coerce``, which is
    where "a pair that cannot be true is unknown" is pinned; what this pins is
    the POOLING.
    """
    floor = cloud_entanglement_floor_hz(per_position)
    assert (floor.hz, floor.source) == expected


def test_the_floor_clamp_never_inflates_the_interference_interval_count():
    """``merged_excluded_bands_hz`` (and so `/state`'s
    ``excluded_interval_count``) is the honesty instruments' own finding —
    "how much interference did we find". A gate artifact is a different fact
    and must not be silently counted as interference."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    # 500 Hz validity -> 1250 Hz trusted: comfortably below the reference
    # band's own 2000 Hz top (a trusted floor at or above it empties the
    # reference band itself and raises -- see test_flat_spec.py's
    # test_a_floor_at_or_above_the_reference_band_top_raises), while still
    # clamping real bins out of band1's low end.
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=500.0,
    )
    assert (
        clamped["merged_excluded_bands_hz"] == unclamped["merged_excluded_bands_hz"]
    )
    assert (
        clamped["screen_excluded_bands_hz"] == unclamped["screen_excluded_bands_hz"]
    )


# --------------------------------------------------------------------------- #
# THE frame-consistency contract
# --------------------------------------------------------------------------- #


def _durable_cloud_block(result, *, phase: str = PHASE_CLOUD_VERIFY) -> dict:
    """The shape ``_cloud_summary`` writes into the durable v2 state."""
    return {
        phase: {
            "geometry": result.get("geometry") or {},
            "positions": [],
            "pipeline": result,
        }
    }


def _walk_every_surface(result, monkeypatch) -> dict:
    """One pipeline result → EVERY spec-facing surface's view of it.

    Walks the REAL functions in the real order the host uses:
    ``assemble_cloud_group_result`` → ``_cloud_summary``'s durable shape →
    ``compact_cloud_status`` (`/state`) → ``build_crossover_envelope_v2``
    (the wizard envelope + its rendered ledger line) → the shipped doctor
    check (N-1: the doctor is a spec-facing surface too, so "every surface"
    has to include it rather than be quietly scoped to three).

    The doctor reads through ``crossover_v2_status_block`` (its own import,
    resolved at call time), so it is reached by patching the durable-state
    loader underneath it — the same seam PR-4's own doctor corpus test uses
    — rather than by handing it a pre-built block.
    """
    compact = compact_cloud_status(_durable_cloud_block(result))
    chart = chart_cloud_status(_durable_cloud_block(result))
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done",
            "verify": {"outcome": "pass"},
            "cloud": compact,
            "cloud_chart": chart,
        },
    })

    from jasper.cli.doctor import correction as doctor_correction
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host, "load_v2_state", lambda: {"cloud": _durable_cloud_block(result)}
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    doctor = doctor_correction.check_crossover_v2_cloud_pipeline()

    return {
        "pipeline": result["flatness"],
        "state": compact[PHASE_CLOUD_VERIFY]["flatness"],
        "envelope": envelope["cloud"][PHASE_CLOUD_VERIFY]["flatness"],
        "ledger_lines": envelope["expert_details"],
        "state_overall_passed": compact[PHASE_CLOUD_VERIFY]["overall_passed"],
        "state_validity_floor_hz": compact[PHASE_CLOUD_VERIFY]["validity_floor_hz"],
        "state_spec_bands": compact[PHASE_CLOUD_VERIFY]["spec_bands"],
        "doctor_status": doctor.status,
        "doctor_reason": doctor.reason,
        "spec": result["spec"],
        "pipeline_validity_floor_hz": result.get("validity_floor_hz"),
        # PR-7: the tolerance-corridor reference, and the chart's own curve
        # feed — same "one construction, copied everywhere" contract.
        "state_reference_db": compact[PHASE_CLOUD_VERIFY]["reference_db"],
        "envelope_reference_db": envelope["cloud"][PHASE_CLOUD_VERIFY]["reference_db"],
        "state_cloud_chart_curve": chart[PHASE_CLOUD_VERIFY]["curve"],
        "envelope_cloud_chart_curve": (
            envelope["cloud_chart"][PHASE_CLOUD_VERIFY]["curve"]
        ),
        "pipeline_curve": result.get("curve"),
    }


def _assert_one_number_everywhere(views: dict) -> None:
    """The contract: gauge, ledger, spec report, doctor, and the VERIFY-phase
    flatness block are the SAME bytes, from one construction."""
    canonical = json.dumps(views["pipeline"], sort_keys=True)
    assert json.dumps(views["state"], sort_keys=True) == canonical
    assert json.dumps(views["envelope"], sort_keys=True) == canonical

    spec = views["spec"]
    gauge = views["pipeline"]
    # The gauge's own figures are the spec report's figures — not merely
    # equal-looking, but the exact band entry they were lifted from.
    worst = next(
        b for b in spec["bands"]
        if b["evaluable"] and b["max_deviation_db"] == gauge["max_db"]
    )
    assert gauge["max_hz"] == worst["max_deviation_hz"]
    assert gauge["max_band_hz"] == [worst["f_lo_hz"], worst["f_hi_hz"]]
    assert gauge["tolerance_db"] == worst["tolerance_db"]
    assert gauge["passed"] == spec["overall_passed"]
    assert views["state_overall_passed"] == spec["overall_passed"]

    # SF-2: the clamp is separable from interference on the LIVE surface, not
    # only in the durable state — otherwise a reader seeing a large
    # n_excluded cannot tell a combed room from a collapsed gate.
    assert views["state_validity_floor_hz"] == views["pipeline_validity_floor_hz"]

    # N-3: per-band numbers reach `/state` verbatim, so a chart never needs a
    # second derivation to label a band.
    assert [b["max_deviation_db"] for b in views["state_spec_bands"]] == [
        b["max_deviation_db"] for b in spec["bands"]
    ]
    assert [b["passed"] for b in views["state_spec_bands"]] == [
        b["passed"] for b in spec["bands"]
    ]

    # And the household-facing line prints those digits, not a re-derivation.
    rendered = " ".join(views["ledger_lines"])
    assert f"{gauge['max_db']:+.2f} dB" in rendered
    assert f"{gauge['max_hz']:.0f} Hz" in rendered
    assert f"{gauge['rms_db']:.2f} dB" in rendered

    # N-1: the doctor's verdict is derived from the same spec verdict, not a
    # re-graded one (AGENTS.md/ADR-0232 rule 3 pins status+reason, not the
    # doctor's prose — which used to be pinned digit-for-digit here).
    from jasper.cli.doctor import correction as doctor_correction

    if spec["overall_passed"] is False:
        assert views["doctor_status"] == "warn"
        assert views["doctor_reason"] == doctor_correction.REASON_CLOUD_VERIFY_SPEC_FAILED
    else:
        assert views["doctor_status"] == "ok"
        assert views["doctor_reason"] == ""

    # PR-7: the before/after chart's own inputs are the SAME report, not a
    # fourth derivation. reference_db is the corridor's center line;
    # spec_bands already carries tolerance_db (asserted above via
    # max_deviation_db/passed) — this pins the reference alongside it.
    assert views["state_reference_db"] == spec["reference_db"]
    assert views["envelope_reference_db"] == spec["reference_db"]
    # Review S-1 (2026-07-27): the chart feed re-decimates the pipeline's own
    # 512-point ``curve`` down to its own 256-point ceiling
    # (``chart_cloud_status``'s own ``CHART_CURVE_MAX_JSON_POINTS`` —
    # measured 41,161 bytes for both phases at the full resolution, halved to
    # 20,653 by this re-decimation), so it is no longer byte-identical to the
    # pipeline curve — but every point it DOES carry must still be an actual
    # point of the pipeline curve, at the same stride
    # ``chart_cloud_status`` computes, never an interpolation or a
    # re-derivation. Stride is CEILING division (gate finding on #1858,
    # SF-1): ``decimate_curve_for_chart`` used to floor-divide, a soft
    # ceiling that could overshoot 256 by up to one stride; #1858's
    # block-average fix to the (unrelated) predicted-sum path could land a
    # persisted length just below 512, where floor division gave step=1 --
    # no reduction at all -- so the chart owner now ceiling-divides for a
    # true hard bound. This curve's own persist path
    # (``_decimate_curve_for_json``) is untouched by that fix and still
    # lands here at ~513 points, same as before; only the RE-DECIMATION
    # formula this replica must match moved.
    pipeline_freqs = views["pipeline_curve"]["freqs_hz"]
    pipeline_mags = views["pipeline_curve"]["magnitude_db"]
    n = len(pipeline_freqs)
    step = max(1, -(-n // 256))
    expected_chart_curve = {
        "freqs_hz": pipeline_freqs[:n:step],
        "magnitude_db": pipeline_mags[:n:step],
    }
    assert views["state_cloud_chart_curve"] == expected_chart_curve
    # The envelope carries the chart-feed projection through unchanged — one
    # re-decimation, not two.
    assert views["envelope_cloud_chart_curve"] == views["state_cloud_chart_curve"]


def test_the_gauge_the_ledger_the_spec_report_and_verify_are_one_number(monkeypatch):
    """THE frame-consistency contract test (plan PR-5 acceptance).

    Kills the MEASURE-vs-VERIFY ledger-discrepancy class: for one session,
    every spec-facing surface — gauge, `/state`, envelope ledger line, doctor
    detail, and the spec report itself — shows byte-identical numbers because
    there is one construction and the rest is copying.
    """
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result["available"] is True
    _assert_one_number_everywhere(_walk_every_surface(result, monkeypatch))


def test_the_pre_apply_cloud_never_supplies_the_post_apply_flatness_claim():
    """``cloud_measure`` carries its own gauge (it is the same construction on
    the same footing) but the POST-apply claim is a different claim — the same
    pre-vs-post distinction PR-4's doctor blocker drew. A pre-apply-only
    session must never report the uncorrected baseline as "how flat your
    speaker is".

    **Was ``…_never_supplies_the_household_flatness_line``, asserting
    ``expert_details == []``** — one of three places (with
    ``test_crossover_envelope_v2`` and ``test_crossover_v2_cloud_pipeline``)
    that encoded the pre-vs-post rule as SILENCE. #1965 is what that proxy
    cost: silence was also what the FULL tier showed on its own stage-1 review
    screen, where the pre-apply cloud is the only measured evidence there is
    and Express was already showing it. The rule is unchanged; it is now
    enforced by the FRAME — these numbers lead with "Measured before tuning:"
    and are never rendered bare the way the CLOUD-VERIFY path renders them.
    """
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    compact = compact_cloud_status(
        _durable_cloud_block(result, phase=PHASE_CLOUD_MEASURE)
    )
    assert compact[PHASE_CLOUD_MEASURE]["flatness"] is not None
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done", "verify": {"outcome": "pass"}, "cloud": compact,
        },
    })
    details = envelope["expert_details"]
    assert details[0].startswith("Measured before tuning: ")
    assert not any(line.startswith("flatness ") for line in details)


def test_an_unavailable_pipeline_degrades_honestly_at_every_surface():
    """Cloud-absent honesty (plan PR-5): a group that closed but could not be
    analysed reports ``None`` — never ``0``, never a spec-frame number
    invented from a construction that did not run — and the ledger says so
    in words rather than going silently blank like a session with no group."""
    result = assemble_cloud_group_result(None, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result == {"available": False, "reason": "combine_failed"}
    compact = compact_cloud_status(_durable_cloud_block(result))
    entry = compact[PHASE_CLOUD_VERIFY]
    assert entry["flatness"] is None
    assert entry["overall_passed"] is None
    assert entry["excluded_interval_count"] is None
    # PR-7: same honesty rule for the corridor reference and the chart curve.
    assert entry["reference_db"] is None
    chart = chart_cloud_status(_durable_cloud_block(result))
    assert chart[PHASE_CLOUD_VERIFY]["curve"] is None
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done", "verify": {"outcome": "pass"}, "cloud": compact,
        },
    })
    assert envelope["expert_details"] == [
        "flatness not available for this measurement — the spatial "
        "measurement could not be analysed"
    ]


# --------------------------------------------------------------------------- #
# Corpus-gated: the same contract on the real S0 main-leg cloud
# --------------------------------------------------------------------------- #


S0_ECHO_BAND_HZ = (5000.0, 19_000.0)

# The TRUSTED floor the clamp's cost is measured at, and the validity floor
# that produces it (#2551 moved the grading floor from 1/T to 2.5/T, so the
# constant a test hands the assembler is the validity one).
#
# It is an EXPLICIT constant rather than a reading taken off this corpus, and
# that changed on 2026-08-02 (#2045). Until PR #1991, ``cloud_04`` reported a
# measured reflection here and the group's own ``max()`` floor WAS 1777.8 Hz —
# but that reflection was a false early fire (the #1790 field instance #1991
# was written to fix), so the number was an artifact of the detector rather
# than a property of the room.
#
# The clamp's COST is worth pinning at a fixed floor — it is the mechanism's
# own behaviour, and it moves the headline number in the flattering direction
# — so ``test_the_trusted_floor_clamp_costs_the_low_band`` keeps measuring it
# here. Sourcing it from a constant means that guard does not depend on a
# detector bug to have a subject, and it is why the two facts have separate
# tests with separate failure names:
# ``test_the_real_s0_positions_no_longer_collapse_a_gate`` owns "every
# position gates to the same 7 ms bound", this constant owns "clamping at a
# floor this high costs exactly this".
CLAMP_TRUSTED_FLOOR_HZ = 1777.8
CLAMP_VALIDITY_FLOOR_HZ = CLAMP_TRUSTED_FLOOR_HZ / TRUSTED_FLOOR_MULTIPLIER


def _s0_combined():
    captures = corpus.s0_position_captures(corpus.S0_MAIN)
    assert len(captures) == 10
    return combine_positions(
        captures, echo_band_hz=S0_ECHO_BAND_HZ,
        signal_band_hz=corpus.S0_SUMMED_PASSBAND_HZ,
    )


@corpus.requires_s0_curves
def test_the_real_s0_session_shows_one_number_at_every_surface(monkeypatch):
    """The frame-consistency contract against hardware data, through PR-4's
    own pipeline outputs.

    Pinned figures are the S0 main leg's, measured on this exact chain (10
    positions, echo band 5-19 kHz, no validity-floor clamp — the unclamped
    case, so these are directly comparable to
    ``test_crossover_v2_cloud_pipeline.py``'s own S0 pins): worst deviation
    -11.57 dB at 15999.7 Hz in the 8-16 kHz band, pooled RMS 5.77 dB, 3074 of
    7678 spec-band bins excluded. That worst bin is the same
    comb-contaminated top-octave read the plan doc's "S0 executed" § d
    discusses and explicitly forbids quoting as "the speaker's top octave" —
    same bin, same band, though no longer the same DIGIT the plan doc quotes
    (see the second RE-PINNED note below for why).

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote re-gating
    ``cloud_04`` — see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era".
    The pooled RMS and the bin counts moved (3.7649 → 3.8031 dB, 3054/7698 →
    3074/7678) and the reference re-centred −27.2670 → −27.2386 dB. The
    HEADLINE survived to the digit the plan doc quotes: −8.9399 → −8.9389 dB,
    still at the same 15999.7 Hz bin.

    RE-PINNED AGAIN when :data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`
    narrowed from 250 Hz-8 kHz to 250 Hz-2 kHz, the low-mid band alone
    (#1857): the bin counts are untouched (still 3074 of 7678), but dropping
    the 2-8 kHz band out of the pool that used to hold the reference down
    raised ``reference_db`` from −27.2386 dB to −24.6035 dB, and every
    deviation stated against it moved with it — the headline −8.9389 →
    −11.5741 dB, the pooled RMS 3.8031 → 5.7705 dB. The worst bin and band
    are unmoved (15999.7 Hz, 8-16 kHz); only the frame it is read against
    did, which is exactly why the plan doc's own -8.94 dB quote no longer
    appears here.
    """
    result = assemble_cloud_group_result(_s0_combined(), echo_band_hz=S0_ECHO_BAND_HZ)
    assert result["available"] is True
    views = _walk_every_surface(result, monkeypatch)
    _assert_one_number_everywhere(views)

    gauge = views["pipeline"]
    assert gauge["max_db"] == pytest.approx(-11.5741, abs=5e-4)
    assert gauge["max_hz"] == pytest.approx(15999.7, abs=0.5)
    assert gauge["max_band_hz"] == [8000.0, 16000.0]
    assert gauge["tolerance_db"] == 2.5
    assert gauge["rms_db"] == pytest.approx(5.7705, abs=5e-4)
    assert gauge["n_bins"] == 7678
    assert gauge["n_excluded"] == 3074
    assert gauge["passed"] is False
    # The reference the deviations are measured against, pinned because the
    # clamp test below moves it and the headline number moves WITH it.
    assert result["spec"]["reference_db"] == pytest.approx(-24.6035, abs=5e-4)
    assert result["validity_floor_hz"] is None


@corpus.requires_s0_curves
def test_the_real_s0_positions_no_longer_collapse_a_gate():
    """#1790's regression guard: no S0 position collapses its own gate.

    NEW 2026-08-02 (#2045) — see ``tests._flat_lin_corpus`` "The 2026-08-02
    re-pin era". Until PR #1991, ``cloud_04`` alone reported
    ``floor_source="measured_reflection"`` at a **1777.8 Hz** validity floor:
    a reflection "found" 3 samples past the search-window open, which is the
    field instance #1991's prominence vote was written to reject. It
    propagated through the group ``max()`` and flipped a band verdict
    fail→pass — so a detector artifact was silently re-grading the speaker.

    All ten positions gate to the search-span bound at 142.857 Hz.

    This half was carved out of the old
    ``test_the_real_s0_worst_position_floor_clamps_the_low_band``, whose name
    described the artifact rather than the speaker. The clamp MECHANISM's cost
    is still pinned, next door, at an explicit floor.

    **The second half of this test was inverted by #2551**, and the numbers
    below are the reason the issue was filed. 142.857 Hz is indeed under the
    spec table's 250 Hz edge, and while the evaluator was handed that
    VALIDITY floor the group's own floor clamped nothing — which is what this
    test used to assert. The floor the same session's gate disclosure prints,
    and the one its delta probe grades above, is ``2.5/T`` = **357.14 Hz**,
    which clears 250 Hz. So the corpus's own honest floor was clamping the
    low band all along and the spec was not applying it.
    """
    combined = _s0_combined()
    floors = {
        pid: corpus.s0_position_driver_response(corpus.S0_MAIN, pid)[0].validity_floor_hz
        for pid in sorted(corpus._session_groups(corpus.S0_MAIN))
    }
    assert len(floors) == 10
    assert sorted(floors)[0] == "cloud_01"
    assert all(f == pytest.approx(142.857, abs=1e-3) for f in floors.values())

    unclamped = assemble_cloud_group_result(combined, echo_band_hz=S0_ECHO_BAND_HZ)
    natural_worst = max(floors.values())
    # The validity floor is below the table's edge; its trusted counterpart is
    # above it. That gap is the defect #2551 names, on real hardware data.
    assert natural_worst < unclamped["spec"]["bands"][0]["f_lo_hz"]
    natural_trusted = cloud_trusted_floor_hz(natural_worst)
    assert natural_trusted == pytest.approx(357.1429, abs=5e-4)
    assert natural_trusted > unclamped["spec"]["bands"][0]["f_lo_hz"]

    natural = assemble_cloud_group_result(
        combined, echo_band_hz=S0_ECHO_BAND_HZ, validity_floor_hz=natural_worst,
    )
    assert natural["trusted_floor_hz"] == pytest.approx(357.1429, abs=5e-4)
    assert natural["spec"]["bands"][0]["graded_lo_hz"] == pytest.approx(
        357.1429, abs=5e-4
    )
    # What the session's OWN floor costs, measured on this corpus. Small, and
    # in the flattering direction — stated rather than discovered. RE-PINNED
    # when REFERENCE_BAND_HZ narrowed to 250 Hz-2 kHz (#1857): the shift
    # shrank from -0.0611 dB to -0.0243 dB, direction unchanged.
    assert natural["flatness"]["n_bins"] == unclamped["flatness"]["n_bins"] - 73
    assert natural["spec"]["reference_db"] == pytest.approx(-24.6278, abs=5e-4)
    reference_shift = (
        natural["spec"]["reference_db"] - unclamped["spec"]["reference_db"]
    )
    headline_shift = natural["flatness"]["max_db"] - unclamped["flatness"]["max_db"]
    assert reference_shift == pytest.approx(-0.0243, abs=5e-4)
    assert headline_shift == pytest.approx(+0.0243, abs=5e-4)
    assert headline_shift == pytest.approx(-reference_shift, abs=1e-9)
    # No verdict is bought by it — every band still fails on its own merits.
    assert [b["passed"] for b in natural["spec"]["bands"]] == [False, False, False]
    assert natural["spec"]["overall_passed"] is False


@corpus.requires_s0_curves
def test_the_trusted_floor_clamp_costs_the_low_band(monkeypatch):
    """The clamp's measured cost, stated and pinned rather than assumed.

    **RE-WORKED 2026-08-02 (#2045)**, and re-pointed at the trusted floor by
    **#2551** — see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era".
    This test used to take its floor FROM the corpus, where ``cloud_04``'s
    collapsed gate supplied 1777.8 Hz; PR #1991 showed that collapse was a
    false early fire (#1790) and removed it.

    The costs below are the CLAMP MECHANISM's own behaviour and are worth
    guarding at a floor high enough to move real numbers, so the floor is the
    explicit :data:`CLAMP_TRUSTED_FLOOR_HZ` — the value the artifact used to
    produce. Sourcing it from a constant is what keeps this guard from
    depending on a detector bug to have a subject. #2551 only changed HOW it
    is supplied: the assembler takes a validity floor and derives ``2.5/T``
    itself, so the test hands over :data:`CLAMP_VALIDITY_FLOOR_HZ` and
    asserts the trusted number it produces. Every figure below is unchanged
    by that move, because the clamp grades the same bins either way.

    **RE-PINNED AGAIN** when
    :data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ` narrowed from
    250 Hz-8 kHz to 250 Hz-2 kHz, the low-mid band alone (#1857). The bin
    counts below are the clamp mechanism's own and do not depend on which
    band the reference is pooled over, so they are untouched; every figure
    computed FROM the reference moved, several of them by a lot more than
    before, because the same 250-1777.8 Hz sliver is now a much bigger share
    of a much smaller reference pool.

    Clamping at that floor costs, measured on this corpus:

    * 987 bins leave the 250 Hz-2 kHz band (7678 -> 6691 graded);
    * the reference re-centres -24.6035 -> -29.1532 dB;
    * the HEADLINE ``max_db`` moves -11.5741 -> -7.0243 dB — **+4.5498 dB in
      the flattering direction**, exactly the reference shift, because the
      worst bin (15999.7 Hz) survives the clamp and its deviation tracks the
      reference one-for-one. It moves FURTHER than the RMS does, and it is
      the first number the ledger line prints;
    * the pooled RMS moves 5.7705 -> 2.7474 dB (-3.0232 dB);
    * and the 250 Hz-2 kHz band **VERDICT FLIPS**, -4.9174 dB (fail) ->
      -0.3677 dB (pass), because ``passed`` is ``abs(max) <= tolerance``. The
      pre-clamp worst bin in that band now sits on the QUIET side of the new
      reference rather than the loud side (it read +4.2458 dB, too loud,
      before #1857 narrowed the frame) — a different bin becomes the argmax
      once the whole curve is re-read against one constant, which is exactly
      why this number is read off a run rather than derived by hand.

    The direction is response-shape dependent and measured on THIS corpus
    only: the removed region sat above the surviving reference here, so
    dropping it flattered everything left. A speaker whose sub-floor region
    is quiet would move the other way — the sign does not generalize.

    None of it is the speaker improving; it is the same speaker graded on
    fewer bins, which is exactly why ``n_bins`` rides on the gauge
    (``ConvergenceResidual``'s own "a residual that fell because the
    denominator shrank is not convergence" rule).
    """
    combined = _s0_combined()
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=S0_ECHO_BAND_HZ)

    # The mechanism itself, exercised at the floor the artifact used to make.
    clamped = assemble_cloud_group_result(
        combined,
        echo_band_hz=S0_ECHO_BAND_HZ,
        validity_floor_hz=CLAMP_VALIDITY_FLOOR_HZ,
    )
    assert clamped["trusted_floor_hz"] == pytest.approx(
        CLAMP_TRUSTED_FLOOR_HZ, abs=0.1
    )
    assert clamped["spec"]["bands"][0]["graded_lo_hz"] == pytest.approx(
        CLAMP_TRUSTED_FLOOR_HZ, abs=0.1
    )
    assert (
        clamped["flatness"]["n_bins"] == unclamped["flatness"]["n_bins"] - 987
    )

    # The reference re-centring, and the headline number that rides it.
    assert clamped["spec"]["reference_db"] == pytest.approx(-29.1532, abs=5e-4)
    assert clamped["flatness"]["max_db"] == pytest.approx(-7.0243, abs=5e-4)
    reference_shift = (
        clamped["spec"]["reference_db"] - unclamped["spec"]["reference_db"]
    )
    max_shift = clamped["flatness"]["max_db"] - unclamped["flatness"]["max_db"]
    assert reference_shift == pytest.approx(-4.5498, abs=5e-4)
    # Equal and opposite: the worst bin survives the clamp, so the headline
    # number is displaced by exactly the reference, no more and no less.
    assert max_shift == pytest.approx(-reference_shift, abs=1e-9)
    # And it moves FURTHER than the RMS — the number a reader sees first is
    # the number the clamp perturbs most.
    rms_shift = clamped["flatness"]["rms_db"] - unclamped["flatness"]["rms_db"]
    assert clamped["flatness"]["rms_db"] == pytest.approx(2.7474, abs=5e-4)
    assert abs(max_shift) > abs(rms_shift)

    # The band VERDICT flip, not merely a number shift.
    low_before = unclamped["spec"]["bands"][0]
    low_after = clamped["spec"]["bands"][0]
    assert (low_before["f_lo_hz"], low_before["f_hi_hz"]) == (250.0, 2000.0)
    assert low_before["max_deviation_db"] == pytest.approx(-4.9174, abs=5e-4)
    assert low_before["passed"] is False
    assert low_after["max_deviation_db"] == pytest.approx(-0.3677, abs=5e-4)
    assert low_after["passed"] is True
    # Overall still fails — the other two bands fail on their own merits, so
    # the flip is visible per band rather than flattering the whole verdict.
    assert clamped["spec"]["overall_passed"] is False

    # The interference accounting is untouched by a gate artifact.
    assert (
        clamped["merged_excluded_bands_hz"] == unclamped["merged_excluded_bands_hz"]
    )
    # And the whole walk still shows one number, clamped or not.
    _assert_one_number_everywhere(_walk_every_surface(clamped, monkeypatch))
