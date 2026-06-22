# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_export_wake_corpus_bundle.py."""
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
    / "_export_wake_corpus_bundle.py"
)
_spec = importlib.util.spec_from_file_location("export_wake_corpus_bundle", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
exporter = importlib.util.module_from_spec(_spec)
sys.modules["export_wake_corpus_bundle"] = exporter
_spec.loader.exec_module(exporter)


def _write_wav(path: Path, *, value: int = 1200, duration_sec: float = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.full(int(16000 * duration_sec), value, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())


def _write_bad_rate_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.full(8000, 100, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(samples.tobytes())


def _write_session(
    root: Path,
    *,
    session_id: str = "20260609T120000Z-abcd",
    condition: str = "music",
    distance: str = "near",
    files: dict[str, str] | None = None,
    capture_health: dict[str, object] | None = None,
    deleted: bool = False,
    label_kind: str | None = None,
    phrase: str | None = None,
    transcript: str | None = None,
) -> Path:
    metadata = root / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else {
        "chip_aec_150": (
            "/var/lib/jasper/enrollment_positives/"
            "aec_chip_aec_150_music/clip.aec-chip_aec_150.wav"
        ),
        "raw0": (
            "/var/lib/jasper/enrollment_positives/"
            "aec_raw0_music/clip.aec-raw0.wav"
        ),
    }
    data = {
        "metadata_schema_version": 2,
        "session_id": session_id,
        "member": "jasper",
        "corpus_profile": "chip_aec_comparison_v1",
        "enabled_legs": list(files.keys()),
        "capture_plan": {
            "recipe": "chip_aec_comparison",
            "legs": [
                {
                    "token": "chip_aec_150",
                    "label": "Chip AEC ASR 150",
                    "device_id": "xvf3800",
                    "native_stream": "chip_aec_asr_150",
                    "source_channel": "fixed_beam_150",
                    "processing": "hardware_aec",
                    "profile_role": "production_wake",
                    "wake_input": True,
                },
                {
                    "token": "raw0",
                    "label": "XVF raw0",
                    "device_id": "xvf3800",
                    "native_stream": "raw_mic_0",
                    "source_channel": "chip_channel_2",
                    "processing": "none",
                    "profile_role": "corpus_only",
                    "wake_input": False,
                },
            ],
        },
        "clips": [
            {
                "clip_id": "clip-1",
                "member": "jasper",
                "condition": condition,
                "distance": distance,
                "session_id": session_id,
                "seq": 1,
                "start_ts": "2026-06-09T12:00:00.000+00:00",
                "stop_ts": "2026-06-09T12:00:01.000+00:00",
                "duration_sec": 1.0,
                "files": files,
                "deleted": deleted,
                "auto_stopped": False,
                "notes": "",
                **({"label_kind": label_kind} if label_kind is not None else {}),
                **({"phrase": phrase} if phrase is not None else {}),
                **({"transcript": transcript} if transcript is not None else {}),
                **({"capture_health": capture_health} if capture_health is not None else {}),
            },
        ],
    }
    path = metadata / f"enroll_jasper_{session_id}.json"
    path.write_text(json.dumps(data))
    return path


def test_export_bundle_copies_audio_and_writes_training_manifest(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "chip_aec_150": (
            "/var/lib/jasper/enrollment_positives/"
            "aec_chip_aec_150_music/clip.aec-chip_aec_150.wav"
        ),
        "raw0": (
            "/var/lib/jasper/enrollment_positives/"
            "aec_raw0_music/clip.aec-raw0.wav"
        ),
    }
    for path_str in files.values():
        _write_wav(exporter._resolve_wav_path(root, path_str))
    _write_session(root, files=files)
    out = tmp_path / "bundle"

    summary = exporter.export_bundle(root, out, eval_fraction=0.2, seed=7)

    assert summary["session_count"] == 1
    assert summary["utterance_count"] == 1
    assert summary["manifest_row_count"] == 2
    assert summary["rejection_count"] == 0
    rows = [
        json.loads(line)
        for line in (out / "manifest.jsonl").read_text().splitlines()
    ]
    assert {row["leg"] for row in rows} == {"chip_aec_150", "raw0"}
    assert {row["split"] for row in rows} == {"train"}
    assert {row["utterance_id"] for row in rows} == {"20260609T120000Z-abcd:001"}
    chip = next(row for row in rows if row["leg"] == "chip_aec_150")
    assert chip["device_id"] == "xvf3800"
    assert chip["processing"] == "hardware_aec"
    assert chip["wake_input"] is True
    assert chip["label_kind"] == ""
    assert chip["phrase"] == ""
    assert chip["transcript"] == ""
    assert chip["sha256"]
    assert (out / chip["bundle_path"]).is_file()
    assert "chip_aec_150" in (out / "manifest.csv").read_text()
    assert chip["sha256"] in (out / "SHA256SUMS").read_text()


def test_export_preserves_label_metadata_for_negative_corpora(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    files = {
        "chip_aec_150": (
            "/var/lib/jasper/enrollment_positives/"
            "aec_chip_aec_150_music/clip.aec-chip_aec_150.wav"
        ),
    }
    for path_str in files.values():
        _write_wav(exporter._resolve_wav_path(root, path_str))
    _write_session(
        root,
        files=files,
        label_kind="hard_negative",
        phrase="hey harvest",
        transcript="hey harvest",
    )
    out = tmp_path / "bundle"

    exporter.export_bundle(root, out)

    row = json.loads((out / "manifest.jsonl").read_text().splitlines()[0])
    assert row["label_kind"] == "hard_negative"
    assert row["phrase"] == "hey harvest"
    assert row["transcript"] == "hey harvest"
    csv_text = (out / "manifest.csv").read_text()
    assert "label_kind" in csv_text
    assert "hard_negative" in csv_text


def test_split_is_assigned_per_utterance_across_legs(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    for idx in range(1, 4):
        session_id = f"20260609T12000{idx}Z-abcd"
        files = {
            "on": str(root / f"aec_on_music/clip{idx}.aec-on.wav"),
            "off": str(root / f"aec_off_music/clip{idx}.aec-off.wav"),
        }
        for path_str in files.values():
            _write_wav(Path(path_str), value=idx)
        _write_session(root, session_id=session_id, files=files)

    out = tmp_path / "bundle"
    exporter.export_bundle(root, out, eval_fraction=0.34, seed=1)

    rows = [
        json.loads(line)
        for line in (out / "manifest.jsonl").read_text().splitlines()
    ]
    splits_by_utterance: dict[str, set[str]] = {}
    for row in rows:
        splits_by_utterance.setdefault(row["utterance_id"], set()).add(row["split"])
    assert splits_by_utterance
    assert all(len(splits) == 1 for splits in splits_by_utterance.values())
    assert {row["split"] for row in rows} == {"train", "eval"}


def test_export_rejects_bad_wav_and_compromised_capture(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    good = root / "aec_on_music" / "good.aec-on.wav"
    bad_rate = root / "aec_off_music" / "bad.aec-off.wav"
    _write_wav(good)
    _write_bad_rate_wav(bad_rate)
    _write_session(
        root,
        files={"on": str(good), "off": str(bad_rate)},
        capture_health={
            "status": "clean",
            "legs": {
                "on": {"status": "clean"},
                "off": {"status": "clean"},
            },
        },
    )
    compromised = root / "aec_raw0_music" / "compromised.aec-raw0.wav"
    _write_wav(compromised)
    _write_session(
        root,
        session_id="20260609T120002Z-abcd",
        files={"raw0": str(compromised)},
        capture_health={
            "status": "compromised",
            "legs": {"raw0": {"status": "compromised"}},
        },
    )
    out = tmp_path / "bundle"

    summary = exporter.export_bundle(root, out)

    assert summary["manifest_row_count"] == 1
    rows = [
        json.loads(line)
        for line in (out / "manifest.jsonl").read_text().splitlines()
    ]
    assert [row["leg"] for row in rows] == ["on"]
    rejections = [
        json.loads(line)
        for line in (out / "rejections.jsonl").read_text().splitlines()
    ]
    assert {item["leg"] for item in rejections} == {"off", "raw0"}
    assert any("sample_rate:8000" in item.get("reason", "") or item.get("wav_issues") for item in rejections)


def test_manifest_only_keeps_source_paths_without_copy(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    wav = root / "aec_on_ambient" / "clip.aec-on.wav"
    _write_wav(wav)
    _write_session(
        root,
        condition="ambient",
        files={"on": str(wav)},
    )
    out = tmp_path / "bundle"

    exporter.export_bundle(root, out, copy_audio=False)

    row = json.loads((out / "manifest.jsonl").read_text().splitlines()[0])
    assert row["bundle_path"] == ""
    assert row["src_path"] == str(wav)
    assert not (out / "audio").exists()


def test_force_remove_guard_rejects_source_corpus_and_repo_root(tmp_path: Path) -> None:
    root = tmp_path / "enrollment_positives"
    nested = root / "exports"
    assert exporter._safe_to_remove_output(tmp_path / "bundle", corpus_dir=root)
    assert not exporter._safe_to_remove_output(root, corpus_dir=root)
    assert not exporter._safe_to_remove_output(nested, corpus_dir=root)
    assert not exporter._safe_to_remove_output(Path.cwd(), corpus_dir=root)
