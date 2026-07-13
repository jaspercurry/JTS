# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from jasper.audio_measurement import sweep
from jasper.correction import bundle_tools, bundles, interop
from jasper.correction.session import MeasurementSession, SessionState
from .correction_session_fixtures import make_measurement_session


async def _complete_one_position_bundle(tmp_path: Path) -> MeasurementSession:
    sess = make_measurement_session(
        tmp_path,
        input_device={
            "label": "USB measurement mic",
            "device_id_hash": "abc123",
            "sample_rate": 48000,
            "channel_count": 1,
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
        },
    )
    sess.noise_floor_db = -80.0

    async def fake_play_sweep(path: str, **kwargs):
        return None

    await sess.prepare_and_play_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_CAPTURE

    sweep_signal, sample_rate = sweep.read_wav_mono(sess.sweep_wav_path)
    capture_path = sess.capture_path_for_position(0)
    sweep.write_sweep_wav(capture_path, sweep_signal, sample_rate)
    await sess.on_capture_uploaded(capture_path)
    assert sess.state == SessionState.READY
    return sess


def test_frequency_response_text_is_rew_friendly():
    text = interop.format_frequency_response_text(
        {
            "freqs_hz": [100.0, 20.0],
            "magnitude_db": [-3.0, 1.5],
        },
        title="JTS measured",
        source="/tmp/bundle",
    )

    assert "# Columns: frequency_hz magnitude_db phase_deg" in text
    rows = [line for line in text.splitlines() if not line.startswith("#")]
    assert rows[0].startswith("20.000000\t1.500000\t0.000000")
    assert rows[1].startswith("100.000000\t-3.000000\t0.000000")


@pytest.mark.asyncio
async def test_bundle_inspect_recompute_and_export(tmp_path: Path):
    sess = await _complete_one_position_bundle(tmp_path)

    inspected = bundle_tools.inspect_bundle(sess.bundle_dir, recompute=True)
    assert inspected["session_id"] == sess.session_id
    assert inspected["state"] == "ready"
    assert inspected["raw_capture_count"] == 1
    assert inspected["exports_available"]["frequency_response_text"] is True
    assert inspected["exports_available"]["impulse_response_wav"] is True
    assert inspected["confidence"]["level"] in {"medium", "low"}
    assert inspected["acoustic_quality"]["snr_level"] == "high"
    assert inspected["recompute"]["position_count"] == 1
    assert inspected["recompute"]["stored_average_delta"]["rms_db"] < 0.01

    out_dir = tmp_path / "exported"
    exported = bundle_tools.export_bundle(sess.bundle_dir, out_dir)
    exported_names = {Path(path).name for path in exported["written"]}
    assert f"{sess.session_id}-measured.frd" in exported_names
    assert f"{sess.session_id}-measured.txt" in exported_names
    assert f"{sess.bundle_dir.name}-p0-ir.wav" in exported_names
    assert (out_dir / f"{sess.session_id}-measured.frd").read_text().startswith(
        "# JTS measured correction curve"
    )


def test_bundle_export_refuses_empty_bundle(tmp_path: Path):
    bundle_dir = tmp_path / "empty-session"
    bundle_dir.mkdir()

    with pytest.raises(bundle_tools.BundleToolError, match="no exportable"):
        bundle_tools.export_bundle(bundle_dir, tmp_path / "exported")


def test_bundle_calibration_reader_allows_absent_file(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    assert bundle_tools._load_bundle_calibration(bundle_dir) is None


@pytest.mark.parametrize(
    "contents",
    (
        "{",
        "[]",
        "{}",
        '{"curve": null}',
        '{"curve": []}',
        '{"curve": "invalid"}',
        '{"curve": {}}',
        json.dumps({
            "curve": {
                "freqs_hz": [20.0, "1000"],
                "correction_db": [0.0, 0.0],
            }
        }),
    ),
)
def test_bundle_calibration_reader_rejects_present_malformed_file(
    tmp_path: Path,
    contents: str,
):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "mic_calibration.json").write_text(contents)

    with pytest.raises(bundle_tools.BundleToolError) as exc:
        bundle_tools._load_bundle_calibration(bundle_dir)
    assert "mic_calibration.json" in str(exc.value)


@pytest.mark.asyncio
async def test_snr_estimate_is_recorded_in_capture_quality(tmp_path: Path):
    sess = await _complete_one_position_bundle(tmp_path)
    report = sess.capture_quality[0]
    assert report["noise_floor_dbfs"] == -80.0
    assert report["estimated_snr_db"] > 20.0

    acoustic = json.loads((sess.bundle_dir / "acoustic_quality.json").read_text())
    assert acoustic["summary"]["snr_level"] == "high"
    assert acoustic["summary"]["min_estimated_snr_db"] == (
        report["estimated_snr_db"]
    )

    result = json.loads((sess.bundle_dir / "result.json").read_text())
    assert result["capture_quality"][0]["estimated_snr_db"] == (
        report["estimated_snr_db"]
    )
    assert result["acoustic_quality"]["snr_level"] == "high"
    assert not any(
        issue.severity == "fail"
        for issue in bundles.validate_bundle(sess.bundle_dir)
    )
