# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The limiter domain is bound to the emitter's real validation, not invented."""

from __future__ import annotations

import re
from pathlib import Path

from jasper.active_speaker import camilla_yaml
from jasper.bass_extension.bench.context import (
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
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
