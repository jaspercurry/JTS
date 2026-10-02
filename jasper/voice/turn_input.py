# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-turn input transitions and speech evidence on the capture clock."""
from dataclasses import dataclass, field
from typing import Literal

from .conversation import NO_SPEECH_ABORT_SEC
from .push_to_talk import HARD_RECORDING_CAP_SEC
from .speech_activity import (
    END_OF_UTTERANCE_SILENCE_SEC,
    END_OF_UTTERANCE_SPEECH_THRESHOLD,
    SPEECH_RUN_PEAK_MIN,
    SpeechActivity,
)


@dataclass
class TurnInputState:
    """Close events must reach the provider immediately, before turn cleanup;
    otherwise the provider can swallow the next utterance as unfinished input.
    """

    started_at: float = 0.0
    manual: bool = False
    speech_seen: bool = False
    ended: bool = False
    manual_frames: int = 0
    max_silero_aec: float = 0.0
    max_silero_raw: float = 0.0
    silero_aec_armed_at_ms: int | None = None
    silero_raw_armed_at_ms: int | None = None
    speech: SpeechActivity = field(default_factory=SpeechActivity)

    def reset_gap(self) -> None:
        self.speech.reset_run()
        self.speech.silence_started_at = 0.0

    def close(self) -> None:
        self.ended = True
        self.speech.reset_run()

    def manual_frame(self, now: float, *, continuous: bool) -> None:
        self.manual_frames += 1
        if continuous:
            self.speech.confirm(now)
            self.speech_seen = True

    def endpointed_frame(
        self, score: float, now: float, *, frame_seconds: float,
    ) -> Literal["no_speech", "cap", "speech_detected", "speech_end", "end-of-utterance"] | None:
        self.max_silero_aec = max(self.max_silero_aec, score)
        elapsed = now - self.started_at
        if not self.speech_seen and elapsed >= NO_SPEECH_ABORT_SEC:
            return "no_speech"
        if elapsed >= HARD_RECORDING_CAP_SEC:
            return "cap"
        armed = self.speech.update(score, END_OF_UTTERANCE_SPEECH_THRESHOLD, now)
        if score >= END_OF_UTTERANCE_SPEECH_THRESHOLD:
            self.speech.silence_started_at = 0.0
            if armed and not self.speech_seen:
                self.speech_seen = True
                self.silero_aec_armed_at_ms = int(elapsed * 1000)
                return "speech_detected"
        elif self.speech_seen:
            if self.speech.silence_started_at == 0.0:
                # The first quiet frame already spans one capture interval.
                self.speech.silence_started_at = now - frame_seconds
                return "speech_end"
            if now - self.speech.silence_started_at >= END_OF_UTTERANCE_SILENCE_SEC:
                return "end-of-utterance"
        return None

    def continuous_frame(
        self, score: float, threshold: float, now: float,
    ) -> Literal["utterance", "pause"] | None:
        self.max_silero_aec = max(self.max_silero_aec, score)
        if self.speech.update(score, threshold, now):
            new_utterance = self.speech.confirm(now)
            self.speech_seen = True
            self.ended = False
            if new_utterance:
                return "utterance"
        elif (score < threshold and self.speech_seen and not self.ended
              and now - self.speech.last_at >= END_OF_UTTERANCE_SILENCE_SEC):
            return "pause"
        return None

    def playback_frame(self, score: float, threshold: float, now: float) -> bool:
        if not self.speech.update(score, threshold, now, peak_min=0.0) or self.speech.signalled:
            return False
        self.speech.signalled = True
        return True

    def shadow_frame(self, score: float, now: float) -> int | None:
        self.max_silero_raw = max(self.max_silero_raw, score)
        if self.silero_raw_armed_at_ms is None and score >= SPEECH_RUN_PEAK_MIN:
            self.silero_raw_armed_at_ms = int((now - self.started_at) * 1000)
            return self.silero_raw_armed_at_ms
        return None
