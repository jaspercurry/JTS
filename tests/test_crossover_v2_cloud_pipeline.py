# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The cloud JSON decimation, the measurement band, the geometry guidance copy,
and the per-band flatness journal field."""
from __future__ import annotations


import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
from jasper.active_speaker.crossover_v2.programs import measurement_band_hz
from jasper.active_speaker.crossover_v2.spatial import (
    CLOUD_CURVE_MAX_JSON_POINTS,
    _decimate_curve_for_json,
    _geometry_guidance_copy,
)
from jasper.active_speaker.crossover_v2.verification import _per_band_flatness_log_field
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand


# --------------------------------------------------------------------------- #
# Synthetic two-path cloud (local to this file — a different test file's own
# fixture generator is not a shared-library import; this is the same ~15-line
# shape test_interference_nulls.py builds, reused in spirit, not coupled).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [0, 1, 512, 513, 1023, 1024, 1535])
def test_cloud_curve_serialization_bounds_paired_ordered_samples(size):
    freqs = np.arange(size, dtype=float)
    magnitudes = -0.25 * freqs

    curve = _decimate_curve_for_json(freqs, magnitudes)
    kept = curve["freqs_hz"]

    assert len(kept) <= CLOUD_CURVE_MAX_JSON_POINTS
    assert len(kept) == len(curve["magnitude_db"])
    np.testing.assert_array_equal(curve["magnitude_db"], -0.25 * np.asarray(kept))
    if size:
        assert kept[0] == freqs[0]
        assert np.all(np.diff(kept) > 0)
    if size <= CLOUD_CURVE_MAX_JSON_POINTS:
        np.testing.assert_array_equal(kept, freqs)


# --------------------------------------------------------------------------- #
# measurement_band_hz
# --------------------------------------------------------------------------- #


def test_measurement_band_hz_unions_both_roles():
    roles = [
        RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
    ]
    assert measurement_band_hz(roles) == (45.0, 20000.0)


def test_measurement_band_hz_is_order_independent():
    a = RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0))
    b = RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0))
    assert measurement_band_hz([a, b]) == measurement_band_hz([b, a])


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
    gradient -- the two copy variants must differ (softened, not suppressed),
    and the thin variant must not claim a percentage/gradient."""
    locked = _geometry_guidance_copy({"locked": True, "thin_evidence": False})
    thin = _geometry_guidance_copy({"locked": True, "thin_evidence": True})
    assert thin != locked
    assert thin
    assert "%" not in thin


# --------------------------------------------------------------------------- #
# #1857: the crossover_v2_cloud_spec log line names every band, not just the
# one flatness_max_db happened to flag as worst
# --------------------------------------------------------------------------- #


def test_per_band_flatness_log_field_charges_each_band_its_own_deviation():
    """#1857's shape, read from the log-line helper, against the low-mid frame.

    A uniformly dark tweeter used to drag the shared reference down and
    inflate every OTHER band's number with it. On this shape that drag was
    2.03 dB: the woofer's lone +3 dB bin read +5.03, the tweeter's honest
    -6 read -3.97, and the top band -- level with the woofer and flat --
    read +2.03 out of nothing at all.

    The reference is now pooled over the low-mid band alone
    (``flat_spec.REFERENCE_BAND_HZ``), which no band above 2 kHz is inside,
    so each band is charged its own deviation and nothing else's: +3.00,
    -6.00, -0.00. This is the frame ruling landing, asserted where the
    misattribution was reproduced.
    """
    n = 1000
    woofer_freqs = np.linspace(250.0, 1999.0, n)
    tweeter_freqs = np.linspace(2000.0, 7999.0, n)
    top_freqs = np.linspace(8000.0, 15999.0, n)
    freqs = np.concatenate([woofer_freqs, tweeter_freqs, top_freqs])
    woofer_curve = np.zeros(n)
    woofer_curve[n // 2] = 3.0
    tweeter_curve = np.full(n, -6.0)
    top_curve = np.zeros(n)
    curve = np.concatenate([woofer_curve, tweeter_curve, top_curve])
    order = np.argsort(freqs)
    report = evaluate_flat_spec(freqs[order], curve[order], None)

    field = _per_band_flatness_log_field(report.to_dict()["bands"])
    assert field == (
        "250-2000Hz:+3.00dB:fail;2000-8000Hz:-6.00dB:fail;8000-16000Hz:-0.00dB:pass"
    )
    # The band that was never touched now reads as untouched, and the dark
    # tweeter is charged its whole deficit instead of sharing it out.
    assert "8000-16000Hz:-0.00dB" in field
    assert "2000-8000Hz:-6.00dB" in field


def test_per_band_flatness_log_field_uniformly_flat():
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    report = evaluate_flat_spec(freqs, np.zeros_like(freqs), None)
    field = _per_band_flatness_log_field(report.to_dict()["bands"])
    assert field == "250-2000Hz:+0.00dB:pass;2000-8000Hz:+0.00dB:pass;8000-16000Hz:+0.00dB:pass"


def test_per_band_flatness_log_field_single_band_defect():
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    curve = np.where(freqs >= 8000.0, -6.0, 0.0)
    report = evaluate_flat_spec(freqs, curve, None)
    field = _per_band_flatness_log_field(report.to_dict()["bands"])
    assert field.startswith("250-2000Hz:+0.00dB:pass;2000-8000Hz:+0.00dB:pass;")
    assert field.endswith("8000-16000Hz:-6.00dB:fail")


def test_per_band_flatness_log_field_both_bands_failing():
    """Edge case named in #1857's remedy: both the woofer and tweeter
    genuinely out of spec (not one dragging the other) -- the field must
    show both, not just the one flatness_max_db picked."""
    bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "evaluable": True, "within_target": False,
         "max_deviation_db": 3.0},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "evaluable": True, "within_target": False,
         "max_deviation_db": -4.5},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "evaluable": True, "within_target": True,
         "max_deviation_db": 1.0},
    ]
    assert _per_band_flatness_log_field(bands) == (
        "250-2000Hz:+3.00dB:fail;2000-8000Hz:-4.50dB:fail;8000-16000Hz:+1.00dB:pass"
    )


def test_per_band_flatness_log_field_skips_unevaluable_bands():
    bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "evaluable": False, "within_target": None,
         "max_deviation_db": None},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "evaluable": True, "within_target": False,
         "max_deviation_db": -4.5},
    ]
    assert _per_band_flatness_log_field(bands) == "2000-8000Hz:-4.50dB:fail"


def test_per_band_flatness_log_field_empty_or_malformed_input():
    assert _per_band_flatness_log_field([]) == ""
    assert _per_band_flatness_log_field(None) == ""
    assert _per_band_flatness_log_field("not a list") == ""
    assert _per_band_flatness_log_field([{"evaluable": True}, "not a mapping"]) == ""


# --------------------------------------------------------------------------- #
# PR-6b: the carve-out reaches every surface the flatness gauge does
# --------------------------------------------------------------------------- #


def test_an_unavailable_pipeline_projects_no_carve_outs():
    """Same rule as ``excluded_interval_count``: a pipeline that never ran must
    not project anything a reader could take for "we looked and found
    nothing"."""
    from jasper.active_speaker.crossover_envelope_v2 import compact_cloud_status

    compact = compact_cloud_status({
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False},
            "pipeline": {"available": False, "reason": "combine_failed"},
        }
    })
    entry = compact[PHASE_CLOUD_VERIFY]
    assert entry["carve_outs"] == []
    assert entry["excluded_interval_count"] is None
    assert entry["flatness"] is None
