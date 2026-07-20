# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The limiter domain is bound to the emitter's real validation, not invented."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from jasper.active_speaker import camilla_yaml
from jasper.bass_extension.bench.context import (
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
    ContextError,
    build_measured_context,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _context(baseline: float = -1.0):
    return build_measured_context(
        target_family_fingerprint=_sha("family"),
        target_order=[("deep", _sha("t:deep")), ("natural", _sha("t:natural"))],
        driver_safety_fingerprint=_sha("ds"),
        margin_policy_fingerprint=_sha("mp"),
        transparency_policy_fingerprint=_sha("tp"),
        natural_graph_fingerprint=_sha("ng"),
        baseline_limiter_clip_limit_dbfs=baseline,
        camilladsp_build_id="build",
        owner_channels=[2],
        sample_rate_hz=48_000,
        limiter_name="baseline_limiter_woofer",
        tap_implementation_id="tap",
    )


def test_domain_endpoints_match_the_emitter_validated_range() -> None:
    # Pin the domain to the emitter's real clip_limit validation so a drift in
    # the emitter's bound fails here (the emitter refuses clip_limit < -120 or
    # > 0 at every emit path).
    source = Path(camilla_yaml.__file__).read_text(encoding="utf-8")
    bound = re.compile(
        r"limiter_clip_limit_db\s*<\s*-120\s+or\s+limiter_clip_limit_db\s*>\s*0"
    )
    assert bound.search(source), "emitter limiter clip_limit validation drifted"
    assert LIMITER_DOMAIN_MIN_DBFS == -120.0
    assert LIMITER_DOMAIN_MAX_DBFS == 0.0


def test_emitter_baseline_constant_lies_inside_the_domain() -> None:
    assert (
        LIMITER_DOMAIN_MIN_DBFS
        <= camilla_yaml.BASELINE_LIMITER_CLIP_LIMIT_DB
        <= LIMITER_DOMAIN_MAX_DBFS
    )


def test_build_measured_context_shape_and_domain_fields() -> None:
    context = _context()
    assert context["limiter_domain_min_dbfs"] == LIMITER_DOMAIN_MIN_DBFS
    assert context["limiter_domain_max_dbfs"] == LIMITER_DOMAIN_MAX_DBFS
    assert context["limiter_type"] == "Limiter"
    assert context["soft_clip"] is True
    assert context["detector_reference"].endswith("_at_limiter_input")
    assert [entry["target_id"] for entry in context["target_order"]] == ["deep", "natural"]
    assert context["owner_channels"] == [2]


def test_read_back_baseline_outside_domain_is_refused() -> None:
    with pytest.raises(ContextError):
        _context(baseline=-130.0)
    with pytest.raises(ContextError):
        _context(baseline=1.0)


def test_duplicate_owner_channels_are_refused() -> None:
    with pytest.raises(ContextError):
        build_measured_context(
            target_family_fingerprint=_sha("family"),
            target_order=[("deep", _sha("t:deep"))],
            driver_safety_fingerprint=_sha("ds"),
            margin_policy_fingerprint=_sha("mp"),
            transparency_policy_fingerprint=_sha("tp"),
            natural_graph_fingerprint=_sha("ng"),
            baseline_limiter_clip_limit_dbfs=-1.0,
            camilladsp_build_id="build",
            owner_channels=[2, 2],
            sample_rate_hz=48_000,
            limiter_name="baseline_limiter_woofer",
            tap_implementation_id="tap",
        )
