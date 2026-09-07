# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's wake-event telemetry: every `WakeEventStore` write the
daemon makes, and sole ownership of the in-flight event id.

The fire-time row, the funnel-stage updates, the terminal outcome, the
session-VAD shadow columns and the post-fire audio snapshot. Fail-soft
throughout: the wake and session paths are never blocked by telemetry
trouble. The loop hands in what it observed; nothing here reads loop state.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..aec_sweep import (
    AGC1_ENABLED_ENV,
    AGC1_MAX_GAIN_DB_ENV,
    AGC1_TARGET_DBFS_ENV,
    NS_ENABLED_ENV,
    NS_LEVEL_ENV,
)
from ..wake_condition_context import ConditionContext
from ..wake_events import (
    CAPTURE_POST_SEC,
    CAPTURE_PRE_SEC,
    WakeEventStore,
    make_event_id,
)

logger = logging.getLogger("jasper.voice_daemon")


# Per-leg wake_events column mapping. The peak_score column is irregular for
# back-compat with the existing corpus (aec_on/aec_off vs dtln_aec), so the
# columns are listed explicitly rather than derived from the token. A new leg
# adds an entry here plus the matching additive columns in jasper.wake_events.
_LEG_DB: dict[str, dict[str, str]] = {
    "on": {
        "trigger_kind": "fire_aec_on", "peak_score": "peak_score_aec_on",
        "peak_offset": "peak_offset_ms_on", "mic_rms": "mic_rms_dbfs_on",
    },
    "off": {
        "trigger_kind": "fire_aec_off", "peak_score": "peak_score_aec_off",
        "peak_offset": "peak_offset_ms_off", "mic_rms": "mic_rms_dbfs_off",
    },
    "dtln": {
        "trigger_kind": "fire_dtln", "peak_score": "peak_score_dtln_aec",
        "peak_offset": "peak_offset_ms_dtln", "mic_rms": "mic_rms_dbfs_dtln",
    },
    "chip_aec_150": {
        "trigger_kind": "fire_chip_aec_150",
        "peak_score": "peak_score_chip_aec_150",
        "peak_offset": "peak_offset_ms_chip_aec_150",
        "mic_rms": "mic_rms_dbfs_chip_aec_150",
    },
    "chip_aec_210": {
        "trigger_kind": "fire_chip_aec_210",
        "peak_score": "peak_score_chip_aec_210",
        "peak_offset": "peak_offset_ms_chip_aec_210",
        "mic_rms": "mic_rms_dbfs_chip_aec_210",
    },
}


@dataclass(frozen=True)
class LegFireScore:
    """One leg's fire-time observation, as the wake loop saw it.

    ``score_at`` is the loop clock the score was taken on; 0.0 means the
    leg has never scored. ``mic_rms_dbfs`` is the instantaneous RMS of the
    last frame in that leg's capture ring — None when the ring is empty.
    """

    score: float
    score_at: float
    mic_rms_dbfs: float | None


