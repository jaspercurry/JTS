# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from jasper import vad


class _FakeOpenWakeWordVAD:
    def predict(self, frame):
        return 0.0


def _install_fake_openwakeword(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    filenames: set[str],
    vad_cls=_FakeOpenWakeWordVAD,
) -> None:
    pkg = tmp_path / "openwakeword"
    models = pkg / "resources" / "models"
    models.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for filename in filenames:
        (models / filename).write_bytes(b"model")

    module = types.ModuleType("openwakeword")
    module.__file__ = str(pkg / "__init__.py")
    module.VAD = vad_cls
    monkeypatch.setitem(sys.modules, "openwakeword", module)


def test_speech_vad_fails_clearly_when_silero_asset_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_openwakeword(monkeypatch, tmp_path, filenames=set())

    with pytest.raises(vad.SpeechVADSetupError) as exc:
        vad.SpeechVAD()

    msg = str(exc.value)
    assert "silero_vad.onnx" in msg
    assert "deploy/install.sh" in msg
    assert "scripts/deploy-to-pi.sh" in msg
    assert "jasper-doctor" in msg


def test_speech_vad_wraps_openwakeword_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openwakeword", None)

    with pytest.raises(vad.SpeechVADSetupError) as exc:
        vad.SpeechVAD()

    msg = str(exc.value)
    assert "silero_vad.onnx" in msg
    assert "deploy/install.sh" in msg
    assert "jasper-doctor" in msg
    assert "Original error:" in msg


def test_speech_vad_wraps_openwakeword_init_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class BrokenVAD:
        def __init__(self) -> None:
            raise RuntimeError("onnxruntime could not load model")

    _install_fake_openwakeword(
        monkeypatch,
        tmp_path,
        filenames={"silero_vad.onnx"},
        vad_cls=BrokenVAD,
    )

    with pytest.raises(vad.SpeechVADSetupError) as exc:
        vad.SpeechVAD()

    msg = str(exc.value)
    assert "silero_vad.onnx" in msg
    assert "onnxruntime could not load model" in msg
    assert "jasper-doctor" in msg


def test_speech_vad_constructs_when_silero_asset_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_openwakeword(
        monkeypatch,
        tmp_path,
        filenames={"silero_vad.onnx"},
    )

    instance = vad.SpeechVAD()

    assert isinstance(instance._vad, _FakeOpenWakeWordVAD)


def _build_speech_vad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    predict_return: object,
) -> vad.SpeechVAD:
    """Construct a SpeechVAD whose underlying VAD.predict() returns a
    fixed, caller-supplied value on every call — for pinning predict()'s
    scalar-passthrough vs list/ndarray-max handling of whatever
    openwakeword's VAD.predict() hands back.
    """

    class _StubVAD:
        def predict(self, frame):
            return predict_return

    _install_fake_openwakeword(
        monkeypatch,
        tmp_path,
        filenames={"silero_vad.onnx"},
        vad_cls=_StubVAD,
    )
    return vad.SpeechVAD()


def test_predict_passes_through_scalar_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """openwakeword==0.6.0's VAD.predict() returns np.mean(...) — a plain
    numpy scalar, not a list/ndarray. This is the live production path:
    predict() must return that value as-is (as a float), not run it
    through the max() branch.
    """
    instance = _build_speech_vad(
        monkeypatch, tmp_path, predict_return=np.float32(0.42)
    )

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == pytest.approx(0.42, abs=1e-6)


def test_predict_scalar_passthrough_covers_python_float_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instance = _build_speech_vad(monkeypatch, tmp_path, predict_return=0.73)

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == pytest.approx(0.73, abs=1e-6)


def test_predict_list_result_takes_max_shim_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unreachable against the hard-pinned openwakeword==0.6.0 (which
    always returns a scalar — see the scalar-passthrough tests above),
    but pinned anyway: it is the version-compat shim documented in
    jasper/vad.py, and this asserts it actually takes the max rather
    than e.g. the mean or the first element if a future openwakeword
    release ever returns per-chunk scores as a list.
    """
    instance = _build_speech_vad(
        monkeypatch, tmp_path, predict_return=[0.10, 0.90, 0.30]
    )

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == pytest.approx(0.90, abs=1e-6)


def test_predict_ndarray_result_takes_max_shim_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instance = _build_speech_vad(
        monkeypatch,
        tmp_path,
        predict_return=np.array([0.05, 0.20, 0.81], dtype=np.float32),
    )

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == pytest.approx(0.81, abs=1e-6)


def test_predict_multidimensional_ndarray_flattens_before_max(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A (1, N) shaped array — as e.g. a batched model output might
    return — must still be reduced to a single float, not raise or
    silently pick the wrong axis.
    """
    instance = _build_speech_vad(
        monkeypatch,
        tmp_path,
        predict_return=np.array([[0.10, 0.65, 0.20]], dtype=np.float32),
    )

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == pytest.approx(0.65, abs=1e-6)


def test_predict_empty_ndarray_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty result array has no max; predict() must return 0.0
    rather than raising on `.max()` of an empty array.
    """
    instance = _build_speech_vad(
        monkeypatch,
        tmp_path,
        predict_return=np.array([], dtype=np.float32),
    )

    result = instance.predict(np.zeros(1280, dtype=np.int16))

    assert result == 0.0
