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
  contract; the validity-floor clamp's contract; and the frame-consistency
  walk — pipeline result → durable ``cloud`` block → ``_compact_cloud_status``
  → ``/state`` → the envelope's rendered ledger line — asserted byte-identical
  at every hop.
* **Corpus-gated.** The same walk on the real S0 main-leg cloud, so the
  contract is pinned against hardware data and the S0 session's own measured
  regime (including its one collapsed-gate position) is stated with numbers
  rather than assumed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    assemble_cloud_group_result,
    cloud_validity_floor_hz,
)
from jasper.active_speaker.flat_spec import (
    SPEC_BANDS,
    evaluate_flat_spec,
    spec_convergence_residual,
    spec_flatness_gauge,
)
from jasper.audio_measurement.spatial_combine import PositionCapture, combine_positions
from jasper.web.correction_crossover_v2 import (
    _chart_cloud_status,
    _compact_cloud_status,
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


def test_validity_floor_clamps_the_spec_bands_lower_edge():
    """A floor above the spec table's own 250 Hz edge removes those bins from
    the evaluation — from the deviation metrics AND from the reference level,
    since a bin that is not a measurement must not be able to re-center the
    target either."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=1000.0,
    )
    low_before = unclamped["spec"]["bands"][0]
    low_after = clamped["spec"]["bands"][0]
    assert low_after["n_excluded"] > low_before["n_excluded"]
    assert low_after["n_bins"] == low_before["n_bins"]  # membership is unchanged
    assert clamped["spec"]["reference_db"] != unclamped["spec"]["reference_db"]
    assert clamped["validity_floor_hz"] == 1000.0


def test_a_floor_below_the_spec_edge_changes_no_graded_number():
    """The ordinary case (JTS3 gates measure ~143-200 Hz, the spec starts at
    250 Hz): the clamp grades exactly the same bins, so every band figure,
    the reference level, the verdict, and the whole gauge are byte-identical.

    What it DOES change is ``excluded_intervals`` — a report-wide disclosure
    spanning the entire axis, which now records the sub-250 Hz region the
    clamp removed. That is honest (those bins genuinely were dropped) and it
    is exactly why the gauge quotes spec-band BIN counts rather than an
    interval count: an interval below every spec band was never graded, so
    counting it as "excluded from grading" would over-report."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=142.86,
    )
    assert json.dumps(clamped["flatness"], sort_keys=True) == json.dumps(
        unclamped["flatness"], sort_keys=True
    )
    for key in ("bands", "reference_db", "overall_passed"):
        assert json.dumps(clamped["spec"][key], sort_keys=True) == json.dumps(
            unclamped["spec"][key], sort_keys=True
        ), key
    added = [
        i for i in clamped["spec"]["excluded_intervals"]
        if i not in unclamped["spec"]["excluded_intervals"]
    ]
    assert added and all(hi < SPEC_BANDS[0][0] for _lo, hi in added)
    assert clamped["validity_floor_hz"] == pytest.approx(142.86)


