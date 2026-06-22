# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the runtime acoustic-condition estimator (classify_condition)."""
from __future__ import annotations

from jasper.wake_conditions import CONDITIONS
from jasper.wake_condition_context import (
    AMBIENT_FLOOR_DBFS,
    MUSIC_FLOOR_DBFS,
    classify_condition,
)


def test_music_when_playback_loud():
    ctx = classify_condition(
        music_dbfs=MUSIC_FLOOR_DBFS + 5, noise_floor_dbfs=-70.0,
    )
    assert ctx.condition == "music"
    assert ctx.music_active is True


def test_music_wins_over_ambient():
    # Loud music AND a high noise floor -> music, not ambient: music is the
    # dominant, most-reliable signal and is checked first.
    ctx = classify_condition(
        music_dbfs=MUSIC_FLOOR_DBFS + 10,
        noise_floor_dbfs=AMBIENT_FLOOR_DBFS + 10,
    )
    assert ctx.condition == "music"


def test_ambient_when_quiet_playback_but_noisy_room():
    ctx = classify_condition(
        music_dbfs=MUSIC_FLOOR_DBFS - 20,
        noise_floor_dbfs=AMBIENT_FLOOR_DBFS + 5,
    )
    assert ctx.condition == "ambient"
    assert ctx.music_active is False


def test_quiet_when_no_music_and_low_noise_floor():
    ctx = classify_condition(
        music_dbfs=None, noise_floor_dbfs=AMBIENT_FLOOR_DBFS - 10,
    )
    assert ctx.condition == "quiet"


def test_missing_signals_degrade_to_quiet():
    # Both unknown -> the safe base condition, never an error.
    ctx = classify_condition(music_dbfs=None, noise_floor_dbfs=None)
    assert ctx.condition == "quiet"
    assert ctx.music_active is False


def test_output_is_always_a_known_condition():
    # Across the signal space, classify only ever emits a taxonomy member,
    # keeping it bound to the wake_conditions SSOT.
    for m in (None, -90.0, -60.0, -10.0):
        for n in (None, -90.0, -50.0, -10.0):
            assert classify_condition(m, n).condition in CONDITIONS


def test_context_carries_the_raw_signals():
    ctx = classify_condition(music_dbfs=-12.0, noise_floor_dbfs=-44.0)
    assert ctx.music_dbfs == -12.0
    assert ctx.noise_floor_dbfs == -44.0
