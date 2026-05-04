from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class SpeechVAD:
    """Wrapper around openWakeWord's Silero VAD for in-session barge-in
    detection.

    Silero VAD is trained to distinguish human conversational speech from
    music, environmental noise, and synthesised audio. We use it during a
    voice session while the model is producing TTS: bleed-through of the
    model's own speech (and any ducked music) reaches the mic, but Silero
    can tell that bleed apart from a real user trying to interrupt. So
    the daemon can gate mic-to-Gemini on speech_prob >= threshold —
    server doesn't get confused by bleed, but real barge-in still works.

    Stateful (LSTM internally). Call reset() between sessions so state
    from one session doesn't bleed into the next.
    """

    def __init__(self) -> None:
        # Lazy import — keeps daemon startup fast and avoids loading
        # openwakeword's resources at module-import time in tests.
        from openwakeword import VAD
        self._vad = VAD()

    def predict(self, frame_int16: np.ndarray) -> float:
        """Predict speech probability for a 16 kHz int16 mono frame.

        Accepts any frame size; openWakeWord's VAD chunks internally
        based on its frame_size param. Returns 0-1: max sub-chunk
        score, which answers "did ANY part of this frame look like
        speech?" — the right question for gating mic forwarding.
        """
        result = self._vad.predict(frame_int16)
        if isinstance(result, (list, np.ndarray)):
            arr = np.asarray(result).flatten()
            return float(arr.max()) if arr.size else 0.0
        return float(result)

    def reset(self) -> None:
        """Reset Silero's LSTM state between sessions."""
        if hasattr(self._vad, "reset_states"):
            self._vad.reset_states()
