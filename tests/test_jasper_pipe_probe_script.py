# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for scripts/jasper-pipe-probe.

Most cases go through the real CLI (subprocess + `analyze --json`), not an
importlib direct-load: the script has no .py suffix, and
importlib.util.spec_from_file_location returns None for an unrecognized
suffix without an explicit loader. A few cases (record pairing/reassembly,
click-sample location, SSH ControlPath construction) have no CLI surface
at all -- they are exercised via a direct module load instead, see
_load_module().
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
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


def test_dual_tone_row_reads_low_thd_n_and_null_glitches(tmp_path: Path) -> None:
    """A 10 kHz-only reference misreads the dual-tone section's 9 kHz
    partner as noise (proven: it read 0.00 dB in review); section-aware
    thd_n_db fixes it -- this pins the fix through the real analyze path.
    phase_glitches is null there too (proven: ~2000/s on clean input --
    Hilbert instantaneous frequency fires by construction at a two-tone
    envelope's nulls, the same false-failure shape as the toggle section)."""
    raw = tmp_path / "probe.raw"
    _write_stereo_raw(raw, _probe_shaped_buffer())

    readings = _analyze_json(raw)["channels"]["ch0_woofer"]["readings"]
    dual_tone_rows = [r for r in readings if r["section"] == jasper_pipe_probe.DUAL_TONE_SECTION_LABEL]

    assert dual_tone_rows
    for row in dual_tone_rows:
        assert row["thd_n_db"] is not None
        assert row["thd_n_db"] < -60.0
        assert row["phase_glitches"] is None
        assert row["reason"]


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


def test_locate_click_sample_finds_prominent_click_over_near_silence() -> None:
    """The tap sits after master volume (commonly ~-15 dB) and the
    crossover, so a healthy click can arrive at only a few hundred LSB out
    of 32768 (hit: a click peaking ~700 LSB read as "not found" against a
    fixed 0.2-of-full-scale absolute threshold, 5/5 repeats, on a healthy
    chain). A ~1%-FS click over a near-silent background must still be
    located at its exact sample."""
    n = RATE  # 1 s
    rng = np.random.default_rng(7)
    background = rng.integers(-3, 4, size=n)  # bounded: max magnitude 3 LSB
    ch0 = background.astype(np.int16).copy()
    ch1 = background.astype(np.int16).copy()
    click_index = 40_000
    click_amplitude = round(0.01 * 32768)  # ~1% FS
    ch0[click_index] = click_amplitude
    ch1[click_index] = click_amplitude

    found = jasper_pipe_probe.locate_click_sample(ch0, ch1)

    assert found == click_index


def test_locate_click_sample_reports_not_found_on_low_level_noise() -> None:
    """Below CLICK_DETECT_FLOOR_LSB, nothing is ever a click -- the
    absolute floor exists so pure noise cannot self-trigger no matter how
    prominently it ratios against its own (even quieter) background."""
    n = RATE
    rng = np.random.default_rng(11)
    ch0 = rng.integers(-10, 11, size=n).astype(np.int16)  # bounded: max 10 LSB < floor
    ch1 = rng.integers(-10, 11, size=n).astype(np.int16)

    assert jasper_pipe_probe.locate_click_sample(ch0, ch1) is None


def test_ssh_control_path_stays_short_regardless_of_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """ControlPath must not depend on TMPDIR: macOS's default TMPDIR is a
    deep per-user path, and AF_UNIX sun_path caps at ~104 bytes -- a real
    run hit 'unix_listener: path ... too long for Unix domain socket'
    building ControlPath under tempfile.gettempdir(). Reload the module
    under a deliberately deep TMPDIR and confirm the constructed path
    (with ssh's own unexpanded %C token) stays well under the sun_path
    budget and does not embed TMPDIR at all."""
    deep_tmpdir = "/private/var/folders/" + "x" * 40 + "/T/" + "y" * 40
    monkeypatch.setenv("TMPDIR", deep_tmpdir)
    monkeypatch.setattr(tempfile, "tempdir", None)  # force TMPDIR to be re-read, not cached

    reloaded = _load_module()

    control_path_opt = next(opt for opt in reloaded.SSH_OPTS if opt.startswith("ControlPath="))
    control_path = control_path_opt.removeprefix("ControlPath=")
    assert len(control_path) < 90
    assert deep_tmpdir not in control_path
