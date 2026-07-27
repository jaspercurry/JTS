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

import json
import logging
import re
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2_flow import (
    CLOUD_CURVE_MAX_JSON_POINTS,
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
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
# Drift guard (N5 review finding, 2026-07-26)
# --------------------------------------------------------------------------- #


def test_cloud_curve_max_json_points_mirrors_the_verify_priors_decimation_cap():
    """``CLOUD_CURVE_MAX_JSON_POINTS``'s own comment states the mirror is
    deliberate (independent constant only because importing
    ``correction_crossover_v2.MAX_PERSISTED_SUM_POINTS`` would be a
    circular import) — pinned so the two curve-decimation caps cannot drift
    apart silently, the same way ``tests/test_env_load_mirrors_unit.py``
    pins ``ENV_FILES`` against the units that source it."""
    from jasper.web.correction_crossover_v2 import MAX_PERSISTED_SUM_POINTS

    assert CLOUD_CURVE_MAX_JSON_POINTS == MAX_PERSISTED_SUM_POINTS


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
    # zero above) could not have produced this on its own. Pinned to the
    # exact figures (N2 review finding, 2026-07-26), not just >0/==0: 5462
    # total bins in this band on the S0 corpus's analysis grid, 2949 of them
    # excluded by the union -- the same numbers this module's docstrings and
    # the plan doc's "S0 executed" section quote.
    assert band_8_16k["n_bins"] == 5462
    assert band_8_16k["n_excluded"] == 2949
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


@corpus.requires_s0_curves
def test_doctor_does_not_warn_on_the_real_s0_pre_apply_spec_failure(monkeypatch):
    """BLOCKER review finding (2026-07-27), against REAL hardware data rather
    than a synthetic monkeypatch: the reviewer verified on the real S0
    corpus, through the shipped doctor, that a perfectly corrected speaker
    warned forever, because ``cloud_measure`` — the PRE-APPLY, uncorrected
    baseline — never passes its spec by construction. This mirrors that
    scripted run as a pinned, repeatable test.

    The S0 main-leg cloud genuinely fails its spec (``overall_passed`` is
    ``False`` in all three bands — this is real measured data, not a rigged
    number), so it stands in for a real ``cloud_measure`` block exactly as
    it would appear in a household's durable state. Paired with a synthetic
    PASSING ``cloud_verify`` (no second real corrected-speaker corpus
    exists), the doctor must report OK, not WARN — the pre-apply grade is
    diagnostic, never the gate.
    """
    from types import SimpleNamespace

    from jasper.cli.doctor.correction import check_crossover_v2_cloud_pipeline
    from jasper.web import correction_crossover_v2 as v2host

    echo_band_hz = (5000.0, 19_000.0)
    captures = corpus.s0_position_captures(corpus.S0_MAIN)
    combined = combine_positions(
        captures, echo_band_hz=echo_band_hz,
        signal_band_hz=corpus.S0_SUMMED_PASSBAND_HZ,
    )
    real_measure_result = assemble_cloud_group_result(combined, echo_band_hz=echo_band_hz)
    assert real_measure_result["spec"]["overall_passed"] is False, (
        "this test's whole premise is a REAL pre-apply spec failure"
    )

    monkeypatch.setattr(
        v2host, "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": bool(combined.geometry.locked)},
                    "pipeline": real_measure_result,
                },
                "cloud_verify": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": True, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "cloud_measure: spec=fail" in r.detail
    assert "cloud_verify: spec=pass" in r.detail


# --------------------------------------------------------------------------- #
# PR-6b: the carve-out disclosure (owner decision 1, 2026-07-25)
#
# "Identified interference nulls are excluded from spec evaluation AND from
# correction; the ±2.5 dB tolerance applies to the surviving envelope; the
# report discloses 'EQ cannot fill these' with the numbers." The exclusion half
# shipped in PR-4/PR-5 (``evaluate_flat_spec`` drops the masked bins from both
# the reference and every deviation). These pin the DISCLOSURE half.
# --------------------------------------------------------------------------- #

