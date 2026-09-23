# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime acoustic-condition estimator for wake telemetry.

Turns the two cheap runtime signals the daemon already has at a wake fire
into one :data:`jasper.wake_conditions.CONDITIONS` label:

  * **music** — from cheap playback-chain loudness telemetry refreshed by
    the daemon before the wake turn. Music is the dominant false-fire
    driver and the most reliable signal (we know what we play), so it wins
    first.
  * **quiet vs ambient** — from the mic-capture noise floor (a low percentile
    of the pre-fire capture ring's per-frame RMS; see
    ``jasper.voice.wake_detect._ring_noise_floor_dbfs``).

:func:`classify_condition` is intentionally **pure** — both signals are
passed in — so it is unit-testable. The result is recorded as
``wake_events.condition_class``: a telemetry label the wake fire gate never
reads (it compares only the leg detector's own score threshold).

The boundaries below are tunable knobs, not laws. ``MUSIC_FLOOR_DBFS`` is
the one threshold for "is music playing" — also used by
:class:`jasper.voice.content_activity.ContentActivityTracker`.
``AMBIENT_FLOOR_DBFS`` is a **placeholder** on a different signal (the mic
noise floor) — the quiet/ambient split is the soft boundary to tune against
the corpus; it affects an observability label, never a wake decision.
"""
from __future__ import annotations

from dataclasses import dataclass

# Playback-chain loudness (dBFS) above which we call it music. The one
# threshold for "is music playing" — jasper.voice.content_activity also
# compares against this constant.
MUSIC_FLOOR_DBFS: float = -60.0

# Mic-capture noise floor (dBFS) above which a non-music room counts as
# "ambient" rather than "quiet". PLACEHOLDER — tune against the corpus.
AMBIENT_FLOOR_DBFS: float = -50.0


@dataclass(frozen=True)
class ConditionContext:
    """The acoustic situation at a wake fire.

    Recorded as ``wake_events.condition_class``. ``condition`` is always one of
    :data:`jasper.wake_conditions.CONDITIONS`.
    """

    condition: str
    music_active: bool
    music_dbfs: float | None
    noise_floor_dbfs: float | None


def classify_condition(
    music_dbfs: float | None,
    noise_floor_dbfs: float | None,
) -> ConditionContext:
    """Map the two runtime signals to one acoustic condition. Pure.

    Music wins first. Otherwise the mic noise floor splits ambient
    (AC/fridge/TV murmur) from quiet. A missing signal degrades toward the
    *quieter* classification (never raises): unknown music -> not music;
    unknown noise floor -> quiet. So a misread can only make wake less eager,
    never spuriously more.
    """
    music_active = music_dbfs is not None and music_dbfs > MUSIC_FLOOR_DBFS
    if music_active:
        condition = "music"
    elif noise_floor_dbfs is not None and noise_floor_dbfs > AMBIENT_FLOOR_DBFS:
        condition = "ambient"
    else:
        condition = "quiet"
    return ConditionContext(
        condition=condition,
        music_active=music_active,
        music_dbfs=music_dbfs,
        noise_floor_dbfs=noise_floor_dbfs,
    )