def test_no_floor_clamps_nothing_and_is_disclosed_as_unknown():
    """``None`` is "the lower edge could not be verified", not zero and not a
    withheld gauge — the whole 2-16 kHz evidence would otherwise be thrown
    away over an unknown floor."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result["validity_floor_hz"] is None
    assert result["flatness"]["evaluable"] is True


def test_the_floor_clamp_never_inflates_the_interference_interval_count():
    """``merged_excluded_bands_hz`` (and so `/state`'s
    ``excluded_interval_count``) is the honesty instruments' own finding —
    "how much interference did we find". A gate artifact is a different fact
    and must not be silently counted as interference."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=1000.0,
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
    ``_compact_cloud_status`` (`/state`) → ``build_crossover_envelope_v2``
    (the wizard envelope + its rendered ledger line) → the shipped doctor
    check (N-1: the doctor is a spec-facing surface too, so "every surface"
    has to include it rather than be quietly scoped to three).

    The doctor reads through ``crossover_v2_status_block`` (its own import,
    resolved at call time), so it is reached by patching the durable-state
    loader underneath it — the same seam PR-4's own doctor corpus test uses
    — rather than by handing it a pre-built block.
    """
    compact = _compact_cloud_status(_durable_cloud_block(result))
    chart = _chart_cloud_status(_durable_cloud_block(result))
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
        "doctor_detail": doctor.detail,
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

    # N-1: the doctor quotes the same worst deviation, to the digit.
    assert f"worst={gauge['max_db']:+.2f}dB" in views["doctor_detail"]

    # PR-7: the before/after chart's own inputs are the SAME report, not a
    # fourth derivation. reference_db is the corridor's center line;
    # spec_bands already carries tolerance_db (asserted above via
    # max_deviation_db/passed) — this pins the reference alongside it.
    assert views["state_reference_db"] == spec["reference_db"]
    assert views["envelope_reference_db"] == spec["reference_db"]
    # Review S-1 (2026-07-27): the chart feed re-decimates the pipeline's own
    # 512-point ``curve`` down to its own 256-point ceiling
    # (``_chart_cloud_status``'s own ``CHART_CURVE_MAX_JSON_POINTS`` —
    # measured 41,161 bytes for both phases at the full resolution, halved to
    # 20,653 by this re-decimation), so it is no longer byte-identical to the
    # pipeline curve — but every point it DOES carry must still be an actual
    # point of the pipeline curve, at the same stride
    # ``_chart_cloud_status`` computes, never an interpolation or a
    # re-derivation.
    pipeline_freqs = views["pipeline_curve"]["freqs_hz"]
    pipeline_mags = views["pipeline_curve"]["magnitude_db"]
    n = len(pipeline_freqs)
    step = max(1, n // 256)
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


def test_the_pre_apply_cloud_never_supplies_the_household_flatness_line():
    """``cloud_measure`` carries its own gauge (it is the same construction on
    the same footing) but the ledger line is the POST-apply claim — the same
    pre-vs-post distinction PR-4's doctor blocker drew. A pre-apply-only
    session says nothing rather than reporting the uncorrected baseline as
    "how flat your speaker is"."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    compact = _compact_cloud_status(
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
    assert envelope["expert_details"] == []


