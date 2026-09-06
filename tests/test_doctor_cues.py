# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor cue-cache domain."""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from jasper.cli.doctor import cues
from jasper.cues.factory import build_cue_tts_backend
from jasper.cues.manager import AudioCueManager
from jasper.cues.registry import CueDef

from .doctor_test_support import _fresh_cfg


def _manager_for(cfg) -> AudioCueManager:
    """Same construction as ``check_cue_cache`` itself, for laying out
    fixture files at the paths the check will actually look for."""
    backend, voice = build_cue_tts_backend(cfg)
    hostname = urllib.parse.urlparse(cfg.management_url).hostname or "this speaker"
    return AudioCueManager(cfg.sounds_dir, hostname, voice, backend)


# A primary cue plus its fallback target — enough to exercise every branch
# of a one-hop fallback chain without depending on the real registry's shape.
_FALLBACK = CueDef(slug="fallback_cue", template="fallback", description="d")
_PRIMARY = CueDef(
    slug="primary_cue", template="primary", description="d", fallback="fallback_cue",
)
_TWO_HOP = (_PRIMARY, _FALLBACK)

# A -> B -> C fallback chain, to pin that a multi-hop cascade that eventually
# resolves counts as fallback_only all the way up, never missing partway.
_C = CueDef(slug="c_cue", template="c", description="d")
_B = CueDef(slug="b_cue", template="b", description="d", fallback="c_cue")
_A = CueDef(slug="a_cue", template="a", description="d", fallback="b_cue")
_THREE_HOP = (_A, _B, _C)


@pytest.mark.parametrize(
    "registry, populate, status, reason_name",
    [
        # (a) both cues cached under their current hash.
        (_TWO_HOP, "current", "ok", None),
        # (b) both cues cached, but under a stale hash.
        (_TWO_HOP, "stale", "warn", "REASON_CUE_CACHE_STALE"),
        # (c) nothing cached anywhere, and the fallback has nothing either.
        (_TWO_HOP, "nothing", "fail", "REASON_CUE_CACHE_MISSING"),
        # (d) only the fallback target is cached (current hash).
        (_TWO_HOP, "fallback_only", "warn", "REASON_CUE_CACHE_FALLBACK_ONLY"),
        # (e) only the last link of a 3-hop chain is cached: the first cue
        # still plays (A -> B -> C cascades), so it must warn, never fail.
        (_THREE_HOP, "only_last", "warn", "REASON_CUE_CACHE_FALLBACK_ONLY"),
    ],
)
def test_check_cue_cache_classifies_the_registry(
    monkeypatch, tmp_path: Path, registry, populate, status, reason_name,
):
    monkeypatch.setattr(cues, "CUES", registry)
    cfg = _fresh_cfg(
        monkeypatch, GEMINI_API_KEY="AIzaSyTest", JASPER_SOUNDS_DIR=str(tmp_path),
    )
    manager = _manager_for(cfg)

    if populate == "current":
        for cue in registry:
            Path(manager.expected_path(cue)).write_bytes(b"")
    elif populate == "stale":
        for cue in registry:
            (tmp_path / f"{cue.slug}-deadbeef.wav").write_bytes(b"")
    elif populate == "fallback_only":
        Path(manager.expected_path(_FALLBACK)).write_bytes(b"")
    elif populate == "only_last":
        Path(manager.expected_path(_C)).write_bytes(b"")
    # "nothing": tmp_path stays empty.

    result = cues.check_cue_cache(cfg)
    assert result.status == status
    assert result.reason == (getattr(cues, reason_name) if reason_name else "")


def test_check_cue_cache_lists_sounds_dir_once_per_run(monkeypatch, tmp_path: Path):
    """One os.listdir call classifies every slug missing from is_cached,
    not one per slug: a large registry must not turn into N directory reads."""
    monkeypatch.setattr(cues, "CUES", _THREE_HOP)
    cfg = _fresh_cfg(
        monkeypatch, GEMINI_API_KEY="AIzaSyTest", JASPER_SOUNDS_DIR=str(tmp_path),
    )
    # Nothing cached anywhere: every slug in the chain misses is_cached and
    # falls through to the any-cached-anywhere lookup this test counts.
    calls = []
    real_listdir = cues.os.listdir
    monkeypatch.setattr(
        cues.os, "listdir",
        lambda path: (calls.append(path), real_listdir(path))[1],
    )

    cues.check_cue_cache(cfg)

    assert calls == [str(tmp_path)]
