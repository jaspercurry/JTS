from __future__ import annotations

import logging

import numpy as np
from openwakeword.model import Model

logger = logging.getLogger(__name__)


class WakeWordDetector:
    """Stateful wake-word scorer over 16 kHz int16 frames.

    Frame size is openWakeWord-flexible but should be a multiple of 80 ms
    (1280 samples at 16 kHz). MicCapture produces exactly that.
    """

    def __init__(self, model_name: str, threshold: float = 0.5) -> None:
        # model_name can be a stock name like "hey_jarvis" (resolved by
        # openWakeWord's bundled models) or a path to a custom .onnx file.
        # inference_framework="onnx" is required: openwakeword 0.6.0 defaults
        # to "tflite", but tflite-runtime has no Python 3.13 wheel (see
        # deploy/install.sh comment) and isn't installed here. We use the
        # bundled .onnx model files exclusively.
        self._model = Model(
            wakeword_models=[model_name],
            inference_framework="onnx",
        )
        self._threshold = threshold
        self._key = self._resolve_score_key(model_name)

    @staticmethod
    def _resolve_score_key(model_name: str) -> str:
        # openWakeWord keys predictions by the bare model basename.
        if "/" in model_name or model_name.endswith((".onnx", ".tflite")):
            base = model_name.rsplit("/", 1)[-1]
            return base.rsplit(".", 1)[0]
        return model_name

    def feed(self, frame: np.ndarray) -> bool:
        scores = self._model.predict(frame)
        score = float(scores.get(self._key, 0.0))
        return score >= self._threshold

    def reset(self) -> None:
        """Reset internal model state after a wake fires.

        openWakeWord's prediction smoothing keeps recent-activation
        state across calls — once the model has scored a wake-word
        spike, its baseline stays elevated for several seconds, so
        anything speech-shaped (music vocals, TTS-tail bleed) can
        more easily push past the threshold and false-fire on the
        next pass through WAKE state. Calling this between a wake
        firing and the next listening window clears that bias.

        Implementation note: openWakeWord exposes
        `model.reset()` which clears per-model prediction-buffer
        history. The deque-style internal buffers and any model-
        level smoothing both get zeroed.
        """
        try:
            self._model.reset()
        except Exception as e:  # noqa: BLE001
            # Older openwakeword versions might not expose reset();
            # don't crash if it's not there — the symptom (post-wake
            # false-fires) is annoying but not catastrophic.
            logger.debug("wake detector reset() not available: %s", e)
