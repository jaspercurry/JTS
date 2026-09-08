# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np

from .openwakeword_guard import ensure_openwakeword_import_safe

class WakeWordDetector:
    """Stateful wake-word scorer over 16 kHz int16 frames.

    Frame size is openWakeWord-flexible but should be a multiple of 80 ms
    (1280 samples at 16 kHz). MicCapture produces exactly that.
    """

    def __init__(self, model_name: str, threshold: float = 0.5) -> None:
        ensure_openwakeword_import_safe()
        from openwakeword.model import Model  # lazy: optional Pi dependency

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
        # openWakeWord 0.6.0 reset reruns mel/embedding inference on 4 s of
        # random PCM. Save pristine startup buffers to keep that work out of reset.
        prep = self._model.preprocessor
        self._initial_melspectrogram = prep.melspectrogram_buffer.copy()
        self._initial_features = prep.feature_buffer.copy()

    @property
    def threshold(self) -> float:
        return self._threshold

    @staticmethod
    def _resolve_score_key(model_name: str) -> str:
        # openWakeWord keys predictions by the bare model basename.
        if "/" in model_name or model_name.endswith((".onnx", ".tflite")):
            base = model_name.rsplit("/", 1)[-1]
            return base.rsplit(".", 1)[0]
        return model_name

    def score_frame(self, frame: np.ndarray) -> float:
        """Return the raw wake score (0.0–1.0); callers apply the threshold."""
        scores = self._model.predict(frame)
        return float(scores.get(self._key, 0.0))

    def reset(self) -> None:
        """Discard audio and prediction history without running inference."""
        prep = self._model.preprocessor
        prep.raw_data_buffer.clear()
        prep.melspectrogram_buffer = self._initial_melspectrogram.copy()
        prep.accumulated_samples = 0
        prep.raw_data_remainder = np.empty(0)
        prep.feature_buffer = self._initial_features.copy()
        self._model.prediction_buffer.clear()
