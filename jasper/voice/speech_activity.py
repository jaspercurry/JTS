# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Local speech runs and utterance timing on the capture clock."""
from dataclasses import dataclass


END_OF_UTTERANCE_SILENCE_SEC = 0.8
# Music vocals measured 0.13; quiet user speech reached 0.19.
END_OF_UTTERANCE_SPEECH_THRESHOLD = 0.15
# Short commands last about 250 ms; wake-word tails still need the peak test.
SUSTAINED_SPEECH_TO_ARM_SEC = 0.20
# See scripts/probe-wake-gate.py: wake-tail peaks reached 0.52.
SPEECH_RUN_PEAK_MIN = 0.60


@dataclass
class SpeechActivity:
    run_started_at: float = 0.0
    peak: float = 0.0
    signalled: bool = False
    silence_started_at: float = 0.0
    started_at: float = 0.0
    last_at: float = 0.0

    def reset_run(self) -> None:
        self.run_started_at = self.peak = 0.0
        self.signalled = False

    def update(
        self, score: float, threshold: float, now: float, *, peak_min: float = SPEECH_RUN_PEAK_MIN,
    ) -> bool:
        if score < threshold:
            self.reset_run()
            return False
        if not self.run_started_at:
            self.run_started_at = now
        self.peak = max(self.peak, score)
        return now - self.run_started_at >= SUSTAINED_SPEECH_TO_ARM_SEC and self.peak >= peak_min

    def confirm(self, now: float) -> bool:
        new_utterance = not self.started_at or now - self.last_at >= END_OF_UTTERANCE_SILENCE_SEC
        if new_utterance:
            self.started_at = self.run_started_at or now
        self.last_at = now
        return new_utterance

    def confirming(self, now: float, poll_seconds: float) -> bool:
        # Allow one watchdog tick for the 80 ms capture cadence to confirm
        # an onset at the deadline. Unconfirmed noise cannot hold indefinitely.
        return (
            self.run_started_at > self.last_at
            and now < self.run_started_at + SUSTAINED_SPEECH_TO_ARM_SEC + poll_seconds
        )
