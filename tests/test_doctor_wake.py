# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor wake domain."""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace


from jasper import wake_models
from jasper.cli import doctor


# -------------------------------------------------- openWakeWord assets


def _install_fake_openwakeword_package(
    monkeypatch,
    tmp_path: Path,
    files: dict[str, bytes],
    required_assets: tuple[SimpleNamespace, ...],
    package_assets: tuple[SimpleNamespace, ...] | None = None,
) -> None:
    pkg = tmp_path / "openwakeword"
    models = pkg / "resources" / "models"
    models.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for filename, payload in files.items():
        (models / filename).write_bytes(payload)
    monkeypatch.setitem(
        sys.modules,
        "openwakeword",
        SimpleNamespace(__file__=str(pkg / "__init__.py")),
    )
    monkeypatch.setattr(
        wake_models,
        "required_openwakeword_assets",
        lambda: required_assets,
    )
    if package_assets is not None:
        monkeypatch.setattr(
            wake_models,
            "openwakeword_assets",
            lambda: package_assets,
        )


def _fake_asset(filename: str, payload: bytes = b"model") -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        download_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _required_fake_openwakeword_assets() -> tuple[SimpleNamespace, ...]:
    return (
        _fake_asset("embedding_model.onnx"),
        _fake_asset("melspectrogram.onnx"),
        _fake_asset("silero_vad.onnx"),
    )


def _fake_openwakeword_assets() -> tuple[SimpleNamespace, ...]:
    return _required_fake_openwakeword_assets() + (
        SimpleNamespace(
            key="hey_jarvis",
            filename="hey_jarvis_v0.1.onnx",
            download_sha256=hashlib.sha256(b"model").hexdigest(),
        ),
        SimpleNamespace(
            key="alexa",
            filename="alexa_v0.1.onnx",
            download_sha256=hashlib.sha256(b"model").hexdigest(),
        ),
    )


def test_openwakeword_doctor_fails_when_silero_asset_missing(
    monkeypatch, tmp_path: Path
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "silero_vad.onnx" in r.detail


def test_openwakeword_doctor_allows_missing_inactive_bundled_model(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "ok"
    assert "hey_jarvis_v0.1.onnx" in r.detail


def test_openwakeword_doctor_fails_when_active_bundled_model_missing(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "active wake model" in r.detail
    assert "deploy/install.sh" in r.detail


def test_openwakeword_doctor_missing_custom_model_points_at_path(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )
    missing = tmp_path / "custom-missing.onnx"

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model=str(missing)))

    assert r.status == "fail"
    assert f"active wake model path missing: {missing}" in r.detail
    assert "registered model in /wake/" in r.detail


def test_openwakeword_doctor_fails_when_required_asset_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"wrong-model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "hash mismatch" in r.detail
    assert "silero_vad.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_openwakeword_doctor_fails_when_active_external_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    active_model = tmp_path / "jarvis_v2.onnx"
    active_model.write_bytes(b"wrong-model")
    monkeypatch.setattr(
        wake_models,
        "by_model",
        lambda model: (
            SimpleNamespace(
                download_sha256=hashlib.sha256(b"model").hexdigest(),
            )
            if model == str(active_model)
            else None
        ),
    )
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model=str(active_model)))

    assert r.status == "fail"
    assert "active wake model hash mismatch" in r.detail
    assert "jarvis_v2.onnx" in r.detail


def test_openwakeword_doctor_fails_when_active_bundled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"wrong-model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "active wake model hash mismatch" in r.detail
    assert "hey_jarvis_v0.1.onnx" in r.detail


# ---------------------------------------------------------------------------
# _assess_wake_legs — configured intent vs runtime-armed legs
# (the runtime cross-check added with /state.voice.wake_legs)
# ---------------------------------------------------------------------------


def test_assess_wake_legs_skips_when_aec_disabled():
    r = doctor._assess_wake_legs(
        "disabled",
        raw=True,
        dtln=False,
        armed_runtime=None,
    )
    assert r.status == "ok"
    assert "n/a" in r.detail


def test_assess_wake_legs_reports_intent_when_daemon_unreachable():
    """armed_runtime=None (jasper-control down) → fall back to configured
    intent, never a false 'leg skipped' warning."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime=None,
    )
    assert r.status == "ok"
    assert "configured" in r.detail
    assert "aec3" in r.detail and "raw" in r.detail
    assert "/wake/" in r.detail  # not the stale /system


def test_assess_wake_legs_ok_when_runtime_matches_config():
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=True,
        armed_runtime={"on", "off", "dtln"},
    )
    assert r.status == "ok"
    assert "3 leg(s) armed" in r.detail


def test_assess_wake_legs_warns_when_configured_leg_not_armed():
    """The whole point: raw is configured on, but the daemon only opened
    the primary leg (a startup skip). Surface it instead of claiming
    'armed' off stale config. raw maps to the chip-direct "off" token."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime={"on"},
    )
    assert r.status == "warn"
    assert "off" in r.detail  # the missing leg (raw -> off)
    assert "wake.leg_skipped" in r.detail  # actionable hint


def test_assess_wake_legs_dtln_skip_warns():
    """DTLN configured but not armed (model OOM / bridge not emitting on
    :9878) → warn naming dtln."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=True,
        armed_runtime={"on", "off"},
    )
    assert r.status == "warn"
    assert "dtln" in r.detail


def test_assess_wake_legs_chip_aec_does_not_false_warn_on_cleared_raw():
    """Chip-AEC mutual exclusion: the reconciler clears raw/DTLN *device*
    vars when chip is on but preserves their booleans as wizard intent. So
    raw=True can coexist with chip_aec=True, and the default armed set is
    the primary chip beam on the "on" leg — with NO "off" leg and no extra
    beam detectors. This is the resource regression this fix prevents."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime={"on"},
        chip_aec=True,
    )
    assert r.status == "ok", r.detail
    assert "1 leg(s) armed" in r.detail
    assert "off" not in r.detail


def test_assess_wake_legs_chip_aec_warns_when_enabled_extra_beams_not_armed():
    """Extra chip-AEC beam toggles are explicit. When configured but not
    armed, warn naming the missing beams."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime={"on"},
        chip_aec=True,
        chip_aec_150=True,
        chip_aec_210=True,
    )
    assert r.status == "warn"
    assert "chip_aec_150" in r.detail and "chip_aec_210" in r.detail
    assert "6-ch firmware" in r.detail


def test_assess_wake_legs_chip_aec_warns_on_unexpected_extra_beams():
    """Default chip-AEC should arm only the primary beam; extra armed
    beams are resource burn, so doctor should not report ok."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime={"on", "chip_aec_150"},
        chip_aec=True,
    )
    assert r.status == "warn"
    assert "unexpected wake legs" in r.detail
    assert "chip_aec_150" in r.detail


def test_assess_wake_legs_chip_aec_intent_when_daemon_unreachable():
    """Daemon down + chip configured → report chip intent, never a false
    leg-skip warning."""
    r = doctor._assess_wake_legs(
        "auto",
        raw=True,
        dtln=False,
        armed_runtime=None,
        chip_aec=True,
    )
    assert r.status == "ok"
    assert "primary_chip_beam" in r.detail
    # raw is on but mutual exclusion means it isn't part of the chip config.
    assert "raw" not in r.detail
