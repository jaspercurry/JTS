# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for scripts/jasper-pipe-probe.

Most cases go through the real CLI (subprocess + `analyze --json`), not an
importlib direct-load: the script has no .py suffix, and
importlib.util.spec_from_file_location returns None for an unrecognized
suffix without an explicit loader. One case (record pairing/reassembly)
has no CLI surface at all -- it is exercised via a direct module load
instead, see _load_module().
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "jasper-pipe-probe"
RATE = 48_000


def _load_module():
    """Direct import for reassemble_records(), which has no CLI surface.
    Needs an explicit loader (the script's missing .py suffix defeats
    spec_from_file_location's own suffix-based loader guess) AND
    registration in sys.modules BEFORE exec_module runs: the script's
    dataclasses combine with `from __future__ import annotations` (PEP
    563 string annotations), and dataclass's own type resolution reads
    `sys.modules[cls.__module__]` while the class body executes."""
    loader = importlib.machinery.SourceFileLoader("jasper_pipe_probe", str(SCRIPT))
    spec = importlib.util.spec_from_file_location("jasper_pipe_probe", str(SCRIPT), loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


jasper_pipe_probe = _load_module()


def _write_stereo_raw(path: Path, mono: np.ndarray) -> None:
    pcm = (mono * 32767.0).astype("<i2")
    stereo = np.column_stack([pcm, pcm]).astype("<i2")
    path.write_bytes(stereo.tobytes())


def _analyze(path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "analyze", str(path), *extra_args],
        check=True, text=True, capture_output=True, timeout=30,
    )


def _analyze_json(path: Path) -> dict:
    return json.loads(_analyze(path, "--json").stdout)


