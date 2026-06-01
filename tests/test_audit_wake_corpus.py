"""Tests for scripts/_audit_wake_corpus.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "_audit_wake_corpus.py"
_spec = importlib.util.spec_from_file_location("audit_wake_corpus", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
audit_wake_corpus = importlib.util.module_from_spec(_spec)
sys.modules["audit_wake_corpus"] = audit_wake_corpus
_spec.loader.exec_module(audit_wake_corpus)


def _write_wav(path: Path, *, value: int = 1200, duration_sec: float = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.full(int(16000 * duration_sec), value, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())


def _write_session(
    root: Path,
    *,
    session_id: str = "20260526T120000Z-abcd",
    include_raw0: bool = True,
    include_usb: bool = False,
    include_usb_dtln: bool = False,
    enabled_legs: list[str] | None = None,
    files: dict[str, str] | None = None,
    ports: dict[str, int] | None = None,
    capture_health: dict[str, object] | None = None,
) -> Path:
    metadata = root / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
        "raw0": str(root / "aec_raw0_nomusic" / "clip.aec-raw0.wav"),
    }
    data = {
        "session_id": session_id,
        "member": "jasper",
        "ports": ports or {"on": 9876, "off": 9877, "dtln": 9878, "raw0": 9879},
        "include_raw_mic_0": include_raw0,
        "include_usb_mic": include_usb,
        "include_usb_dtln": include_usb_dtln,
        "clips": [
            {
                "clip_id": "clip-1",
                "member": "jasper",
                "condition": "quiet",
                "distance": "near",
                "session_id": session_id,
                "seq": 1,
                "start_ts": "2026-05-26T12:00:00.000+00:00",
                "stop_ts": "2026-05-26T12:00:01.000+00:00",
                "duration_sec": 1.0,
                "files": files,
                "deleted": False,
                "auto_stopped": False,
                "notes": "",
                **({"capture_health": capture_health} if capture_health is not None else {}),
            },
        ],
    }
    if enabled_legs is not None:
        data["enabled_legs"] = enabled_legs
    path = metadata / f"enroll_jasper_{session_id}.json"
    path.write_text(json.dumps(data))
    return path


def test_resolve_pi_absolute_paths_to_local_corpus(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    local = root / "aec_on_nomusic" / "clip.aec-on.wav"
    _write_wav(local)

    resolved = audit_wake_corpus._resolve_wav_path(
        root,
        "/var/lib/jasper/enrollment_positives/aec_on_nomusic/clip.aec-on.wav",
    )

    assert resolved == local


def test_audit_passes_for_raw0_enabled_four_leg_session(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": "/var/lib/jasper/enrollment_positives/aec_on_nomusic/clip.aec-on.wav",
        "off": "/var/lib/jasper/enrollment_positives/aec_off_nomusic/clip.aec-off.wav",
        "dtln": "/var/lib/jasper/enrollment_positives/aec_dtln_nomusic/clip.aec-dtln.wav",
        "raw0": "/var/lib/jasper/enrollment_positives/aec_raw0_nomusic/clip.aec-raw0.wav",
    }
    for path_str in files.values():
        _write_wav(audit_wake_corpus._resolve_wav_path(root, path_str))
    _write_session(root, files=files)

    rc = audit_wake_corpus.audit(root, expect_raw0=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert "raw0-enabled sessions: 1/1" in out
    assert "Issues: none" in out


def test_audit_fails_when_raw0_flagged_but_missing_from_ports_and_files(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(
        root,
        include_raw0=True,
        files=files,
        ports={"on": 9876, "off": 9877, "dtln": 9878},
    )

    rc = audit_wake_corpus.audit(root, expect_raw0=True)

    out = capsys.readouterr().out
    assert rc == 1
    assert "metadata ports omit raw0" in out
    assert "missing expected leg(s): raw0" in out


def test_audit_min_per_cell_reports_sparse_coverage(tmp_path: Path, capsys) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(
        root,
        include_raw0=False,
        files=files,
        ports={"on": 9876, "off": 9877},
    )

    rc = audit_wake_corpus.audit(root, min_per_cell=1)

    out = capsys.readouterr().out
    assert rc == 1
    assert "coverage near/ambient: 0 clip(s)" in out
    assert "coverage far/music: 0 clip(s)" in out


def test_audit_accepts_enabled_legs_for_usb_ref_session(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
        "raw0": str(root / "aec_raw0_nomusic" / "clip.aec-raw0.wav"),
        "ref": str(root / "aec_ref_nomusic" / "clip.aec-ref.wav"),
        "usb_raw": str(root / "aec_usb_raw_nomusic" / "clip.aec-usb_raw.wav"),
        "usb_webrtc": str(root / "aec_usb_webrtc_nomusic" / "clip.aec-usb_webrtc.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(
        root,
        include_raw0=True,
        include_usb=True,
        enabled_legs=[
            "on", "off", "dtln", "raw0", "ref", "usb_raw", "usb_webrtc",
        ],
        files=files,
        ports={
            "on": 9876,
            "off": 9877,
            "dtln": 9878,
            "raw0": 9879,
            "ref": 9880,
            "usb_raw": 9881,
            "usb_webrtc": 9882,
        },
    )

    rc = audit_wake_corpus.audit(
        root,
        expect_raw0=True,
        expect_legs=("ref", "usb_raw", "usb_webrtc"),
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Issues: none" in out
    assert "'usb_webrtc': 1" in out


def test_expected_legs_accepts_chip_aec_profile_tokens() -> None:
    session = {
        "corpus_profile": "chip_aec_comparison_v1",
        "enabled_legs": [
            "chip_aec_150",
            "chip_aec_210",
            "raw0",
            "xvf_raw0_webrtc_aec3",
            "ref",
            "usb_raw",
            "usb_webrtc",
        ],
    }

    assert audit_wake_corpus._expected_legs(session) == (
        "chip_aec_150",
        "chip_aec_210",
        "raw0",
        "xvf_raw0_webrtc_aec3",
        "ref",
        "usb_raw",
        "usb_webrtc",
    )


def test_audit_prints_audio_context_summary(tmp_path: Path, capsys) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
        "raw0": str(root / "aec_raw0_nomusic" / "clip.aec-raw0.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    path = _write_session(root, files=files)
    data = json.loads(path.read_text())
    data["audio_context"] = {
        "production_audio_profile": {
            "requested": "xvf_software_aec3",
            "active": "xvf_software_aec3",
            "state": "active",
        },
        "microphone": {
            "name": "Seeed ReSpeaker XVF3800 (USB UA)",
            "firmware": {"label": "6-channel firmware"},
        },
        "dac_reference": {
            "validation": {"status": "pass"},
        },
    }
    data["clips"][0]["selected_legs"] = ["on", "off", "dtln", "raw0"]
    path.write_text(json.dumps(data))

    rc = audit_wake_corpus.audit(root)

    out = capsys.readouterr().out
    assert rc == 0
    assert "audio-context sessions: 1/1" in out
    assert "profile=xvf_software_aec3 state=active" in out
    assert "validation=pass" in out


def test_audit_accepts_usb_dtln_expected_leg(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
        "ref": str(root / "aec_ref_nomusic" / "clip.aec-ref.wav"),
        "usb_raw": str(root / "aec_usb_raw_nomusic" / "clip.aec-usb_raw.wav"),
        "usb_dtln": str(root / "aec_usb_dtln_nomusic" / "clip.aec-usb_dtln.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(
        root,
        include_raw0=False,
        include_usb_dtln=True,
        files=files,
        ports={
            "on": 9876,
            "off": 9877,
            "dtln": 9878,
            "ref": 9880,
            "usb_raw": 9881,
            "usb_dtln": 9883,
        },
    )

    rc = audit_wake_corpus.audit(root, expect_legs=("usb_dtln",))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Issues: none" in out
    assert "'usb_dtln': 1" in out


def test_audit_fails_when_expected_usb_leg_not_enabled(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
        "dtln": str(root / "aec_dtln_nomusic" / "clip.aec-dtln.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(root, include_raw0=False, files=files)

    rc = audit_wake_corpus.audit(root, expect_legs=("usb_webrtc",))

    out = capsys.readouterr().out
    assert rc == 1
    assert "expected leg 'usb_webrtc' not enabled" in out


def test_audit_fails_compromised_capture_health(tmp_path: Path, capsys) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "on": str(root / "aec_on_nomusic" / "clip.aec-on.wav"),
        "off": str(root / "aec_off_nomusic" / "clip.aec-off.wav"),
    }
    for path_str in files.values():
        _write_wav(Path(path_str))
    _write_session(
        root,
        include_raw0=False,
        files=files,
        ports={"on": 9876, "off": 9877},
        capture_health={"status": "compromised"},
    )

    rc = audit_wake_corpus.audit(root)

    out = capsys.readouterr().out
    assert rc == 1
    assert "capture health: {'compromised': 1}" in out
    assert "clip-1: capture health compromised" in out
