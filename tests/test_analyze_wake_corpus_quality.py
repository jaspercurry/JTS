"""Tests for scripts/_analyze_wake_corpus_quality.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_analyze_wake_corpus_quality.py"
)
_spec = importlib.util.spec_from_file_location("analyze_wake_corpus_quality", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
analyzer = importlib.util.module_from_spec(_spec)
sys.modules["analyze_wake_corpus_quality"] = analyzer
_spec.loader.exec_module(analyzer)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.asarray(samples, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def _sine(
    *,
    duration_sec: float = 1.0,
    amp: float = 0.25,
    freq: float = 440.0,
) -> np.ndarray:
    t = np.arange(int(16000 * duration_sec)) / 16000.0
    return np.round(np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def _write_session(
    root: Path,
    *,
    session_id: str = "20260527T131954Z-7469",
    files: dict[str, str] | None = None,
) -> None:
    metadata = root / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    files = files or {
        "off": str(root / "aec_off_music" / "clip.aec-off.wav"),
        "on": str(root / "aec_on_music" / "clip.aec-on.wav"),
        "usb_raw": str(root / "aec_usb_raw_music" / "clip.aec-usb_raw.wav"),
        "usb_webrtc": str(
            root / "aec_usb_webrtc_music" / "clip.aec-usb_webrtc.wav"
        ),
    }
    data = {
        "session_id": session_id,
        "member": "jasper",
        "enabled_legs": list(files.keys()),
        "clips": [
            {
                "clip_id": "clip-1",
                "member": "jasper",
                "condition": "music",
                "distance": "near",
                "session_id": session_id,
                "seq": 1,
                "start_ts": "2026-05-27T13:19:54.000+00:00",
                "stop_ts": "2026-05-27T13:19:56.000+00:00",
                "duration_sec": 1.0,
                "files": files,
                "deleted": False,
                "auto_stopped": False,
                "notes": "",
            },
        ],
    }
    path = metadata / f"enroll_jasper_{session_id}.json"
    path.write_text(json.dumps(data))


def test_analyze_wav_flags_exact_clipping(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    path = root / "aec_usb_raw_music" / "clip.aec-usb_raw.wav"
    samples = _sine(amp=0.2)
    samples[100:106] = 32767
    _write_wav(path, samples)
    clip = analyzer.ClipRef(
        session_id="s1",
        seq=1,
        clip_id="clip-1",
        condition="music",
        distance="near",
        files={"usb_raw": str(path)},
    )

    row, _ = analyzer.analyze_wav(
        corpus_dir=root,
        clip=clip,
        leg="usb_raw",
        path_str=str(path),
        config=analyzer.AnalyzerConfig(),
    )

    assert row["exact_clip_count"] == 6
    assert "exact_clip" in row["flags"]
    assert "peak_gt_-1dbfs" in row["flags"]


def test_analyze_wav_finds_transient_candidate(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    path = root / "aec_usb_webrtc_music" / "clip.aec-usb_webrtc.wav"
    samples = _sine(amp=0.08)
    samples[3000] = 30000
    samples[3001] = -30000
    _write_wav(path, samples)
    clip = analyzer.ClipRef(
        session_id="s1",
        seq=1,
        clip_id="clip-1",
        condition="music",
        distance="near",
        files={"usb_webrtc": str(path)},
    )

    row, _ = analyzer.analyze_wav(
        corpus_dir=root,
        clip=clip,
        leg="usb_webrtc",
        path_str=str(path),
        config=analyzer.AnalyzerConfig(event_z=8.0, event_min_jump=0.015),
    )

    assert row["transient_event_count"] >= 1
    assert row["events"][0]["t_s"] == pytest_approx(3000 / 16000, abs=0.01)


def test_analyze_corpus_writes_repeatable_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "off": str(root / "aec_off_music" / "clip.aec-off.wav"),
        "on": str(root / "aec_on_music" / "clip.aec-on.wav"),
        "usb_raw": str(root / "aec_usb_raw_music" / "clip.aec-usb_raw.wav"),
        "usb_webrtc": str(root / "aec_usb_webrtc_music" / "clip.aec-usb_webrtc.wav"),
    }
    base = _sine(amp=0.15)
    _write_wav(Path(files["off"]), base)
    _write_wav(Path(files["on"]), np.clip(base * 1.2, -32768, 32767))
    _write_wav(Path(files["usb_raw"]), _sine(amp=0.20, freq=480.0))
    usb_webrtc = _sine(amp=0.22, freq=480.0)
    usb_webrtc[2000] = 29000
    _write_wav(Path(files["usb_webrtc"]), usb_webrtc)
    _write_session(root, files=files)
    out = tmp_path / "quality"

    result = analyzer.analyze_corpus(root, out)

    assert result["metrics_path"].exists()
    assert result["cross_path"].exists()
    assert result["events_path"].exists()
    assert result["summary_path"].exists()
    summary = result["summary_path"].read_text()
    assert "Wake Corpus Quality Summary" in summary
    assert "`usb_webrtc-usb_raw`" in summary
    events = json.loads(result["events_path"].read_text())
    assert events["events"]


def test_latest_session_filter_selects_newest_session(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    old_files = {"off": str(root / "aec_off_music" / "old.aec-off.wav")}
    new_files = {"off": str(root / "aec_off_music" / "new.aec-off.wav")}
    _write_wav(Path(old_files["off"]), _sine(amp=0.1))
    _write_wav(Path(new_files["off"]), _sine(amp=0.2))
    _write_session(root, session_id="20260527T120000Z-old", files=old_files)
    _write_session(root, session_id="20260527T131954Z-new", files=new_files)

    sessions, clips = analyzer._load_clips(root, session_ids=None, latest=1)

    assert [s["session_id"] for s in sessions] == ["20260527T131954Z-new"]
    assert [c.session_id for c in clips] == ["20260527T131954Z-new"]


def pytest_approx(value: float, *, abs: float):
    """Tiny local shim keeps this test import-light for dynamic script loading."""
    import pytest

    return pytest.approx(value, abs=abs)
