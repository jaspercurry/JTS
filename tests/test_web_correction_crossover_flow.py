# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Secure correction crossover measurement flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker import web_measurement
from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow


def test_request_payload_parses_capture_query():
    handler = SimpleNamespace(
        path=(
            "/crossover/driver-capture?speaker_group_id=mono&role=woofer"
            "&playback_id=abc&test_level_dbfs=-42.5"
            "&has_mic_calibration=true&expect_null=0"
        )
    )

    payload = flow._request_payload(handler)

    assert payload == {
        "speaker_group_id": "mono",
        "role": "woofer",
        "playback_id": "abc",
        "test_level_dbfs": -42.5,
        "has_mic_calibration": True,
        "expect_null": False,
    }


def test_driver_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    calls = {}
    topology = object()
    preset = object()
    wav_path = tmp_path / "driver.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    monkeypatch.setattr(web_measurement, "capture_preset", lambda t: preset)
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {"sample_rate": 48000, "n_samples": 1},
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: ("curve", "cal-1", {"mode": "phase_aware"}),
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.safe_playback as safe_playback

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )
    monkeypatch.setattr(
        safe_playback,
        "load_safe_playback_state",
        lambda: {"status": "armed"},
    )

    def fake_record(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"recorded": True}

    monkeypatch.setattr(capture, "record_driver_acoustic_capture", fake_record)

    payload = backend.record_driver_capture(
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "playback_id": "play-1",
            "has_mic_calibration": True,
        },
        b"wav",
    )

    assert payload["recorded"] is True
    assert payload["calibration_id"] == "cal-1"
    assert payload["measurement_mode"] == {"mode": "phase_aware"}
    assert calls["args"] == (topology, preset)
    assert calls["kwargs"]["speaker_group_id"] == "mono"
    assert calls["kwargs"]["role"] == "woofer"
    assert calls["kwargs"]["captured_wav"] == wav_path
    assert calls["kwargs"]["playback_id"] == "play-1"
    assert calls["kwargs"]["calibration"] == "curve"


def test_summed_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    calls = {}
    topology = object()
    preset = object()
    wav_path = tmp_path / "summed.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    monkeypatch.setattr(web_measurement, "capture_preset", lambda t: preset)
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {"sample_rate": 48000, "n_samples": 1},
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: (None, None, {"mode": "magnitude_only"}),
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_capture as capture

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )

    def fake_record(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"recorded": True, "verdict": "blend_ok"}

    monkeypatch.setattr(capture, "record_summed_acoustic_capture", fake_record)

    payload = backend.record_summed_capture(
        {
            "speaker_group_id": "mono",
            "summed_test_id": "sum-1",
            "playback_id": "sum-1",
            "expect_null": True,
        },
        b"wav",
    )

    assert payload["recorded"] is True
    assert payload["measurement_mode"] == {"mode": "magnitude_only"}
    assert calls["args"] == (topology, preset)
    assert calls["kwargs"]["speaker_group_id"] == "mono"
    assert calls["kwargs"]["captured_wav"] == wav_path
    assert calls["kwargs"]["summed_test_id"] == "sum-1"
    assert calls["kwargs"]["expect_null"] is True


def test_backend_status_includes_active_speaker_commission_state(monkeypatch):
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {"ok": True, "targets": {"drivers": [], "summed": []}, "measurements": {}},
    )
    from jasper.active_speaker import web_commissioning

    monkeypatch.setattr(
        web_commissioning,
        "commission_status_payload",
        lambda: {"ramp": {"pending": None}},
    )

    payload = backend.status_payload()

    assert payload["commission"] == {"ramp": {"pending": None}}


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("driver-test", "driver-test"),
        ("driver-confirm", "driver-confirm"),
        ("driver-abort", "driver-abort"),
        ("summed-test", "summed-test"),
        ("driver-capture-sweep", "driver-capture-sweep"),
        ("summed-capture-sweep", "summed-capture-sweep"),
    ],
)
def test_crossover_module_calls_secure_measurement_routes(name, path):
    source = Path("deploy/assets/correction/js/crossover/main.js").read_text(
        encoding="utf-8",
    )

    assert f"'{path}'" in source or f'"{path}"' in source, name
    assert "micCaptureSupport" in source
    assert "support.message" in source
    assert "postJSON" in source
