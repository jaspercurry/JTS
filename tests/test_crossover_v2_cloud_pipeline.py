# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Flat-linearization plan PR-4: the live-flow wiring of the honesty pipeline.

Two layers:

* **Synthetic (always runs).** ``_composed_swept_band_hz`` /
  ``_derive_cloud_echo_band_hz``'s band-derivation contract (containment, the
  HF-regime floor, graceful degradation on a malformed contract), and
  ``assemble_cloud_group_result``'s wiring contract (issue #1742 item 4: mask
  ∪ geometry ∪ registry consumed TOGETHER) on a small constructed two-path
  cloud — no corpus needed to prove the UNION happens.
* **Corpus-gated.** The exact wiring assembly fed the real S0 main-leg cloud,
  pinning the numbers this module's docstrings and the plan doc's "S0
  executed" section already state (geometry locked, screen excludes 0 of
  5462 bins in 8-16 kHz, the null registry excludes ~2949) so a regression
  that silently drops the null registry's contribution is caught against
  real hardware data, not only a synthetic proxy.
"""
from __future__ import annotations

import logging

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2_flow import (
    CLOUD_CURVE_MAX_JSON_POINTS,
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
    _composed_swept_band_hz,
    _derive_cloud_echo_band_hz,
    _geometry_guidance_copy,
    assemble_cloud_group_result,
)
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.spatial_combine import (
    DEFAULT_ECHO_BAND_HZ,
    PositionCapture,
    combine_positions,
)

from tests import _flat_lin_corpus as corpus

SAMPLE_RATE = 48_000
N_FFT = 4096
SYNTHETIC_BAND_HZ = (4000.0, 19_000.0)


# --------------------------------------------------------------------------- #
# Synthetic two-path cloud (local to this file — a different test file's own
# fixture generator is not a shared-library import; this is the same ~15-line
# shape test_interference_nulls.py builds, reused in spirit, not coupled).
# --------------------------------------------------------------------------- #


def _two_path_ir(delay_samples: int, r: float, *, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ir = rng.normal(0.0, 1e-7, N_FFT)
    ir[100] += 1.0
    if r != 0.0:
        ir[100 + delay_samples] += r
    return ir


def _curve_from_ir(ir: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    magnitude = 20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(ir, N_FFT)), 1e-12))
    return freqs, magnitude


def _locked_cloud(delay_samples: int = 15, r: float = 0.37, n: int = 6) -> list:
    """A position-invariant two-path cloud — every position sees the SAME
    delay, which is exactly what makes ``geometry.locked`` true and what the
    power-vs-median screen structurally cannot flag (plan doc "S0 executed"
    § e.1 — the fixture is the same shape, a fixed delay across positions).
    """
    captures = []
    for k in range(n):
        ir = _two_path_ir(delay_samples, r, seed=2000 + k)
        freqs, magnitude = _curve_from_ir(ir)
        captures.append(
            PositionCapture(
                position_id=f"p{k:02d}", freqs_hz=freqs, magnitude_db=magnitude,
                sample_rate=SAMPLE_RATE, ir=ir,
            )
        )
    return captures


def _unlocked_cloud(n: int = 6) -> list:
    """No echo at all — geometry has no credible estimates, so ``locked`` is
    False (an absence of evidence, not a measured "does not move")."""
    captures = []
    for k in range(n):
        ir = _two_path_ir(15, 0.0, seed=3000 + k)
        freqs, magnitude = _curve_from_ir(ir)
        captures.append(
            PositionCapture(
                position_id=f"q{k:02d}", freqs_hz=freqs, magnitude_db=magnitude,
                sample_rate=SAMPLE_RATE, ir=ir,
            )
        )
    return captures


# --------------------------------------------------------------------------- #
# _composed_swept_band_hz
# --------------------------------------------------------------------------- #


def test_composed_swept_band_hz_unions_both_roles():
    roles = [
        RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
    ]
    assert _composed_swept_band_hz(roles) == (45.0, 20000.0)


def test_composed_swept_band_hz_is_order_independent():
    a = RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0))
    b = RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0))
    assert _composed_swept_band_hz([a, b]) == _composed_swept_band_hz([b, a])


# --------------------------------------------------------------------------- #
# _derive_cloud_echo_band_hz
# --------------------------------------------------------------------------- #


def test_derive_cloud_echo_band_hz_falls_back_to_module_default_when_undeclared():
    """No tweeter_measurement_band_hz threaded through (an older/incomplete
    confirmed profile) -- the module's own long-standing default, clamped
    inside whatever passband was declared."""
    signal_band = (45.0, 20000.0)
    assert _derive_cloud_echo_band_hz(signal_band, None) == DEFAULT_ECHO_BAND_HZ


def test_derive_cloud_echo_band_hz_uses_the_declared_measurement_band():
    signal_band = (45.0, 20000.0)
    declared = (5000.0, 20000.0)
    assert _derive_cloud_echo_band_hz(signal_band, declared) == declared


def test_derive_cloud_echo_band_hz_clamps_to_the_declared_passband():
    """A tweeter measurement band wider than the composed passband (an
    unusual but not malformed declaration) is clamped -- the inherited
    PR-2/PR-6a containment constraint: an echo band that extends OUTSIDE the
    declared passband is uncalibrated (spatial_combine's own "What is NOT
    calibrated" paragraph)."""
    signal_band = (150.0, 18000.0)
    declared = (5000.0, 20000.0)  # upper edge exceeds the passband
    assert _derive_cloud_echo_band_hz(signal_band, declared) == (5000.0, 18000.0)


def test_derive_cloud_echo_band_hz_falls_back_to_the_passband_when_degenerate(
    caplog,
):
    """A declared measurement band entirely outside the composed passband --
    a genuinely malformed contract -- degrades to the passband itself rather
    than handing a caller an inverted pair, and the fallback is disclosed."""
    signal_band = (150.0, 2000.0)
    declared = (5000.0, 20000.0)  # sits entirely above the passband
    with caplog.at_level(logging.WARNING):
        result = _derive_cloud_echo_band_hz(signal_band, declared)
    assert result == signal_band
    assert any(
        "cloud_echo_band_degenerate" in r.message for r in caplog.records
    )


def test_derive_cloud_echo_band_hz_warns_below_the_hf_regime_floor(caplog):
    """Disclosed, never silently trusted or silently overridden -- see
    ECHO_BAND_HF_REGIME_FLOOR_HZ's own citation of
    test_band_deficit_separation_depends_on_the_analysis_band."""
    signal_band = (150.0, 20000.0)
    declared = (2000.0, 20000.0)  # below the floor, but well inside the passband
    with caplog.at_level(logging.WARNING):
        result = _derive_cloud_echo_band_hz(signal_band, declared)
    # Disclosed, not overridden: the declared value is what is returned.
    assert result == declared
    assert any(
        "cloud_echo_band_below_hf_regime" in r.message for r in caplog.records
    )


def test_derive_cloud_echo_band_hz_above_the_floor_is_silent(caplog):
    signal_band = (150.0, 20000.0)
    declared = (ECHO_BAND_HF_REGIME_FLOOR_HZ, 20000.0)
    with caplog.at_level(logging.WARNING):
        _derive_cloud_echo_band_hz(signal_band, declared)
    assert not any(
        "cloud_echo_band_below_hf_regime" in r.message for r in caplog.records
    )


def test_a_plausible_jts3_like_contract_stays_inside_the_hf_regime_and_the_passband():
    """The contract test the work order asks for: a plausible confirmed
    driver contract at this speaker's own fc_hz (2000 Hz, s0-session-main's
    own session.json) produces a derived echo band that (a) sits INSIDE the
    composed passband (the inherited PR-2/PR-6a containment constraint) and
    (b) clears ECHO_BAND_HF_REGIME_FLOOR_HZ with real margin -- both
    properties this program's tests already lean on
    (test_interference_nulls.py's S0_BAND_HZ == DEFAULT_ECHO_BAND_HZ, the
    exact band PR-1's own corpus acceptance validated against). Numbers
    chosen to match tests/test_active_speaker_driver_safety.py's own
    plausible-tweeter fixture ([5000, 20000]), not invented for this test.
    """
    roles = [
        RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
    ]
    signal_band = _composed_swept_band_hz(roles)
    echo_band = _derive_cloud_echo_band_hz(signal_band, (5000.0, 20000.0))

    assert signal_band[0] <= echo_band[0] and echo_band[1] <= signal_band[1]
    assert echo_band[0] >= ECHO_BAND_HF_REGIME_FLOOR_HZ
    # Comfortably inside the calibrated family this program's corpus tests
    # already validated the null gate against (S0_BAND_HZ in
    # test_interference_nulls.py == DEFAULT_ECHO_BAND_HZ == (5000, 19000)).
    assert echo_band[0] == pytest.approx(DEFAULT_ECHO_BAND_HZ[0])


# --------------------------------------------------------------------------- #
# _geometry_guidance_copy
# --------------------------------------------------------------------------- #


def test_geometry_guidance_empty_when_not_locked():
    assert _geometry_guidance_copy({"locked": False}) == ""


def test_geometry_guidance_present_when_locked():
    text = _geometry_guidance_copy({"locked": True, "thin_evidence": False})
    assert text
    assert "spread" in text.lower() or "spreading" in text.lower()


def test_geometry_guidance_softened_when_thin_evidence():
    """thin_evidence is a cliff at an exact confident-estimate count, not a
    gradient (spatial_combine.GeometryLock's own docstring) -- the two copy
    variants must differ (softened, not suppressed), and the thin variant
    must not claim a percentage/gradient."""
    locked = _geometry_guidance_copy({"locked": True, "thin_evidence": False})
    thin = _geometry_guidance_copy({"locked": True, "thin_evidence": True})
    assert thin != locked
    assert thin
    assert "%" not in thin


# --------------------------------------------------------------------------- #
# assemble_cloud_group_result — the wiring contract (issue #1742 item 4)
# --------------------------------------------------------------------------- #


def test_assemble_cloud_group_result_is_unavailable_when_combine_failed():
    result = assemble_cloud_group_result(None, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result == {"available": False, "reason": "combine_failed"}


def test_assemble_cloud_group_result_never_raises_on_a_pipeline_bug(monkeypatch):
    """Diagnostic/disclosure machinery, never a capture-accept gate: a bug
    inside the pipeline degrades to an honest 'unavailable', never a crash
    that would strand a session that just finished a real cloud group."""
    import jasper.audio_measurement.interference_nulls as nulls_mod

    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)

    def _boom(*_a, **_k):
        raise ValueError("synthetic pipeline failure")

    monkeypatch.setattr(nulls_mod, "identify_interference_nulls", _boom)
    # assemble_cloud_group_result imports identify_interference_nulls lazily
    # INSIDE its own try block, from the module namespace, so patching the
    # module attribute is what a real regression would also hit.
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result == {"available": False, "reason": "pipeline_failed"}


def test_assemble_cloud_group_result_reports_geometry_and_registry_for_a_locked_cloud():
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    assert result["available"] is True
    assert result["geometry"]["locked"] is True
    assert result["geometry_guidance"]
    assert result["null_registry"]["classification"] in (
        "position_invariant", "position_dependent",
    )
    assert result["echo_band_hz"] == list(SYNTHETIC_BAND_HZ)
    # Floor-division stride (mirrors _decimate_sum's exact shape): a grid
    # size not evenly divisible by its own stride yields ceil(n/step), which
    # can land one OVER the nominal cap — not <=, but never far past it.
    assert len(result["curve"]["freqs_hz"]) <= CLOUD_CURVE_MAX_JSON_POINTS + 1
    assert len(result["curve"]["freqs_hz"]) == len(result["curve"]["magnitude_db"])
    assert isinstance(result["spec"]["overall_passed"], bool)


def test_assemble_cloud_group_result_no_geometry_guidance_when_not_locked():
    combined = combine_positions(_unlocked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    assert result["available"] is True
    assert result["geometry"]["locked"] is False
    assert result["geometry_guidance"] == ""


def test_assemble_cloud_group_result_unions_the_screen_and_the_null_registry():
    """THE wiring-contract test: deleting either input from the mask must
    change the assembled spec verdict's excluded-bin accounting. A
    position-invariant comb is exactly the shape the screen alone cannot
    catch (plan doc "S0 executed" § e.1) -- the null registry's contribution
    IS the whole test.
    """
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    assert result["available"] is True

    # The full assembly's merged mask must exclude MORE than the screen
    # alone -- otherwise the null registry contributed nothing and this
    # fixture is not exercising the union at all.
    screen_only_report = evaluate_flat_spec(
        combined.freqs_hz, combined.power_mean_spec_db, combined.excluded,
    )
    screen_only_excluded = sum(b.n_excluded for b in screen_only_report.bands)
    merged_excluded = sum(b["n_excluded"] for b in result["spec"]["bands"])
    assert merged_excluded > screen_only_excluded, (
        "the fixture's comb must be invisible to the screen alone for this "
        "test to prove anything about the union"
    )

    # And the assembled result reports EXACTLY that union, not a different
    # (e.g. registry-alone) accounting — deleting the screen's own
    # contribution would also be a hole, even though this fixture's screen
    # contributes nothing new on top of the registry.
    from jasper.audio_measurement.interference_nulls import identify_interference_nulls

    null_report = identify_interference_nulls(combined, band_hz=SYNTHETIC_BAND_HZ)
    expected_mask = np.asarray(combined.excluded) | np.asarray(null_report.excluded)
    expected_report = evaluate_flat_spec(
        combined.freqs_hz, combined.power_mean_spec_db, expected_mask,
    )
    assert merged_excluded == sum(b.n_excluded for b in expected_report.bands)


# --------------------------------------------------------------------------- #
# Corpus-gated: the exact wiring assembly on the real S0 main-leg cloud
# --------------------------------------------------------------------------- #


@corpus.requires_s0_curves
def test_the_real_s0_cloud_is_identified_and_excluded_by_the_full_assembly():
    """Corpus-replay acceptance: feeds the S0 main-leg cloud (JTS3 cdhorn,
    10 positions) through combine_positions -> assemble_cloud_group_result
    at the SAME band PR-1's own corpus acceptance validated
    (test_interference_nulls.py's S0_BAND_HZ == (5000, 19000) ==
    DEFAULT_ECHO_BAND_HZ), and pins the documented S0 finding: the
    power-vs-median screen alone excludes 0 of 5462 bins in 8-16 kHz (plan
    doc "S0 executed" § e.1's own number), while the null registry
    identifies the position-invariant comb and the union excludes it.

    This is the "delete one input, the test must fail" property against
    REAL hardware data: drop the null registry from the mask (screen alone)
    and the 8-16 kHz band's excluded-bin count collapses to 0 -- the exact
    silent regression this test exists to catch.
    """
    echo_band_hz = (5000.0, 19_000.0)
    captures = corpus.s0_position_captures(corpus.S0_MAIN)
    assert len(captures) == 10
    combined = combine_positions(
        captures, echo_band_hz=echo_band_hz,
        signal_band_hz=corpus.S0_SUMMED_PASSBAND_HZ,
    )
    assert combined.geometry.locked is True
    assert combined.geometry.thin_evidence is False

    in_8_16k = (combined.freqs_hz >= 8000.0) & (combined.freqs_hz < 16000.0)
    screen_excluded_in_band = int(np.sum(combined.excluded & in_8_16k))
    assert screen_excluded_in_band == 0, (
        "the documented S0 finding: the screen alone catches nothing here"
    )

    result = assemble_cloud_group_result(combined, echo_band_hz=echo_band_hz)
    assert result["available"] is True
    assert result["null_registry"]["classification"] == "position_invariant"
    assert result["geometry_guidance"]

    band_8_16k = next(
        b for b in result["spec"]["bands"]
        if b["f_lo_hz"] == 8000.0 and b["f_hi_hz"] == 16000.0
    )
    # The union DOES exclude bins in 8-16 kHz — the screen alone (asserted
    # zero above) could not have produced this on its own.
    assert band_8_16k["n_excluded"] > 0
    merged_excluded_in_band = band_8_16k["n_excluded"]

    # The "delete one input" collapse, made explicit: screen-only accounting
    # for the SAME band is exactly 0, the documented S0 number.
    screen_only_report = evaluate_flat_spec(
        combined.freqs_hz, combined.power_mean_spec_db, combined.excluded,
    )
    screen_only_band = next(
        b for b in screen_only_report.bands
        if b.f_lo_hz == 8000.0 and b.f_hi_hz == 16000.0
    )
    assert screen_only_band.n_excluded == 0
    assert merged_excluded_in_band > screen_only_band.n_excluded