# Hardware nouns the shipped copy must never contain. The program's own rule:
# generalize by contract and measured evidence, never by device taxonomy — the
# JTS3 rim-wave attribution is session knowledge, not measured general truth,
# and "horn rim never appears in shipped copy" is stated verbatim in the work
# order's PR-7 section. Also barred: the room/furniture nouns a single session
# cannot establish (S0 separated source-fixed from room-fixed only by MOVING
# the speaker), which is exactly what the position_invariant copy must not
# claim to know.
_FORBIDDEN_COPY_NOUNS = (
    "horn", "rim", "baffle", "cabinet", "waveguide", "dome", "tweeter",
    "woofer", "driver", "enclosure", "port", "desk", "wall", "floor",
    "ceiling", "table", "furniture",
)


def _copy_strings(carve_outs) -> list[str]:
    """Every household-visible string the carve-out payload carries."""
    strings: list[str] = []
    for band in carve_outs:
        strings.append(band["disclosure"])
        strings.append(band["expert"])
        strings.extend(record["reason"] for record in band["intervals"])
    return [text for text in strings if text]


def _assert_copy_is_hardware_blind(carve_outs) -> None:
    strings = _copy_strings(carve_outs)
    assert strings, "the fixture must produce copy for this check to check any"
    for text in strings:
        lowered = text.lower()
        for noun in _FORBIDDEN_COPY_NOUNS:
            assert not re.search(rf"\b{noun}s?\b", lowered), (noun, text)


def _fake_null(f_lo, f_hi, center, n=2):
    return SimpleNamespace(
        f_lo_hz=f_lo, f_hi_hz=f_hi, f_center_hz=center, n=n, tau_us=300.0,
        r_time=0.37, r_freq=0.35, depth_db=6.0,
        classification="position_invariant",
    )


def _flat_report(n_bins: int = 500):
    return evaluate_flat_spec(
        np.geomspace(200.0, 20_000.0, n_bins), np.zeros(n_bins)
    )


def test_carve_outs_cover_every_spec_band_in_the_reports_own_order():
    """One entry per spec band, always, in ``spec["bands"]`` order — so a
    consumer joins by index or by ``band_hz`` and can render "nothing carved
    here" without inferring it from an absence."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    carve_outs = result["carve_outs"]
    assert [c["band_hz"] for c in carve_outs] == [
        [b["f_lo_hz"], b["f_hi_hz"]] for b in result["spec"]["bands"]
    ]
    assert all(
        {"band_hz", "intervals", "disclosure", "expert"} == set(c)
        for c in carve_outs
    )


def test_carve_outs_carry_tau_r_and_classification_as_the_reason_of_record():
    """The registry rows ARE the exclusion reason of record: τ, r (both
    estimates), the rung, the depth and the classification ride each carved
    range, so the UI — or a later reader of the persisted artifact — can answer
    "why is this band excluded" without re-running the gate."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    nulls = [
        record
        for band in result["carve_outs"]
        for record in band["intervals"]
        if record["source"] == "identified_null"
    ]
    assert nulls, "the position-invariant fixture must produce identified nulls"
    registry = {n["f_center_hz"]: n for n in result["null_registry"]["nulls"]}
    for record in nulls:
        source = registry[record["f_center_hz"]]
        # Copied from the registry, never re-derived — every number must be the
        # same bytes the registry recorded.
        for field in (
            "n", "tau_us", "r_time", "r_freq", "depth_db", "classification",
            "f_lo_hz", "f_hi_hz",
        ):
            assert record[field] == source[field], field
        assert record["reason"]


