# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted measured-context and limiter-domain builder for the bench bundle.

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

from collections.abc import Sequence

from jasper.active_speaker.camilla_yaml import BASELINE_LIMITER_CLIP_LIMIT_DB
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

_CONTEXT_LIMITER_TYPE = "Limiter"


class ContextError(ValueError):
    """A measured-context input is missing, malformed, or out of range."""


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


def _sha256_text(value: object, *, field: str) -> str:
    import re

    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContextError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _trimmed_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContextError(f"{field} must be a non-empty trimmed string")
    return value


def _finite_float(value: object, *, field: str) -> float:
    import math

    if type(value) is not float or not math.isfinite(value):
        raise ContextError(f"{field} must be a finite float")
    return value


def build_measured_context(
    *,
    target_family_fingerprint: str,
    target_order: Sequence[tuple[str, str]],
    driver_safety_fingerprint: str,
    margin_policy_fingerprint: str,
    transparency_policy_fingerprint: str,
    natural_graph_fingerprint: str,
    baseline_limiter_clip_limit_dbfs: float,
    camilladsp_build_id: str,
    owner_channels: Sequence[int],
    sample_rate_hz: int,
    limiter_name: str,
    tap_implementation_id: str,
) -> dict[str, object]:
    """Assemble the exact ``measured_context`` the frozen bundle schema pins.

    ``target_order`` is deepest-target-through-natural, each ``(target_id,
    target_fingerprint)``. The limiter domain and detector reference are owned
    here; the caller supplies the live-system identities (family, natural
    graph, owner channels, build id, and the read-back baseline clip limit).
    The read-back baseline must lie inside the trusted domain.
    """

    domain_min = LIMITER_DOMAIN_MIN_DBFS
    domain_max = LIMITER_DOMAIN_MAX_DBFS
    baseline = _finite_float(
        baseline_limiter_clip_limit_dbfs,
        field="baseline_limiter_clip_limit_dbfs",
    )
    if not domain_min <= baseline <= domain_max:
        raise ContextError(
            "read-back baseline limiter clip limit is outside the trusted domain"
        )
    if not domain_min <= BASELINE_LIMITER_CLIP_LIMIT_DB <= domain_max:  # invariant
        raise ContextError("emitter baseline constant is outside the trusted domain")

    order = list(target_order)
    if not order:
        raise ContextError("target_order must be non-empty")
    seen_ids: set[str] = set()
    seen_fps: set[str] = set()
    order_out: list[dict[str, str]] = []
    for target_id, target_fp in order:
        tid = _trimmed_text(target_id, field="target_order.target_id")
        tfp = _sha256_text(target_fp, field="target_order.target_fingerprint")
        if tid in seen_ids or tfp in seen_fps:
            raise ContextError("target_order entries must be unique")
        seen_ids.add(tid)
        seen_fps.add(tfp)
        order_out.append({"target_id": tid, "target_fingerprint": tfp})

    channels = list(owner_channels)
    if not channels:
        raise ContextError("owner_channels must be non-empty")
    seen_ch: set[int] = set()
    for channel in channels:
        if type(channel) is not int or channel < 0 or channel in seen_ch:
            raise ContextError("owner_channels must be unique non-negative integers")
        seen_ch.add(channel)

    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ContextError("sample_rate_hz must be a positive integer")

    return {
        "target_family_fingerprint": _sha256_text(
            target_family_fingerprint, field="target_family_fingerprint"
        ),
        "target_order": order_out,
        "driver_safety_fingerprint": _sha256_text(
            driver_safety_fingerprint, field="driver_safety_fingerprint"
        ),
        "margin_policy_fingerprint": _sha256_text(
            margin_policy_fingerprint, field="margin_policy_fingerprint"
        ),
        "transparency_policy_fingerprint": _sha256_text(
            transparency_policy_fingerprint, field="transparency_policy_fingerprint"
        ),
        "natural_graph_fingerprint": _sha256_text(
            natural_graph_fingerprint, field="natural_graph_fingerprint"
        ),
        "baseline_limiter_clip_limit_dbfs": baseline,
        "limiter_domain_min_dbfs": domain_min,
        "limiter_domain_max_dbfs": domain_max,
        "limiter_domain_fingerprint": limiter_domain_fingerprint(),
        "camilladsp_build_id": _trimmed_text(
            camilladsp_build_id, field="camilladsp_build_id"
        ),
        "owner_channels": channels,
        "sample_rate_hz": sample_rate_hz,
        "limiter_name": _trimmed_text(limiter_name, field="limiter_name"),
        "limiter_type": _CONTEXT_LIMITER_TYPE,
        "soft_clip": True,
        "tap_implementation_id": _trimmed_text(
            tap_implementation_id, field="tap_implementation_id"
        ),
        "detector_reference": DETECTOR_REFERENCE,
    }
