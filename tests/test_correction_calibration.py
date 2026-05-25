from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from jasper.correction import calibration


SAMPLE_CAL = """# freq correction phase
20 -2.0 0
100 0.5 12
1000 1.5 20
20000 -1.0 0
"""


def test_parse_calibration_text_accepts_common_curve_shape():
    curve = calibration.parse_calibration_text(SAMPLE_CAL)
    assert curve.freqs_hz == [20.0, 100.0, 1000.0, 20000.0]
    assert curve.correction_db == [-2.0, 0.5, 1.5, -1.0]
    assert curve.phase_deg == [0.0, 12.0, 20.0, 0.0]


def test_parse_calibration_text_can_invert_response_curve():
    curve = calibration.parse_calibration_text(
        "20 -2\n100 3\n",
        sign_convention="response",
    )
    assert curve.correction_db == [2.0, -3.0]


def test_apply_calibration_curve_interpolates_on_measurement_grid():
    curve = calibration.parse_calibration_text("20 -2\n100 0\n1000 2\n")
    freqs = np.array([20.0, 60.0, 100.0, 1000.0])
    mag = np.array([0.0, 0.0, 0.0, 0.0])
    corrected = calibration.apply_calibration_curve(freqs, mag, curve)
    assert corrected[0] == -2.0
    assert corrected[2] == 0.0
    assert corrected[3] == 2.0
    assert -2.0 < corrected[1] < 0.0


def test_apply_calibration_curve_interpolates_in_log_frequency():
    curve = calibration.parse_calibration_text("10 0\n1000 2\n")
    freqs = np.array([100.0])
    corrected = calibration.apply_calibration_curve(
        freqs, np.array([0.0]), curve,
    )
    assert corrected[0] == pytest.approx(1.0)


def test_store_load_roundtrip_redacts_serial_from_public_metadata(tmp_path: Path):
    record = calibration.store_calibration(
        text=SAMPLE_CAL,
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        serial="SECRET-123",
        root=tmp_path,
    )
    loaded = calibration.load_calibration_record(
        record.calibration_id,
        root=tmp_path,
    )
    assert loaded.calibration_id == record.calibration_id
    assert loaded.curve.freqs_hz == record.curve.freqs_hz
    public = loaded.public_metadata()
    assert public["serial_hash"]
    assert public["source"] == "uploaded_file"
    assert "SECRET" not in str(public)
    assert Path(loaded.raw_path).exists()
    assert Path(loaded.metadata_path).exists()
    assert (Path(loaded.raw_path).stat().st_mode & 0o777) == 0o600
    assert (Path(loaded.metadata_path).stat().st_mode & 0o777) == 0o600


def test_dayton_fetch_posts_form_and_follows_calibration_link():
    calls: list[urllib.request.Request | str] = []

    def fake_open(req, timeout):
        calls.append(req)
        if isinstance(req, urllib.request.Request):
            data = urllib.parse.parse_qs(req.data.decode())
            assert data["Microphone"] == ["UMM-6"]
            assert data["SerialNumber"] == ["ABC123"]
            return b'<html><a href="/files/umm6_abc123.txt">cal</a></html>'
        assert req == "https://support.daytonaudio.com/files/umm6_abc123.txt"
        return b"20 -1\n100 0\n1000 1\n"

    text, source = calibration.fetch_dayton_calibration_text(
        vendor_model="UMM-6",
        serial="ABC123",
        opener=fake_open,
    )
    assert "1000 1" in text
    assert source.endswith("umm6_abc123.txt")
    assert len(calls) == 2


def test_minidsp_fetch_uses_serial_url_candidates():
    seen: list[str] = []

    def fake_open(req, timeout):
        assert isinstance(req, str)
        seen.append(req)
        if req.endswith("/7001234.txt"):
            return b"20 -1\n100 0\n1000 1\n"
        raise OSError("not found")

    text, source = calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-1",
        serial="700-1234",
        opener=fake_open,
    )
    assert "1000 1" in text
    assert source.endswith("/7001234.txt")
    assert seen[0] == "https://www.minidsp.com/images/umik/7001234.txt"


def test_minidsp_fetch_prefers_90deg_file_when_requested():
    seen: list[str] = []

    def fake_open(req, timeout):
        assert isinstance(req, str)
        seen.append(req)
        if req.endswith("/7001234_90deg.txt"):
            return b"20 -1\n100 0\n1000 1\n"
        raise OSError("not found")

    _text, source = calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-1",
        serial="700-1234",
        orientation="90deg",
        opener=fake_open,
    )
    assert source.endswith("/7001234_90deg.txt")
    assert seen[0].endswith("/7001234_90deg.txt")


def test_fetch_vendor_calibration_stores_known_mic_record(tmp_path: Path):
    def fake_open(req, timeout):
        return b"20 -1\n100 0\n1000 1\n"

    record = calibration.fetch_vendor_calibration(
        model_key="minidsp_umik1",
        serial="700-1234",
        root=tmp_path,
        opener=fake_open,
    )
    assert record.provider == "minidsp"
    assert record.model == "minidsp_umik1"
    assert record.source.endswith("/7001234.txt")
    public = record.public_metadata()
    assert public["source"] == "vendor_lookup"
    assert "7001234" not in str(public)
    assert record.serial_hash
    assert Path(record.raw_path).exists()
