# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-close-reference``: the door, its refusals and its exit codes.

The physics is pinned in ``test_crossover_v2_close_reference.py``. What is
pinned here is the door: a capture bound to its program by CONTENT (#3504
watched a phase label point five captures at the wrong sweep), a refusal that
names the missing input instead of raising, and a report that carries its own
frame.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import fftconvolve

from jasper.active_speaker.crossover_v2.close_reference import (
    GATE_SOURCE_DECLARED,
    REFUSE_NO_CAPTURE,
)
from jasper.active_speaker.crossover_v2.round_captures import REFUSE_PROGRAM_UNMATCHED
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.sweep import synchronized_swept_sine
from jasper.cli.close_reference import AUTHORITY_TIER, build_parser, main

SAMPLE_RATE = 48000
EXIT_OK = 0
EXIT_REFUSED = 1


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    peak = float(np.max(np.abs(samples))) or 1.0
    wavfile.write(
        path, SAMPLE_RATE, (samples / peak * 0.9 * 32767).astype(np.int16)
    )


def _ir(distance_m: float) -> np.ndarray:
    """Direct arrival plus one floor bounce, at a mic distance."""
    ir = np.zeros(4096)
    ir[int(round(distance_m / 343.0 * SAMPLE_RATE)) + 64] = 1.0 / distance_m
    image = float(np.hypot(distance_m, 1.68))
    ir[int(round(image / 343.0 * SAMPLE_RATE)) + 64] = 1.0 / image
    return ir


def _round(root: Path, distance_m: float, *, take_id: str = "verify_01_a01") -> Path:
    """A banked round holding one on-axis summed capture and its program."""
    bundle = root / "bundle" / "0123456789ab"
    program_path = bundle / "crossover_v2" / "wired-fixture" / "verify_program.wav"
    sweep, _meta = synchronized_swept_sine(
        f1=100.0, f2=8000.0, duration_approx_s=0.4, sample_rate=SAMPLE_RATE
    )
    _write_wav(program_path, np.asarray(sweep, dtype=np.float64))
    program_bytes = program_path.read_bytes()

    stem = f"summed_{take_id}_{uuid.uuid4().hex}"
    wav = bundle / "summed" / f"{stem}.wav"
    program_pcm = wavfile.read(program_path)[1].astype(np.float64) / 32768.0
    _write_wav(wav, fftconvolve(program_pcm, _ir(distance_m)))
    (bundle / "summed" / f"{stem}.json").write_text(json.dumps({
        "position_id": take_id,
        "phase": "verify",
        "position_deg": 0,
        "vertical_deg": 0,
        "mark_distance_m": 1.0,
        "wav_path": f"summed/{stem}.wav",
        "curves": [{"role": "summed", "band_hz": [100.0, 8000.0]}],
        "provenance": {
            "stimulus": {
                "phase": "verify",
                "wav_sha256": hashlib.sha256(program_bytes).hexdigest(),
            }
        },
    }))
    return root


@pytest.fixture
def rounds(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _round(tmp_path / "far", 1.0),
        _round(tmp_path / "close", 0.30, take_id="verify_02_a01"),
    )


def _compare_argv(
    rounds: tuple[Path, Path], out: Path, *, geometry: Path | None = None
) -> list[str]:
    """Every invocation names a geometry path, so no test reads /var/lib."""
    far, close = rounds
    return [
        "compare", "--far-round", str(far), "--close-round", str(close),
        "--close-m", "0.30", "--far-m", "1.0", "--fc-hz", "6000",
        "--driver-diameter-in", "5.5", "--out", str(out),
        "--geometry", str(geometry or out.parent / "undeclared.json"),
    ]


def test_compare_publishes_its_frame_and_its_binding(rounds, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out)) == EXIT_OK
    report = json.loads(out.read_text())["close_reference"]
    assert report["schema_version"] == 1
    assert report["generated_by"] == "jasper-close-reference"
    assert set(report["frame"]) >= {
        "window_kind", "taper_fraction", "gate_lead_ms", "smooth_fraction",
        "detrend_fraction", "grid_hz", "n_fft", "alignment_band_hz",
        "gcc_upsample",
    }
    assert report["captures"]["far"]["program"] == "verify_program.wav"
    # The declared distance wins and the sidecar's pinned 1.0 m is disclosed.
    assert report["geometry"]["declared_distance_source"] == "caller"
    assert report["geometry"]["sidecar_mark_distance_m"]["close"] == 1.0
    assert report["geometry"]["sidecar_disagrees"] is True
    assert json.loads(capsys.readouterr().out)["status"] == "compared"


@pytest.mark.parametrize(
    "corrupt, reason",
    [("sha", REFUSE_PROGRAM_UNMATCHED), ("pose", REFUSE_NO_CAPTURE)],
)
def test_an_unbindable_capture_is_a_refusal_not_a_traceback(
    rounds, tmp_path, capsys, corrupt, reason
):
    far, _close = rounds
    sidecar = next(far.glob("**/summed/summed_*.json"))
    doc = json.loads(sidecar.read_text())
    if corrupt == "sha":
        doc["provenance"]["stimulus"]["wav_sha256"] = "0" * 64
    else:
        doc["position_deg"] = 22
    sidecar.write_text(json.dumps(doc))
    assert main(_compare_argv(rounds, tmp_path / "report.json")) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == reason


def test_a_declared_geometry_sets_each_windows_gate(rounds, tmp_path, capsys):
    """The close capture's own clean window is longer, and says where it came
    from: the first bounce's excess path grows as the mic nears the woofer."""
    geometry = tmp_path / "geometry.json"
    DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0
    ).save(geometry)
    out = tmp_path / "report.json"
    assert main(_compare_argv(rounds, out, geometry=geometry)) == EXIT_OK

    report = json.loads(out.read_text())["close_reference"]
    windows = {window["name"]: window for window in report["windows"]}
    assert {window["gate_source"] for window in windows.values()} == {
        GATE_SOURCE_DECLARED
    }
    for window in windows.values():
        assert window["gate_ms"] == pytest.approx(window["declared_clean_window_ms"])
    assert windows["close_window"]["gate_ms"] > windows["far_window"]["gate_ms"]
    assert report["geometry"]["declared_geometry"]["mic_height_m"] == 0.84


def test_distance_verb_prints_both_terms(capsys):
    assert main(["distance", "--driver-diameter-in", "5.5", "--fc-hz", "2500"]) == EXIT_OK
    record = json.loads(capsys.readouterr().out)["distance"]
    assert record["distance_in"] == pytest.approx(12.4, abs=0.1)
    assert record["margin_term_m"] > record["far_field_term_m"]
    assert record["placement_tolerance_db"] > 0.0


def test_the_tool_menu_can_render_this_tool():
    assert AUTHORITY_TIER == "advisory (plays nothing)"
    assert build_parser().prog == "jasper-close-reference"
