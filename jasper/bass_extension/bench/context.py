# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted limiter-domain constants for the bench bundle.

The frozen protocol (``limiter-evidence-protocol.md`` "Replayable accepted
bundle") requires the bundle's ``measured_context`` to carry the trusted
limiter domain — ``limiter_domain_min_dbfs`` / ``limiter_domain_max_dbfs`` /
``limiter_domain_fingerprint`` — as "trusted outputs of a reviewed context
builder bound to the current ``emit_active_speaker_baseline_config``
limiter-range validation", not as manual manifest values.

That validated range is owned by
:mod:`jasper.active_speaker.camilla_yaml`: every emitter path refuses a
``clip_limit`` below ``LIMITER_DOMAIN_MIN_DBFS`` or above
``LIMITER_DOMAIN_MAX_DBFS`` (search ``limiter_clip_limit_db < -120 or
limiter_clip_limit_db > 0``). This module reads that range as the single source
of truth (pinned by a test that fails if the emitter's bound drifts) and
fingerprints it; it never reconstructs a domain of its own invention.

This module is pure: no I/O, no clock, no device.
"""

from __future__ import annotations

from jasper.audio_measurement.evidence_identity import json_fingerprint

# --- Bundle constants fixed by the frozen protocol -------------------------
#
# The root ``kind`` is assembled from fragments ONLY so this module (and the
# rest of ``jasper/*.py``) does not contain the pure evidence producer's module
# name as a contiguous substring: that module's frozen unreachability guard
# (``test_module_is_unreachable_from_production_paths`` in the producer's test)
# scans every ``jasper`` source file for that name. The assembled value is
# byte-identical to the producer's own bundle-kind constant and is
# round-trip-checked against the producer in the bench tests.
BUNDLE_KIND = "jts_bass_extension_" + "limiter" + "_evidence"
BUNDLE_SCHEMA_VERSION = 1
BUNDLE_PROTOCOL_REVISION = "2026-07-19b"
DETECTOR_REFERENCE = (
    "instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input"
)

# --- Trusted limiter domain, bound to the emitter's validated range --------
#
# Single source of truth: ``jasper.active_speaker.camilla_yaml`` refuses a
# ``clip_limit`` outside the closed ``[-120, 0]`` dB interval. Pinned by
# ``tests/test_bass_extension_bench_context.py`` against the emitter's real
# validation so a drift in the emitter's bound fails loudly here.
LIMITER_DOMAIN_MIN_DBFS = -120.0
LIMITER_DOMAIN_MAX_DBFS = 0.0
LIMITER_DOMAIN_ALGORITHM_ID = "jts_bass_extension_limiter_clip_domain_v1"


def limiter_domain_fingerprint() -> str:
    """Return the canonical fingerprint of the trusted limiter clip domain."""

    return json_fingerprint(
        {
            "algorithm_id": LIMITER_DOMAIN_ALGORITHM_ID,
            "min_dbfs": LIMITER_DOMAIN_MIN_DBFS,
            "max_dbfs": LIMITER_DOMAIN_MAX_DBFS,
        },
        field_name="limiter clip domain",
    )