def test_carve_out_expert_line_quotes_tau_and_both_r_estimates():
    """τ/r live in the EXPERT register, never the headline (the plan: "τ/r
    vocabulary lives in an expert disclosure, not the headline"). The two r
    estimates are reported as a pair because their AGREEMENT is what admitted
    the null; an averaged figure would hide it."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    with_nulls = [
        band for band in result["carve_outs"]
        if any(r["source"] == "identified_null" for r in band["intervals"])
    ]
    assert with_nulls
    for band in with_nulls:
        first = next(r for r in band["intervals"] if r["source"] == "identified_null")
        assert f"τ {first['tau_us']:.0f} µs" in band["expert"]
        assert f"{first['r_time']:.3f} measured in time" in band["expert"]
        assert f"{first['r_freq']:.3f} implied by null depth" in band["expert"]
        # The headline stays plain language: no τ/r vocabulary in it.
        assert "τ" not in band["disclosure"]
        assert "reflection ratio" not in band["disclosure"]
        assert "EQ cannot fill" in band["disclosure"]


def test_carve_out_copy_names_no_hardware_and_no_room_furniture():
    """Copy discipline, pinned. Anomaly classification is evidence about how a
    null behaved across a mic cloud; it is never evidence about which PART of a
    speaker or room produced it. A single word here is the difference between a
    hardware-abstracted product and one that ships JTS3's own horn in its copy.
    """
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    _assert_copy_is_hardware_blind(result["carve_outs"])


def test_position_invariant_copy_never_claims_which_of_the_two_it_is():
    """The pre-registered phrasing (plan PR-1's classification vocabulary): a
    single session cannot separate "travels with the speaker" from "a fixed
    path in the room", so the copy names BOTH and names the experiment that
    would tell them apart. S0 separated them only by moving the speaker."""
    from jasper.active_speaker.crossover_v2_flow import _null_classification_copy
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_INSUFFICIENT_EVIDENCE,
        CLASSIFICATION_POSITION_DEPENDENT,
        CLASSIFICATION_POSITION_INVARIANT,
    )

    invariant = _null_classification_copy(CLASSIFICATION_POSITION_INVARIANT)
    assert "travels with the speaker" in invariant
    assert " or " in invariant and "did not change while measuring" in invariant
    assert "moving the speaker and measuring again would tell those apart" in invariant

    dependent = _null_classification_copy(CLASSIFICATION_POSITION_DEPENDENT)
    assert "does not travel with the speaker" in dependent

    # A classification with no copy says nothing rather than guessing. Only a
    # report-level verdict carries this value, and such a report has no nulls.
    assert _null_classification_copy(CLASSIFICATION_INSUFFICIENT_EVIDENCE) == ""


def test_a_carve_out_appears_under_every_spec_band_it_actually_carves():
    """Overlap, not containment: a null straddling a band edge removes bins
    from BOTH bands, so it is disclosed under both. Clipping its interval to a
    band edge would attach τ/r to a fragment of what was measured, so the
    interval stays the null's own."""
    from jasper.active_speaker.crossover_v2_flow import carve_outs_by_band

    straddling = SimpleNamespace(nulls=[_fake_null(7800.0, 8200.0, 8000.0)])
    bands = carve_outs_by_band(_flat_report(), straddling, ())
    assert [b["band_hz"] for b in bands if b["intervals"]] == [
        [2000.0, 8000.0], [8000.0, 16000.0],
    ]
    for band in bands:
        for record in band["intervals"]:
            assert (record["f_lo_hz"], record["f_hi_hz"]) == (7800.0, 8200.0)


def test_the_screen_and_the_registry_stay_separately_attributed():
    """Both honesty instruments carve; only the registry's rows have τ/r. The
    rows are NOT merged, because a merged interval loses which instrument found
    it — and "both flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view for counting."""
    from jasper.active_speaker.crossover_v2_flow import carve_outs_by_band

    registry = SimpleNamespace(nulls=[_fake_null(9000.0, 9400.0, 9200.0)])
    # The screen interval deliberately OVERLAPS the registry's own.
    bands = carve_outs_by_band(_flat_report(), registry, ((9100.0, 9600.0),))
    top = next(b for b in bands if b["band_hz"] == [8000.0, 16000.0])

    assert [r["source"] for r in top["intervals"]] == [
        "identified_null", "position_screen",
    ]
    screened = top["intervals"][1]
    assert "tau_us" not in screened and "r_time" not in screened
    assert "microphone positions disagreed" in screened["reason"]
    # Both are named in the one headline, and the screened one is counted.
    assert "One further range is left out" in top["disclosure"]


def test_severing_the_registry_costs_the_carve_out_its_reason_of_record():
    """The "delete one input, the test must fail" property for the disclosure:
    without the registry the same band still loses bins to the screen, but the
    expert line — the τ/r that IS the reason of record — disappears entirely."""
    from jasper.active_speaker.crossover_v2_flow import carve_outs_by_band

    registry = SimpleNamespace(nulls=[_fake_null(9000.0, 9400.0, 9200.0)])
    with_registry = next(
        b for b in carve_outs_by_band(_flat_report(), registry, ((9100.0, 9600.0),))
        if b["band_hz"] == [8000.0, 16000.0]
    )
    without = next(
        b for b in carve_outs_by_band(
            _flat_report(), SimpleNamespace(nulls=[]), ((9100.0, 9600.0),)
        )
        if b["band_hz"] == [8000.0, 16000.0]
    )
    assert with_registry["expert"] and not without["expert"]
    assert "delayed copy of the sound" in with_registry["disclosure"]
    assert "delayed copy of the sound" not in without["disclosure"]
    assert without["disclosure"], "the screen's own carve-out is still disclosed"


def test_the_gate_validity_clamp_never_appears_as_a_carve_out():
    """``carve_outs_by_band``'s docstring says the clamp is NOT included and
    that a band's ``n_excluded`` can therefore exceed what the carve-outs
    cover. Pinned structurally rather than left as prose.

    The clamp has no path into the records by construction —
    ``carve_outs_by_band`` is handed the registry and the SCREEN's intervals,
    and never sees ``validity_floor_hz`` at all — so this drives the real
    assembly with a clamp high enough to remove a whole spec band's worth of
    bins and checks both halves of the claim: the carve-outs are unmoved, and
    the divergence it creates between ``n_excluded`` and the carved intervals
    is real (which is exactly why ``validity_floor_hz`` is disclosed
    separately)."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    unclamped = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    clamped = assemble_cloud_group_result(
        combined, echo_band_hz=SYNTHETIC_BAND_HZ, validity_floor_hz=3000.0,
    )

    # The clamp moved real bins out of grading...
    assert clamped["validity_floor_hz"] == 3000.0
    excluded_before = sum(b["n_excluded"] for b in unclamped["spec"]["bands"])
    excluded_after = sum(b["n_excluded"] for b in clamped["spec"]["bands"])
    assert excluded_after > excluded_before

    # ...and changed NOTHING about the carve-outs, which speak only for the
    # honesty instruments.
    assert json.dumps(clamped["carve_outs"], sort_keys=True) == json.dumps(
        unclamped["carve_outs"], sort_keys=True
    )
    # Nor did it reach ``merged_excluded_bands_hz`` (PR-5's own separation).
    assert clamped["merged_excluded_bands_hz"] == unclamped["merged_excluded_bands_hz"]


def test_carve_outs_survive_a_registry_that_identified_nothing():
    """A report whose ``reason`` is non-empty has no nulls at all. The payload
    must still be complete (one entry per band) rather than absent — an absent
    key and "nothing was carved" are different facts and only one is true."""
    combined = combine_positions(_unlocked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)

    assert result["null_registry"]["nulls"] == []
    assert len(result["carve_outs"]) == len(result["spec"]["bands"])
    assert all(
        record["source"] == "position_screen"
        for band in result["carve_outs"]
        for record in band["intervals"]
    )


# --------------------------------------------------------------------------- #
# PR-6b: the carve-out reaches every surface the flatness gauge does
# --------------------------------------------------------------------------- #


def _walk_to_envelope(result):
    """Pipeline result → durable ``cloud`` block → ``_compact_cloud_status``
    → the wizard envelope. The REAL functions, in the host's own order — the
    same walk ``tests/test_flat_spec_ssot.py`` uses for the flatness gauge."""
    from jasper.web.correction_crossover_v2 import _compact_cloud_status

    compact = _compact_cloud_status(
        {PHASE_CLOUD_VERIFY: {"geometry": result["geometry"], "pipeline": result}}
    )
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done", "verify": {"outcome": "pass"}, "cloud": compact,
        },
    })
    return compact, envelope


def test_carve_outs_reach_state_and_the_envelope_byte_identically():
    """The projection walk, mirroring PR-5's frame-consistency contract: one
    producer, copied — never a second derivation on the way to a surface."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    compact, envelope = _walk_to_envelope(result)

    canonical = json.dumps(result["carve_outs"], sort_keys=True)
    assert json.dumps(
        compact[PHASE_CLOUD_VERIFY]["carve_outs"], sort_keys=True
    ) == canonical
    assert json.dumps(
        envelope["cloud"][PHASE_CLOUD_VERIFY]["carve_outs"], sort_keys=True
    ) == canonical

    # And the household-visible expert disclosure carries the τ/r line, band
    # prefixed — the part that renders TODAY, before PR-7's chart exists.
    expert = [line for line in envelope["expert_details"] if "carved out" in line]
    assert expert, envelope["expert_details"]
    assert all("µs" in line and "reflection ratio" in line for line in expert)


def test_the_pre_apply_cloud_never_discloses_its_carve_outs_as_the_verdict():
    """Inherited from ``_flatness_details_lines``: ``cloud_measure`` is the
    UNCORRECTED baseline that exists in order to be out of spec, so its
    carve-outs must not render on the screen that answers "how did your speaker
    come out". They stay on the payload for PR-7 and the artifact."""
    combined = combine_positions(_locked_cloud(), echo_band_hz=SYNTHETIC_BAND_HZ)
    result = assemble_cloud_group_result(combined, echo_band_hz=SYNTHETIC_BAND_HZ)
    from jasper.web.correction_crossover_v2 import _compact_cloud_status

    compact = _compact_cloud_status(
        {PHASE_CLOUD_MEASURE: {"geometry": result["geometry"], "pipeline": result}}
    )
    assert compact[PHASE_CLOUD_MEASURE]["carve_outs"], "still on the payload"

    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": "done", "verify": {"outcome": "pass"}, "cloud": compact,
        },
    })
    assert envelope["expert_details"] == []


