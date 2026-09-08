# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor cue domain: the baked cache and the
delivery record the running daemon publishes."""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from jasper.cli.doctor import cues
from jasper.cli.doctor._evidence import StatusRead, evidence
from jasper.cues.factory import build_cue_tts_backend
from jasper.cues.manager import AudioCueManager
from jasper.cues.registry import CueDef
from jasper.platform.control_client import ControlError

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


# --- check_cue_delivery ---


def _delivery_state(*, failed: int, outcome: str, reason: str) -> dict:
    """A /state cue block, counts taken from a real manager's snapshot so a
    new outcome in the closed set can't silently drift out of this fixture."""
    state = AudioCueManager("/nonexistent", "jts.local", "Aoede").snapshot()
    state["counts"].update({"delivered": 3, "failed": failed})
    state["last"] = {
        "outcome": outcome, "reason": reason, "slug": "wake_ack",
        "age_seconds": 5.0,
    }
    return state


_UNREACHABLE = StatusRead(None, ControlError("connection refused"))


@pytest.mark.parametrize(
    "read, streambox, status, reason",
    [
        # (1) jasper-control could not be read at all.
        (_UNREACHABLE, False, "skipped", cues.REASON_CUE_DELIVERY_UNAVAILABLE),
        # (2) the daemon answers but carries no cue manager: every failure
        # cue is silent, which is the streambox's normal shape and nobody
        # else's.
        (
            StatusRead({"cues": None}), False,
            "warn", cues.REASON_CUE_DELIVERY_NO_MANAGER,
        ),
        (
            StatusRead({"cues": None}), True,
            "skipped", cues.REASON_CUE_DELIVERY_NO_MANAGER,
        ),
        # (3) shape drift — never `ok`, because nothing was observed.
        (
            StatusRead({"cues": {"counts": [], "last": None}}), False,
            "skipped", cues.REASON_CUE_DELIVERY_UNAVAILABLE,
        ),
        (
            StatusRead({"cues": {"counts": {"failed": True}, "last": None}}), False,
            "skipped", cues.REASON_CUE_DELIVERY_UNAVAILABLE,
        ),
        # (4) the LAST attempt failed.
        (
            StatusRead({"cues": _delivery_state(
                failed=2, outcome="failed", reason="no_cache",
            )}), False,
            "warn", cues.REASON_CUE_DELIVERY_FAILED,
        ),
        # (5) delivering now. A historical failure count is carried in the
        # detail rather than alarming forever — the counters are monotonic.
        (
            StatusRead({"cues": _delivery_state(
                failed=2, outcome="delivered", reason="ok",
            )}), False,
            "ok", "",
        ),
        (
            StatusRead({"cues": _delivery_state(
                failed=0, outcome="delivered", reason="ok",
            )}), False,
            "ok", "",
        ),
    ],
    ids=[
        "control_unreachable", "no_manager", "no_manager_on_streambox",
        "counts_not_a_dict", "failed_is_a_bool", "last_attempt_failed",
        "recovered_after_failures", "never_failed",
    ],
)
def test_check_cue_delivery_classifies_the_snapshot(
    read, streambox, status, reason,
):
    evidence.seed("control_state", read)
    evidence.seed("install_profile_is_streambox", streambox)

    result = cues.check_cue_delivery()

    assert result.status == status
    assert result.reason == reason
