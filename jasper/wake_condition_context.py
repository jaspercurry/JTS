"""Runtime acoustic-condition estimator for wake fusion (Phase 1).

Turns the two cheap runtime signals the daemon already has at a wake fire
into one :data:`jasper.wake_conditions.CONDITIONS` label:

  * **music** — from the playback-chain loudness (the ``TtsVolumeTracker``
    anchor the daemon reads on the wake hot path). Music is the dominant
    false-fire driver and the most reliable signal (we know what we play),
    so it wins first.
  * **quiet vs ambient** — from the mic-capture noise floor (a low percentile
    of the pre-fire capture ring's per-frame RMS; see
    ``jasper.voice_daemon._ring_noise_floor_dbfs``).

:func:`classify_condition` is intentionally **pure** — both signals are
passed in — so it is unit-testable and so the Phase-1.2 fuser can call it
with the same inputs to pick per-condition thresholds. Phase 1.1 records its
result as ``wake_events.condition_class``.

The boundaries below are tunable knobs, not laws. ``MUSIC_FLOOR_DBFS``
matches the daemon's existing ``music_active_proxy`` (anchor > -60 dBFS).
``AMBIENT_FLOOR_DBFS`` is a **placeholder** on a different signal (the mic
noise floor) — the quiet/ambient split is the soft boundary to tune against
the corpus; until then it only affects an observability label, never a wake
decision. When the 1.2 fuser consumes these, promote them to config knobs.
"""
from __future__ import annotations

from dataclasses import dataclass

# Playback-chain loudness (dBFS) above which we call it music. Mirrors the
# daemon's long-standing music_active_proxy threshold.
MUSIC_FLOOR_DBFS: float = -60.0

# Mic-capture noise floor (dBFS) above which a non-music room counts as
# "ambient" rather than "quiet". PLACEHOLDER — tune against the corpus.
AMBIENT_FLOOR_DBFS: float = -50.0


@dataclass(frozen=True)
class ConditionContext:
    """The acoustic situation at a wake fire.

    Recorded as ``wake_events.condition_class`` (Phase 1.1) and consumed by
    the per-condition fuser (Phase 1.2). ``condition`` is always one of
    :data:`jasper.wake_conditions.CONDITIONS`.
    """

    condition: str
    music_active: bool
    music_dbfs: float | None
    noise_floor_dbfs: float | None


def classify_condition(
    music_dbfs: float | None,
    noise_floor_dbfs: float | None,
    *,
    music_floor_dbfs: float = MUSIC_FLOOR_DBFS,
    ambient_floor_dbfs: float = AMBIENT_FLOOR_DBFS,
) -> ConditionContext:
    """Map the two runtime signals to one acoustic condition. Pure.

    Music wins first. Otherwise the mic noise floor splits ambient
    (AC/fridge/TV murmur) from quiet. A missing signal degrades toward the
    *quieter* classification (never raises): unknown music -> not music;
    unknown noise floor -> quiet. So a misread can only make wake less eager,
    never spuriously more.
    """
    music_active = music_dbfs is not None and music_dbfs > music_floor_dbfs
    if music_active:
        condition = "music"
    elif noise_floor_dbfs is not None and noise_floor_dbfs > ambient_floor_dbfs:
        condition = "ambient"
    else:
        condition = "quiet"
    return ConditionContext(
        condition=condition,
        music_active=music_active,
        music_dbfs=music_dbfs,
        noise_floor_dbfs=noise_floor_dbfs,
    )