def test_an_unavailable_pipeline_projects_no_carve_outs():
    """Same rule as ``excluded_interval_count``: a pipeline that never ran must
    not project anything a reader could take for "we looked and found
    nothing"."""
    from jasper.web.correction_crossover_v2 import _compact_cloud_status

    compact = _compact_cloud_status({
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False},
            "pipeline": {"available": False, "reason": "combine_failed"},
        }
    })
    entry = compact[PHASE_CLOUD_VERIFY]
    assert entry["carve_outs"] == []
    assert entry["excluded_interval_count"] is None
    assert entry["flatness"] is None


# --------------------------------------------------------------------------- #
# PR-6b, corpus-gated: the carve-out on the real S0 cloud
# --------------------------------------------------------------------------- #


@corpus.requires_s0_curves
def test_the_real_s0_carve_out_discloses_the_measured_comb():
    """The disclosure owner decision 1 committed to, against real hardware
    data. Measured 2026-07-27 on the S0 main leg (10 positions, JTS3 — the same
    cloud PR-4's own corpus acceptance uses, at the same band): the null gate
    identifies THREE rungs of one ladder inside 8-16 kHz — 8.7, 11.6 and
    15.0 kHz — at a fitted τ of 299 µs, with r 0.375 in the time domain against
    0.349 implied by the null depths. All three classify
    ``position_invariant``. (These are this pipeline's own figures on this
    grouping; the plan doc's § b quotes the S0 kit's independently-computed
    0.373/0.342 for the same session, which is a different construction and
    not the number pinned here.)

    Two properties beyond the numbers:

    * the 250 Hz-2 kHz band's carve-out is the power-vs-median SCREEN's, not
      the registry's — the 1.7-1.9 kHz dip the plan doc's § e.1 records the
      screen catching correctly — so the two instruments are visibly doing
      different work on one real cloud;
    * the 2-8 kHz band carves NOTHING and says nothing, which is what a clean
      band has to look like.
    """
    echo_band_hz = (5000.0, 19_000.0)
    combined = combine_positions(
        corpus.s0_position_captures(corpus.S0_MAIN),
        echo_band_hz=echo_band_hz,
        signal_band_hz=corpus.S0_SUMMED_PASSBAND_HZ,
    )
    result = assemble_cloud_group_result(combined, echo_band_hz=echo_band_hz)
    by_band = {tuple(c["band_hz"]): c for c in result["carve_outs"]}

    top = by_band[(8000.0, 16000.0)]
    nulls = [r for r in top["intervals"] if r["source"] == "identified_null"]
    assert len(nulls) == 3
    assert [round(r["f_center_hz"] / 1000.0, 1) for r in nulls] == [8.7, 11.6, 15.0]
    assert [r["n"] for r in nulls] == [2, 3, 4]
    assert all(r["classification"] == "position_invariant" for r in nulls)
    assert round(nulls[0]["tau_us"]) == 299
    assert round(nulls[0]["r_time"], 3) == 0.375
    assert round(nulls[0]["r_freq"], 3) == 0.349
    assert top["disclosure"] == (
        "Interference nulls at 8.7 kHz, 11.6 kHz and 15.0 kHz — a delayed copy "
        "of the sound arrives 0.30 ms later. EQ cannot fill these, so they are "
        "left out of correction and out of this band's grading."
    )

    # The screen's own catch, in a different band, from a different instrument.
    low = by_band[(250.0, 2000.0)]
    assert [r["source"] for r in low["intervals"]] == ["position_screen"]
    assert 1700.0 < low["intervals"][0]["f_lo_hz"] < 1900.0
    assert low["expert"] == "", "the screen carries no τ/r to disclose"

    # A clean band says nothing at all.
    mid = by_band[(2000.0, 8000.0)]
    assert mid["intervals"] == []
    assert mid["disclosure"] == "" and mid["expert"] == ""

    # The ±2.5 dB row is NOT re-specified by the carve-out (owner decision 1:
    # the carve-out is disclosed, not re-specified) — the tolerance the
    # surviving envelope is judged against is still the spec table's number.
    top_band = next(b for b in result["spec"]["bands"] if b["f_lo_hz"] == 8000.0)
    assert top_band["tolerance_db"] == 2.5

    _assert_copy_is_hardware_blind(result["carve_outs"])


