# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement import sweep
from jasper.correction import bundle_tools, bundles, interop
from jasper.correction.session import MeasurementSession, SessionState
from .correction_session_fixtures import make_measurement_session


async def _complete_one_position_bundle(
    tmp_path: Path, *, tail_s: float = 0.0,
) -> MeasurementSession:
    """One READY single-position bundle.

    ``tail_s`` pads the capture past the sweep so the length cap engages —
    the analysis reaches it through ``cap_capture_length`` and a fresh replay
    through ``deconvolve``'s own guard, and a test that never pads exercises
    neither route.
    """
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
    if tail_s:
        sweep_signal = np.concatenate([
            sweep_signal,
            np.zeros(int(tail_s * sample_rate), dtype=sweep_signal.dtype),
        ])
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


async def test_banked_responses_are_read_back_without_a_replay(tmp_path: Path):
    """The artifact the analysis banked is what `inspect_bundle` reports.

    Proved by mutating the banked file and reading the mutation back: a
    value that could not have come from deconvolving the WAV is the only
    way to show the bytes are being opened rather than re-derived.
    """
    sess = await _complete_one_position_bundle(tmp_path)
    response_path = sess.bundle_dir / "analysis" / "p0_response.json"
    payload = json.loads(response_path.read_text())
    payload["direct_arrival"] = {"marker": "read-from-disk"}
    response_path.write_text(json.dumps(payload))

    rows = bundle_tools.banked_response_facts(sess.bundle_dir)

    assert [row["stem"] for row in rows] == ["p0"]
    row = rows[0]
    assert row["artifact_path"] == "analysis/p0_response.json"
    assert row["capture_kind"] == "measurement"
    assert row["position_index"] == 0
    assert row["direct_arrival"] == {"marker": "read-from-disk"}
    assert row["analysis_curve"]["freq_count"] > 0
    assert row["analysis_curve"]["f_min_hz"] < row["analysis_curve"]["f_max_hz"]
    assert bundle_tools.inspect_bundle(sess.bundle_dir)["banked_responses"] == rows


async def test_the_replay_grades_each_capture_against_its_banked_curve(
    tmp_path: Path,
):
    """A drift is attributed to a capture, not only to the average."""
    sess = await _complete_one_position_bundle(tmp_path)

    honest = bundle_tools.recompute_bundle_summary(sess.bundle_dir)
    assert [d["stem"] for d in honest["banked_capture_deltas"]] == ["p0"]
    assert honest["banked_capture_deltas"][0]["max_abs_db"] < 0.01

    response_path = sess.bundle_dir / "analysis" / "p0_response.json"
    payload = json.loads(response_path.read_text())
    payload["analysis_curve"]["magnitude_db"] = [
        db + 6.0 for db in payload["analysis_curve"]["magnitude_db"]
    ]
    response_path.write_text(json.dumps(payload))

    drifted = bundle_tools.recompute_bundle_summary(sess.bundle_dir)
    assert drifted["banked_capture_deltas"][0]["max_abs_db"] == pytest.approx(
        6.0, abs=1e-3,
    )
    # The spatial-average check is unaffected: it reads a different artifact,
    # which is what keeps the two answers independent.
    assert drifted["stored_average_delta"]["rms_db"] < 0.01

    response_path.unlink()
    absent = bundle_tools.recompute_bundle_summary(sess.bundle_dir)
    assert absent["banked_capture_deltas"] == [
        {"stem": "p0", "unavailable": "no banked response artifact"}
    ]


async def test_the_banked_impulse_response_is_what_a_replay_derives(
    tmp_path: Path,
):
    """The agreement the IR export now rests on, asserted rather than assumed.

    ``export_bundle`` copies the banked IR instead of re-deconvolving. That is
    only safe while the two paths agree, and they reach the length cap by
    different routes — the analysis calls ``cap_capture_length`` before
    deconvolving, a fresh replay hits the same cap inside ``deconvolve``. The
    40 s tail engages that cap on both sides (the run logs
    ``exceeds cap ... truncating`` and ``code=capture_truncated``), so a
    capture that never padded would exercise neither route.

    EXACT equality is the assertion, not a tolerance: both routes run the same
    ``deconv.deconvolve`` over the same samples in one process, so any
    difference at all means the two derivations diverged — it is not float
    drift, and a tolerance wide enough to absorb drift is wide enough to hide
    the divergence this pin exists for.
    """
    sess = await _complete_one_position_bundle(tmp_path, tail_s=40.0)

    banked, banked_rate = sweep.read_wav_mono(
        sess.bundle_dir / "analysis" / "p0_ir.wav"
    )
    info = json.loads((sess.bundle_dir / "info.json").read_text())
    replayed, replayed_rate = interop.impulse_response_from_capture(
        sess.capture_path_for_position(0),
        sweep_meta=info["sweep_meta"],
    )

    assert banked_rate == replayed_rate
    assert banked.shape == replayed.shape
    assert np.array_equal(banked, replayed)


async def test_the_ir_export_hands_over_the_bundles_own_evidence(tmp_path: Path):
    """Byte-for-byte the banked artifact, proved by mutating it.

    A value that could not have come from deconvolving the capture is the only
    way to show the export is copying rather than re-deriving.
    """
    sess = await _complete_one_position_bundle(tmp_path)
    banked = sess.bundle_dir / "analysis" / "p0_ir.wav"
    marked = banked.read_bytes() + b"JTS-MARKER"
    banked.write_bytes(marked)

    out_dir = tmp_path / "exported"
    bundle_tools.export_bundle(sess.bundle_dir, out_dir)

    exported = out_dir / f"{sess.bundle_dir.name}-p0-ir.wav"
    assert exported.read_bytes() == marked


async def test_a_bundle_with_no_banked_ir_still_exports_one(tmp_path: Path):
    """The era bridge: a bundle banked before `replay_artifacts` existed."""
    sess = await _complete_one_position_bundle(tmp_path)
    (sess.bundle_dir / "analysis" / "p0_ir.wav").unlink()

    out_dir = tmp_path / "exported"
    bundle_tools.export_bundle(sess.bundle_dir, out_dir)

    exported = out_dir / f"{sess.bundle_dir.name}-p0-ir.wav"
    assert exported.is_file()
    derived, _rate = sweep.read_wav_mono(exported)
    assert derived.size > 0


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
