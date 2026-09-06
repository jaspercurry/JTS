# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `_ring_noise_floor_dbfs`, `WakeLoop._read_music_dbfs`,
and `_maybe_refresh_condition`."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from jasper.voice_daemon import WakeLoop


# ---------------------------------------------------------------------------
# _ring_noise_floor_dbfs — fire-time ambient floor for the condition estimator
# ---------------------------------------------------------------------------


def test_ring_noise_floor_empty_or_none_is_none():
    from collections import deque
    from jasper.voice_daemon import _ring_noise_floor_dbfs
    assert _ring_noise_floor_dbfs(None) is None
    assert _ring_noise_floor_dbfs(deque()) is None


def test_ring_noise_floor_tracks_quiet_background_not_utterance():
    """The low percentile reflects the quiet majority (room floor), not the
    few loud frames (the wake utterance) — so it estimates ambient, not the
    speech that just fired."""
    from collections import deque
    from jasper.voice_daemon import _ring_noise_floor_dbfs
    quiet = np.full(1280, 30, dtype=np.int16)     # near-silent background
    loud = np.full(1280, 8000, dtype=np.int16)    # the "utterance" frames
    ring = deque([quiet] * 16 + [loud] * 4)        # utterance is the minority
    floor = _ring_noise_floor_dbfs(ring)
    assert floor is not None
    assert floor < -40.0  # 25th pct sits in the quiet group, far below loud


# --- Phase 1.3a: live-condition refresh (WakeLoop._read_music_dbfs +
# _maybe_refresh_condition) ---

def _wakeloop_for_condition(music_dbfs=-30.0):
    """A bare WakeLoop with only the attributes the condition-refresh path
    touches. music_dbfs=-30 reads as music (> -60 dBFS); the empty capture
    ring makes the noise floor None."""
    from collections import deque

    wl = WakeLoop.for_tests()
    wl._condition_refreshed_at = 0.0
    wl._current_condition = "quiet"
    wl._capture_ring_on = deque(maxlen=8)
    wl._content_activity = MagicMock()
    wl._content_activity.music_dbfs = music_dbfs
    return wl


def test_read_music_dbfs_reads_content_activity():
    assert _wakeloop_for_condition(music_dbfs=-30.0)._read_music_dbfs() == -30.0


def test_read_music_dbfs_none_when_unavailable():
    wl = _wakeloop_for_condition()
    wl._content_activity.music_dbfs = None
    assert wl._read_music_dbfs() is None


def test_maybe_refresh_condition_recomputes_when_elapsed():
    wl = _wakeloop_for_condition(music_dbfs=-30.0)  # > -60 dBFS -> music
    wl._maybe_refresh_condition(now_loop=5.0)
    assert wl._current_condition == "music"
    assert wl._condition_refreshed_at == 5.0


def test_maybe_refresh_condition_skips_within_window():
    wl = _wakeloop_for_condition(music_dbfs=-30.0)
    wl._condition_refreshed_at = 4.5
    wl._current_condition = "quiet"
    wl._maybe_refresh_condition(now_loop=5.0)  # 0.5 s < CONDITION_REFRESH_SEC
    assert wl._current_condition == "quiet"  # unchanged
    assert wl._condition_refreshed_at == 4.5  # unchanged


def test_maybe_refresh_condition_fail_soft_on_classify_error(monkeypatch):
    # The wake path must never break because ancillary condition estimation
    # raised. On error: keep the last good condition, advance the timer (so a
    # persistent failure retries at ~1 Hz, not every frame), do not propagate.
    wl = _wakeloop_for_condition(music_dbfs=-30.0)
    wl._current_condition = "ambient"  # last good

    def _boom(*_a, **_k):
        raise RuntimeError("classify blew up")

    monkeypatch.setattr("jasper.voice_daemon.classify_condition", _boom)
    wl._maybe_refresh_condition(now_loop=5.0)  # must not raise
    assert wl._current_condition == "ambient"  # stale condition kept
    assert wl._condition_refreshed_at == 5.0   # timer advanced -> ~1 Hz retry

