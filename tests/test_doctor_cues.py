# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor cue-cache domain."""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.cli.doctor import cues
from jasper.cues.registry import CueDef

from .doctor_test_support import _fresh_cfg

# A primary cue plus its fallback target — enough to exercise every branch
# of the fallback chain without depending on the real registry's shape.
_FALLBACK = CueDef(slug="fallback_cue", template="fallback", description="d")
_PRIMARY = CueDef(
    slug="primary_cue", template="primary", description="d", fallback="fallback_cue",
)
_FIXTURE_CUES = (_PRIMARY, _FALLBACK)


@pytest.mark.parametrize(
    "populate, status, reason_name",
    [
        # (a) both cues cached under their current hash.
        ("current", "ok", None),
        # (b) both cues cached, but under a stale hash.
        ("stale", "warn", "REASON_CUE_CACHE_STALE"),
        # (c) nothing cached anywhere, and the fallback has nothing either.
        ("nothing", "fail", "REASON_CUE_CACHE_MISSING"),
        # (d) only the fallback target is cached (current hash).
        ("fallback_only", "warn", "REASON_CUE_CACHE_FALLBACK_ONLY"),
    ],
)
def test_check_cue_cache_classifies_the_registry(
    monkeypatch, tmp_path: Path, populate, status, reason_name,
):
    monkeypatch.setattr(cues, "CUES", _FIXTURE_CUES)
    cfg = _fresh_cfg(
        monkeypatch, GEMINI_API_KEY="AIzaSyTest", JASPER_SOUNDS_DIR=str(tmp_path),
    )
    manager = cues._cue_manager_for_doctor(cfg)

    if populate == "current":
        for cue in _FIXTURE_CUES:
            Path(manager.expected_path(cue)).write_bytes(b"")
    elif populate == "stale":
        for cue in _FIXTURE_CUES:
            (tmp_path / f"{cue.slug}-deadbeef.wav").write_bytes(b"")
    elif populate == "fallback_only":
        Path(manager.expected_path(_FALLBACK)).write_bytes(b"")
    # "nothing": tmp_path stays empty.

    result = cues.check_cue_cache(cfg)
    assert result.status == status
    assert result.reason == (getattr(cues, reason_name) if reason_name else "")
