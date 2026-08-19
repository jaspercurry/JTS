# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .openwakeword_guard import ensure_openwakeword_import_safe
from .wake_models import required_openwakeword_assets

logger = logging.getLogger(__name__)


class SpeechVADSetupError(RuntimeError):
    """Silero VAD cannot start because its openWakeWord assets are not ready."""


def _silero_asset_filename() -> str:
    for asset in required_openwakeword_assets():
        if asset.key == "silero_vad":
            return asset.filename
    return "silero_vad.onnx"


def _openwakeword_models_dir(openwakeword_module: object) -> Path | None:
    origin = getattr(openwakeword_module, "__file__", None)
    if not origin:
        return None
    return Path(origin).resolve().parent / "resources" / "models"


def _setup_error_message(
    filename: str,
    asset_path: Path | None,
    *,
    reason: BaseException | None = None,
) -> str:
    location = str(asset_path) if asset_path is not None else filename
    msg = (
        "openWakeWord Silero VAD could not initialize; expected asset: "
        f"{location}. This package asset is staged by deploy/install.sh; re-run "
        "`bash scripts/deploy-to-pi.sh` from the laptop and check "
        "`jasper-doctor`."
    )
    if reason is not None:
        msg += f" Original error: {reason}"
    return msg


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
        filename = _silero_asset_filename()
        # Must precede the openwakeword import; see jasper/openwakeword_guard.py.
        # This module is imported by jasper-voice *after* jasper.wake, but it is
        # also constructible on its own (scripts/probe-wake-gate.py) — the guard
        # belongs here, not in whatever happened to be imported first.
        ensure_openwakeword_import_safe()
        try:
            import openwakeword
            from openwakeword import VAD
        except Exception as e:  # noqa: BLE001
            raise SpeechVADSetupError(
                _setup_error_message(filename, None, reason=e),
            ) from e

        models_dir = _openwakeword_models_dir(openwakeword)
        asset_path = models_dir / filename if models_dir is not None else None
        if asset_path is not None:
            try:
                ready = asset_path.is_file() and asset_path.stat().st_size > 0
            except OSError:
                ready = False
            if not ready:
                raise SpeechVADSetupError(
                    _setup_error_message(filename, asset_path),
                )

        try:
            self._vad = VAD()
        except Exception as e:  # noqa: BLE001
            raise SpeechVADSetupError(
                _setup_error_message(filename, asset_path, reason=e),
            ) from e

    def predict(self, frame_int16: np.ndarray) -> float:
        """Predict speech probability for a 16 kHz int16 mono frame.

        Delegates to openWakeWord's VAD.predict(), which internally splits
        the input into frame_size-sample sub-chunks (default 480, i.e.
        30 ms @ 16 kHz) and returns np.mean(sub_chunk_scores) as a plain
        numpy scalar — not a list or ndarray. JTS calls this with
        1280-sample (80 ms) frames (OUTPUT_FRAME_SAMPLES in
        jasper/audio_io.py) and never passes frame_size, so
        range(0, 1280, 480) yields three sub-chunks of 480, 480, and a
        ragged 320 samples — shorter than the 480-sample frame_size that
        openWakeWord's own docstring says the input length must be an
        integer multiple of. The score this method returns is therefore
        the MEAN over those three unevenly-sized sub-chunks, not a peak:
        a loud transient confined to the trailing 320-sample chunk is
        averaged in at only 1/3 weight rather than dominating the score,
        so it is under-weighted relative to a true "did any part of this
        frame look like speech?" max.
        """
        result = self._vad.predict(frame_int16)
        # openwakeword==0.6.0 (the hard-pinned version — see
        # deploy/lib/install/python-runtime.sh) always returns a scalar
        # here, so this branch is unreachable today. It stays as a
        # version-compat shim: float() on a multi-element ndarray raises,
        # so if a future openwakeword release ever changed VAD.predict()
        # to return a list/array of per-chunk scores, deleting this branch
        # would turn that upstream shape change into a daemon crash
        # instead of a degraded (max-of-chunks) score.
        if isinstance(result, (list, np.ndarray)):
            arr = np.asarray(result).flatten()
            return float(arr.max()) if arr.size else 0.0
        return float(result)

    def reset(self) -> None:
        """Reset Silero's LSTM state between sessions."""
        if hasattr(self._vad, "reset_states"):
            self._vad.reset_states()
