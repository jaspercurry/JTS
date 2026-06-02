from __future__ import annotations

import sys
import types
from pathlib import Path

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
