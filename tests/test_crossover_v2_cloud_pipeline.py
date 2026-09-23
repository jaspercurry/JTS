# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement band, the geometry guidance copy, and the per-band flatness
journal field."""
from __future__ import annotations


from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
from jasper.active_speaker.crossover_v2.programs import measurement_band_hz
from jasper.active_speaker.crossover_v2.spatial import _geometry_guidance_copy
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand


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