def test_an_unavailable_pipeline_degrades_honestly_at_every_surface():
    """Cloud-absent honesty (plan PR-5): a group that closed but could not be
    analysed reports ``None`` — never ``0``, never a spec-frame number
    invented from a construction that did not run — and the ledger says so
    in words rather than going silently blank like a session with no group."""
    result = assemble_cloud_group_result(None, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result == {"available": False, "reason": "combine_failed"}
    compact = _compact_cloud_status(_durable_cloud_block(result))
    entry = compact[PHASE_CLOUD_VERIFY]
    assert entry["flatness"] is None
    assert entry["overall_passed"] is None
    assert entry["excluded_interval_count"] is None
    # PR-7: same honesty rule for the corridor reference and the chart curve.
    assert entry["reference_db"] is None
    chart = _chart_cloud_status(_durable_cloud_block(result))
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

    Pinned figures are the S0 main leg's, measured 2026-07-27 on this exact
    chain (10 positions, echo band 5-19 kHz, no validity-floor clamp — the
    unclamped case, so these are directly comparable to
    ``test_crossover_v2_cloud_pipeline.py``'s own S0 pins): worst deviation
    -8.94 dB at ~16.0 kHz in the 8-16 kHz band, pooled RMS 3.76 dB, 3054 of
    7698 spec-band bins excluded. The -8.94 dB figure is the same
    comb-contaminated top-octave read the plan doc's "S0 executed" § d
    quotes as -8.94 dB and explicitly forbids quoting as "the speaker's top
    octave" — it reproduces here to the digit, which is the point: one
    construction, and it agrees with the forensics.
    """
    result = assemble_cloud_group_result(_s0_combined(), echo_band_hz=S0_ECHO_BAND_HZ)
    assert result["available"] is True
    views = _walk_every_surface(result, monkeypatch)
    _assert_one_number_everywhere(views)

    gauge = views["pipeline"]
    assert gauge["max_db"] == pytest.approx(-8.9399, abs=5e-4)
    assert gauge["max_hz"] == pytest.approx(15999.7, abs=0.5)
    assert gauge["max_band_hz"] == [8000.0, 16000.0]
    assert gauge["tolerance_db"] == 2.5
    assert gauge["rms_db"] == pytest.approx(3.7649, abs=5e-4)
    assert gauge["n_bins"] == 7698
    assert gauge["n_excluded"] == 3054
    assert gauge["passed"] is False
    # The reference the deviations are measured against, pinned because the
    # clamp test below moves it and the headline number moves WITH it.
    assert result["spec"]["reference_db"] == pytest.approx(-27.2670, abs=5e-4)
    assert result["validity_floor_hz"] is None


@corpus.requires_s0_curves
def test_the_real_s0_worst_position_floor_clamps_the_low_band(monkeypatch):
    """The clamp's measured cost, stated and pinned rather than assumed.

    Nine of the S0 main leg's ten positions gate down to 142.86 Hz — below
    the spec table's 250 Hz edge, so the clamp would be a no-op. The tenth,
    ``cloud_04``, collapsed to **1777.8 Hz** (a ~0.56 ms window — a very
    early reflection at that spot), and because the combined curve is a power
    mean ACROSS positions, every bin below that carries cloud_04's
    truncated-window artifact at 1/10 weight.

    Clamping at the group's worst floor therefore costs, measured 2026-07-27:

    * 1009 bins leave the 250 Hz-2 kHz band (7698 -> 6689 graded);
    * the reference re-centres -27.2670 -> -28.3166 dB;
    * the HEADLINE ``max_db`` moves -8.9399 -> -7.8903 dB — **+1.0495 dB in
      the flattering direction**, exactly the reference shift, because the
      worst bin (15999.7 Hz) survives the clamp and its deviation tracks the
      reference one-for-one. It moves FURTHER than the RMS does, and it is
      the first number the ledger line prints;
    * the pooled RMS moves 3.7649 -> 3.1524 dB (-0.6125 dB);
    * and the 250 Hz-2 kHz band **VERDICT FLIPS**, +4.1637 dB (fail) ->
      -1.2855 dB (pass), because ``passed`` is ``abs(max) <= tolerance``.

    The direction is response-shape dependent and measured on THIS corpus
    only: the removed region sat above the surviving reference here, so
    dropping it flattered everything left. A speaker whose sub-floor region
    is quiet would move the other way — the sign does not generalize.

    None of it is the speaker improving; it is the same speaker graded on
    fewer bins, which is exactly why ``n_bins``/``n_excluded`` ride on the
    gauge (``ConvergenceResidual``'s own "a residual that fell because the
    mask grew is not convergence" rule).
    """
    combined = _s0_combined()
    floors = {
        pid: corpus.s0_position_driver_response(corpus.S0_MAIN, pid)[0].validity_floor_hz
        for pid in sorted(corpus._session_groups(corpus.S0_MAIN))
    }
    assert floors["cloud_04"] == pytest.approx(1777.8, abs=0.1)
    assert sorted(floors)[0] == "cloud_01"
    assert all(
        f == pytest.approx(142.857, abs=1e-3)
        for pid, f in floors.items() if pid != "cloud_04"
    )
    worst = max(floors.values())

    unclamped = assemble_cloud_group_result(combined, echo_band_hz=S0_ECHO_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=S0_ECHO_BAND_HZ, validity_floor_hz=worst,
    )
    assert clamped["validity_floor_hz"] == pytest.approx(1777.8, abs=0.1)
    assert (
        clamped["flatness"]["n_bins"] == unclamped["flatness"]["n_bins"] - 1009
    )

    # The reference re-centring, and the headline number that rides it.
    assert clamped["spec"]["reference_db"] == pytest.approx(-28.3166, abs=5e-4)
    assert clamped["flatness"]["max_db"] == pytest.approx(-7.8903, abs=5e-4)
    reference_shift = (
        clamped["spec"]["reference_db"] - unclamped["spec"]["reference_db"]
    )
    max_shift = clamped["flatness"]["max_db"] - unclamped["flatness"]["max_db"]
    assert reference_shift == pytest.approx(-1.0495, abs=5e-4)
    # Equal and opposite: the worst bin survives the clamp, so the headline
    # number is displaced by exactly the reference, no more and no less.
    assert max_shift == pytest.approx(-reference_shift, abs=1e-9)
    # And it moves FURTHER than the RMS — the number a reader sees first is
    # the number the clamp perturbs most.
    rms_shift = clamped["flatness"]["rms_db"] - unclamped["flatness"]["rms_db"]
    assert clamped["flatness"]["rms_db"] == pytest.approx(3.1524, abs=5e-4)
    assert abs(max_shift) > abs(rms_shift)

    # The band VERDICT flip, not merely a number shift.
    low_before = unclamped["spec"]["bands"][0]
    low_after = clamped["spec"]["bands"][0]
    assert (low_before["f_lo_hz"], low_before["f_hi_hz"]) == (250.0, 2000.0)
    assert low_before["max_deviation_db"] == pytest.approx(4.1637, abs=5e-4)
    assert low_before["passed"] is False
    assert low_after["max_deviation_db"] == pytest.approx(-1.2855, abs=5e-4)
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