@corpus.requires_s0_curves
def test_the_carve_out_payload_is_the_biggest_thing_on_a_state_entry():
    """``_compact_cloud_status``'s docstring says so, so it is checked.

    Every other key on that entry is a reduction; this one is deliberately
    not, and the cost lands on `/state` and every envelope poll. Measured
    2026-07-27 on the S0 ten-position cloud — the widest real case this program
    has — the carve-outs were 3162 of the entry's 4056 JSON bytes.

    The DIGITS are not pinned: they move by tens of bytes on any copy edit, and
    a test that failed on a reworded sentence would train people to update the
    number without reading it. What is pinned is the structural claim the
    docstring rests on — four rows on this corpus, and this key outweighing
    every other on the entry combined — plus a generous kB band, so a change
    that made the payload an order of magnitude larger still fails here."""
    from jasper.web.correction_crossover_v2 import _compact_cloud_status

    echo_band_hz = (5000.0, 19_000.0)
    combined = combine_positions(
        corpus.s0_position_captures(corpus.S0_MAIN),
        echo_band_hz=echo_band_hz,
        signal_band_hz=corpus.S0_SUMMED_PASSBAND_HZ,
    )
    result = assemble_cloud_group_result(combined, echo_band_hz=echo_band_hz)
    entry = _compact_cloud_status(
        {PHASE_CLOUD_VERIFY: {"geometry": result["geometry"], "pipeline": result}}
    )[PHASE_CLOUD_VERIFY]

    rows = [r for band in entry["carve_outs"] for r in band["intervals"]]
    assert [r["source"] for r in rows] == [
        "position_screen", "identified_null", "identified_null", "identified_null",
    ]
    carve_out_bytes = len(json.dumps(entry["carve_outs"]))
    others = sum(
        len(json.dumps(entry[key]))
        for key in entry
        if key != "carve_outs"
    )
    assert carve_out_bytes > others
    assert 2000 < carve_out_bytes < 6000, carve_out_bytes