def _probe_shaped_buffer() -> np.ndarray:
    """A 44 s, fully-populated buffer aligned to PROBE_SECTIONS' real
    layout, so section_label_at() labels each second the way a real,
    well-aligned capture would (needed to reach the dual-tone/toggle
    section-aware behavior at all)."""
    sections = jasper_pipe_probe.PROBE_SECTIONS
    ref = jasper_pipe_probe.REFERENCE_TONE_HZ
    second_tone = jasper_pipe_probe.SECOND_TONE_HZ
    total_s = sum(duration for _label, duration in sections)
    mono = np.zeros(round(total_s * RATE))
    cursor_s = 0.0
    for label, duration in sections:
        start = round(cursor_s * RATE)
        n = round(duration * RATE)
        t = np.arange(n) / RATE
        if label == jasper_pipe_probe.DUAL_TONE_SECTION_LABEL:
            chunk = 0.15 * np.sin(2 * np.pi * second_tone * t) + 0.15 * np.sin(2 * np.pi * ref * t)
        elif label == jasper_pipe_probe.TOGGLE_SECTION_LABEL:
            cycle = round(0.5 * RATE)
            on = ((np.arange(n) // cycle) % 2) == 0
            chunk = 0.1 * np.sin(2 * np.pi * ref * t) * on
        elif label == jasper_pipe_probe.NOISE_SECTION_LABEL:
            chunk = 0.05 * np.random.default_rng(1).standard_normal(n)
        else:
            chunk = 0.1 * np.sin(2 * np.pi * ref * t)
        mono[start:start + n] = chunk
        cursor_s += duration
    return mono


def test_clean_10khz_tone_reads_low_thd_n_and_no_glitches(tmp_path: Path) -> None:
    n = RATE * 3
    t = np.arange(n) / RATE
    tone = 0.3 * np.sin(2 * np.pi * 10_000.0 * t)
    raw = tmp_path / "clean.raw"
    _write_stereo_raw(raw, tone)

    readings = _analyze_json(raw)["channels"]["ch0_woofer"]["readings"]

    assert len(readings) == 3
    for row in readings:
        assert row["silent"] is False
        assert row["dominant_hz"] == pytest.approx(10_000.0, abs=1.0)
        assert row["thd_n_db"] < -60.0
        assert row["phase_glitches"] == 0


def test_single_sample_splice_per_second_reports_glitches(tmp_path: Path) -> None:
    n = RATE * 3
    t = np.arange(n) / RATE
    tone = 0.3 * np.sin(2 * np.pi * 10_000.0 * t)
    # A splice at an exact half-second (RATE // 2) lands on a zero-crossing
    # for a 10 kHz tone at 48 kHz (10000 Hz * 0.5 s = 5000 whole cycles) --
    # negating a ~0 sample changes ~nothing. Offset into the second instead.
    splice_offset = 12_345
    for second in range(3):
        idx = second * RATE + splice_offset
        tone[idx] = -tone[idx]
    raw = tmp_path / "spliced.raw"
    _write_stereo_raw(raw, tone)

    readings = _analyze_json(raw)["channels"]["ch0_woofer"]["readings"]

    for row in readings:
        assert row["phase_glitches"] > 0


def test_dual_tone_row_reads_low_thd_n(tmp_path: Path) -> None:
    """A 10 kHz-only reference misreads the dual-tone section's 9 kHz
    partner as noise (proven: it read 0.00 dB in review); section-aware
    thd_n_db fixes it -- this pins the fix through the real analyze path."""
    raw = tmp_path / "probe.raw"
    _write_stereo_raw(raw, _probe_shaped_buffer())

    readings = _analyze_json(raw)["channels"]["ch0_woofer"]["readings"]
    dual_tone_rows = [r for r in readings if r["section"] == jasper_pipe_probe.DUAL_TONE_SECTION_LABEL]

    assert dual_tone_rows
    for row in dual_tone_rows:
        assert row["thd_n_db"] is not None
        assert row["thd_n_db"] < -60.0


def test_toggle_row_is_marked_envelope_test_with_null_metrics(tmp_path: Path) -> None:
    raw = tmp_path / "probe.raw"
    _write_stereo_raw(raw, _probe_shaped_buffer())

    readings = _analyze_json(raw)["channels"]["ch0_woofer"]["readings"]
    toggle_rows = [r for r in readings if r["section"] == jasper_pipe_probe.TOGGLE_SECTION_LABEL]

    assert toggle_rows
    for row in toggle_rows:
        assert row["envelope_test"] is True
        assert row["thd_n_db"] is None
        assert row["phase_glitches"] is None
        assert row["reason"]


def test_all_silent_capture_parses_as_strict_json(tmp_path: Path) -> None:
    raw = tmp_path / "silent.raw"
    _write_stereo_raw(raw, np.zeros(RATE * 3))

    result = _analyze(raw, "--json")

    assert "Infinity" not in result.stdout
    assert "NaN" not in result.stdout
    payload = json.loads(result.stdout)  # raises if this is not valid JSON
    summary = payload["channels"]["ch0_woofer"]["summary"]
    assert summary["audible_seconds"] == 0
    assert summary["worst_thd_n_db"] is None


def test_pairing_preserves_count_and_flags_violations() -> None:
    def record(payload: bytes) -> bytes:
        return struct.pack("<I", len(payload)) + payload

    # Long silent runs, byte-identical pairs: count preserved exactly.
    silent_payload = b"\x00" * 512
    clean_stream = b"".join(record(silent_payload) + record(silent_payload) for _ in range(200))
    clean_result = jasper_pipe_probe.reassemble_records(clean_stream)
    assert clean_result.pairing_ok is True
    assert len(clean_result.payloads) == 200
    assert all(p == silent_payload for p in clean_result.payloads)

    # A violated pair: the warning path is taken, and nothing is dropped.
    bad_stream = record(b"AAAA") + record(b"AAAA") + record(b"BBBB") + record(b"ZZZZ")
    bad_result = jasper_pipe_probe.reassemble_records(bad_stream)
    assert bad_result.pairing_ok is False
    assert bad_result.detail
    assert len(bad_result.payloads) == 4  # every record kept, nothing dropped

    # An odd record count is also a violation, kept unmerged.
    odd_stream = record(b"AAAA") + record(b"AAAA") + record(b"BBBB")
    odd_result = jasper_pipe_probe.reassemble_records(odd_stream)
    assert odd_result.pairing_ok is False
    assert len(odd_result.payloads) == 3