class WakeTelemetry:
    def __init__(
        self,
        *,
        store: WakeEventStore | None,
        wake_model: str,
        voice_provider: str,
    ) -> None:
        self.store = store
        self._wake_model = wake_model
        self._voice_provider = voice_provider
        # The wake event currently in flight, or None when no event is
        # pending. Set by `on_fire`; cleared by `outcome` after the final
        # write. The funnel-stage hooks consult it to know which row to
        # UPDATE.
        self._current_event_id: str | None = None

    @property
    def current_event_id(self) -> str | None:
        """The in-flight event id, for the turn-teardown write that has to
        capture it before `outcome` clears it."""
        return self._current_event_id

    async def on_fire(
        self,
        *,
        leg: str,
        score: float,
        now_loop: float,
        legs: Mapping[str, LegFireScore],
        firing_threshold: float,
        fired_legs: str,
        condition: ConditionContext,
        mic_muted: bool,
    ) -> str | None:
        """Open a wake-event row for the funnel hooks to update as the event
        progresses. One SQLite INSERT in WAL mode; failure is logged but
        never blocks wake response.

        Returns the new event id, or None when the INSERT failed."""
        store = self.store
        if store is None:
            return None
        event_id = make_event_id()
        self._current_event_id = event_id
        trigger_kind = _LEG_DB[leg]["trigger_kind"]
        # Pre-seed every per-leg column to None, derived from _LEG_DB
        # so a new leg's columns are included automatically.
        # begin_event requires peak_score_aec_on/off; configured legs
        # overwrite their own columns below.
        # Any, not object: the values splat into begin_event's per-column
        # float/int/str keyword parameters.
        tel: dict[str, Any] = {
            col: None
            for _db in _LEG_DB.values()
            for col in (_db["peak_score"], _db["peak_offset"], _db["mic_rms"])
        }
        for _name, _fire in legs.items():
            _cols = _LEG_DB[_name]
            tel[_cols["peak_score"]] = (
                score if _name == leg else _fire.score
            )
            # Offset is against the caller's canonical fire-time `now_loop`,
            # NOT a fresh clock read — that would fold in the
            # detector.reset() latency and skew the firing leg's offset.
            # Semantics: 0 = leg's last score == fire frame (the firing
            # leg); negative N = that leg last scored N ms before fire.
            tel[_cols["peak_offset"]] = (
                int((_fire.score_at - now_loop) * 1000)
                if _fire.score_at else None
            )
            tel[_cols["mic_rms"]] = _fire.mic_rms_dbfs
        # Bridge config snapshot — env-var-driven knobs as seen by the
        # bridge at startup, so post-hoc analysis can ask "what NS
        # level was this event captured under?". Read here rather than
        # from the bridge (a separate process): /etc/jasper/jasper.env
        # is the source of truth, and the bridge is restarted after
        # any change to it.
        bridge_config = {
            "ns_enabled": os.environ.get(NS_ENABLED_ENV, "1"),
            "ns_level": os.environ.get(NS_LEVEL_ENV, "low"),
            "agc1_enabled": os.environ.get(AGC1_ENABLED_ENV, "0"),
            "agc1_target_dbfs": os.environ.get(AGC1_TARGET_DBFS_ENV, "9"),
            "agc1_max_gain_db": os.environ.get(AGC1_MAX_GAIN_DB_ENV, "18"),
            "ref_gain_db": os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"),
            "mic_gain_db": os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"),
            "ref_hpf_hz": os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"),
            "chip_hpf_hz": os.environ.get("JASPER_AEC_CHIP_HPF_HZ", "125"),
        }
        try:
            await store.begin_event(
                event_id=event_id,
                trigger_kind=trigger_kind,
                threshold=firing_threshold,
                wake_model=self._wake_model,
                voice_provider=self._voice_provider,
                bridge_config=bridge_config,
                music_active=condition.music_active,
                music_volume_db=condition.music_dbfs,
                condition_class=condition.condition,
                mic_muted=mic_muted,
                fired_legs=fired_legs,
                **tel,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: begin_event failed (will skip telemetry "
                "for this event): %s", e,
            )
            self._current_event_id = None
        return self._current_event_id

    async def finalize_event_audio(
        self,
        event_id: str,
        *,
        snapshot: Callable[[str, int], bytes | None],
    ) -> None:
        """Wait the post-fire collection window, then snapshot each configured
        capture ring — via the loop's `snapshot(leg, n_frames)` — and persist
        WAV files through the store.

        Fire-and-forget: failure logs WARN and does not propagate. Truncation
        on daemon shutdown is acceptable — the row keeps its NULL
        audio_*_path, which queries can filter out."""
        store = self.store
        if store is None:
            return
        try:
            await asyncio.sleep(CAPTURE_POST_SEC)
            # Snapshot count = pre + post window in frames. Rings may hold
            # slightly more than this thanks to the slack in the maxlen
            # sizing.
            from ..audio_io import MicCapture as _MC
            n_frames = int(
                (CAPTURE_PRE_SEC + CAPTURE_POST_SEC)
                * _MC.OUTPUT_RATE / _MC.OUTPUT_FRAME_SAMPLES
            )
            await store.attach_audio(
                event_id=event_id,
                audio_on=snapshot("on", n_frames),
                audio_off=snapshot("off", n_frames),
                audio_dtln=snapshot("dtln", n_frames),
                audio_chip_aec_150=snapshot("chip_aec_150", n_frames),
                audio_chip_aec_210=snapshot("chip_aec_210", n_frames),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: attach_audio failed for %s: %s", event_id, e,
            )

    async def stage(
        self,
        stage: str,
        *,
        tool_name: str | None = None,
    ) -> None:
        """Best-effort funnel-stage update for the in-flight wake event.

        No-op when telemetry is disabled, no event is in flight, or the store
        write fails: the wake and session paths are never blocked by
        telemetry trouble."""
        store = self.store
        event_id = self._current_event_id
        if store is None or event_id is None:
            return
        try:
            await store.update_stage(
                event_id,
                stage,
                tool_name=tool_name,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: update_stage(%s) failed: %s", stage, e,
            )

    async def record_tool_dispatch_stage(self, stage: str, name: str) -> None:
        """Translate the shared dispatch observer into wake-funnel stages.

        ``dispatch_tool`` is the only producer, so this observes Gemini,
        OpenAI, and Grok without provider branches. Manual / research turns
        naturally no-op because they have no in-flight wake event id.
        """
        funnel_stage = {
            "called": "tool_called",
            "completed": "tool_completed",
        }.get(stage)
        if funnel_stage is None:
            raise ValueError(f"unknown tool dispatch stage {stage!r}")
        await self.stage(
            funnel_stage,
            tool_name=name,
        )

    async def outcome(
        self, outcome: str, detail: str | None = None,
    ) -> None:
        """Best-effort terminal-outcome UPDATE for the in-flight wake
        event. Same fail-soft pattern as `stage`. Clears
        `current_event_id` after the write so subsequent funnel hooks
        for the next wake start clean."""
        store = self.store
        event_id = self._current_event_id
        if store is None or event_id is None:
            # Still clear the id (if it exists) so the next wake
            # starts from a clean state.
            self._current_event_id = None
            return
        # Clear early so subsequent stray funnel-hook calls don't keep
        # writing against a finalised row.
        self._current_event_id = None
        try:
            await store.set_outcome(event_id, outcome, detail)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: set_outcome(%s) failed for %s: %s",
                outcome, event_id, e,
            )

    async def record_session_vad(
        self,
        event_id: str,
        *,
        max_silero_aec: float | None,
        max_silero_raw: float | None,
        silero_aec_armed_at_ms: int | None,
        silero_raw_armed_at_ms: int | None,
        endpointer: str,
        music_playing_at_turn: bool,
        music_db_at_turn: float | None,
    ) -> None:
        """Shadow telemetry: what each stream's Silero saw, so the weekly
        review can cross-tab scores.

        Takes the event id explicitly — the caller captures it before the
        terminal outcome clears it."""
        store = self.store
        if store is None:
            return
        try:
            await store.update_session_vad(
                event_id,
                max_silero_aec=max_silero_aec,
                max_silero_raw=max_silero_raw,
                silero_aec_armed_at_ms=silero_aec_armed_at_ms,
                silero_raw_armed_at_ms=silero_raw_armed_at_ms,
                endpointer=endpointer,
                music_playing_at_turn=music_playing_at_turn,
                music_db_at_turn=music_db_at_turn,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("wake_events: session VAD telemetry failed: %s", e)
