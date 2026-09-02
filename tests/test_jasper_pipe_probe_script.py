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


def _record(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def _lo_pair(payload: bytes) -> bytes:
    """One datagram as the AF_PACKET tap on "lo" sees it: twice."""
    return _record(payload) + _record(payload)


# jts3's tap geometry (outputd period_frames=128, S16 stereo). The probe
# learns this from the capture; the test only needs A consistent geometry.
_PERIOD = 512
# The intruder observed on :9891 in the field (issue #3509): another packet's
# IPv4+UDP head, which is not a whole tap period.
_FOREIGN = b"\x45\x00" + bytes(546)
_TONE = bytes(range(256)) * 2
_SILENT = bytes(_PERIOD)


@pytest.mark.parametrize(
    "stream, payloads, pairing_ok, period_bytes, excised_by_length",
    [
        # Well-formed: every record is one tap period, byte-identical pairs.
        pytest.param(
            _lo_pair(_TONE) * 100, 100, True, _PERIOD, {}, id="well_formed",
        ),
        # The field defect: a foreign-LENGTH datagram arrives (twice, like
        # everything on lo) mid-capture. It used to pair perfectly and land in
        # the capture as a full-scale-looking impulse; it is now counted out.
        pytest.param(
            _lo_pair(_TONE) * 50 + _lo_pair(_FOREIGN) + _lo_pair(_TONE) * 50,
            100, True, _PERIOD, {len(_FOREIGN): 2}, id="foreign_length_paired",
        ),
        # The same intruder seen only ONCE. Validating before pairing restores
        # the parity it would otherwise have flipped for every later record --
        # which used to condemn the whole capture as unmerged.
        pytest.param(
            _lo_pair(_TONE) * 50 + _record(_FOREIGN) + _lo_pair(_TONE) * 50,
            100, True, _PERIOD, {len(_FOREIGN): 1}, id="foreign_length_odd",
        ),
        # HONEST LIMIT: foreign content at exactly one period's length is not
        # structurally distinguishable from tap audio, and is accepted. No
        # payload signature is matched -- a blacklist of the one observed
        # intruder would be the anti-pattern this validation replaces.
        pytest.param(
            _lo_pair(_TONE) * 100 + _lo_pair(bytes(reversed(_TONE))),
            101, True, _PERIOD, {}, id="foreign_content_period_length",
        ),
        # Valid but silent: accepted, so a silent tap stays reportable as
        # silence rather than as instrument failure.
        pytest.param(
            _lo_pair(_SILENT) * 100, 100, True, _PERIOD, {}, id="all_zero_valid",
        ),
        # A pair that does not byte-match is still never guessed at.
        pytest.param(
            _lo_pair(_TONE) * 50 + _record(_TONE) + _record(_SILENT),
            102, False, _PERIOD, {}, id="pair_mismatch",
        ),
        # Nothing at all, and no length holding a majority: no geometry can be
        # learned, so nothing is blessed as the tap.
        pytest.param(b"", 0, False, None, {}, id="empty"),
        pytest.param(
            _lo_pair(_TONE) + _lo_pair(_FOREIGN),
            0, False, None, {_PERIOD: 2, len(_FOREIGN): 2}, id="no_majority_geometry",
        ),
    ],
)
def test_capture_records_are_validated_against_the_tap_geometry(
    stream: bytes,
    payloads: int,
    pairing_ok: bool,
    period_bytes: int | None,
    excised_by_length: dict[int, int],
) -> None:
    result = jasper_pipe_probe.reassemble_records(stream)
    assert len(result.payloads) == payloads
    assert result.pairing_ok is pairing_ok
    assert result.period_bytes == period_bytes
    assert result.excised_by_length == excised_by_length
    assert result.excised == sum(excised_by_length.values())
    assert all(len(p) == period_bytes for p in result.payloads)
    assert result.detail


def test_capture_tells_a_blind_instrument_from_a_deaf_tap_from_a_good_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`capture`'s observable contract: a sidecar manifest carrying the
    box-side monotonic anchor and the excised-datagram counts, and three
    distinct exit codes so a null result says WHICH null it was."""
    stderr = "START_MONOTONIC_NS=123456789\nSTART_REALTIME_NS=1756000000000000000\n"
    monkeypatch.setattr(jasper_pipe_probe, "resolve_target", lambda host: ("box", "pi"))
    monkeypatch.setattr(jasper_pipe_probe, "push_sniffer", lambda *a, **k: None)
    monkeypatch.setattr(jasper_pipe_probe, "cleanup_session", lambda *a, **k: None)
    monkeypatch.setattr(
        jasper_pipe_probe, "ssh_run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", stderr),
    )

    def run(stream: bytes) -> tuple[int, dict]:
        out = tmp_path / "cap.raw"
        monkeypatch.setattr(
            jasper_pipe_probe, "fetch_and_reassemble",
            lambda *a, **k: jasper_pipe_probe.reassemble_records(stream),
        )
        code = jasper_pipe_probe.main(["capture", "1", str(out)])
        return code, json.loads(jasper_pipe_probe.capture_header_path(out).read_text())

    # Nothing valid ever arrived: the instrument was blind.
    blind_code, blind = run(b"")
    assert blind_code == jasper_pipe_probe.CAPTURE_NO_VALID_RECORDS_EXIT
    assert blind["records"]["kept"] == 0
    assert blind["tap"]["period_bytes"] is None

    # Valid datagrams arrived carrying only zeros: the chain was deaf.
    deaf_code, deaf = run(_lo_pair(_SILENT) * 10 + _lo_pair(_FOREIGN))
    assert deaf_code == 1
    assert deaf["records"] == {
        "seen": 22, "kept": 20, "excised": 2,
        "excised_by_length": {str(len(_FOREIGN)): 2},
    }
    assert deaf["pcm"]["all_zero"] is True
    assert deaf["pcm"]["frames"] == 10 * _PERIOD // 4
    assert deaf["tap"]["period_bytes"] == _PERIOD
    # The anchor that lets the capture be lined up against the box's journal.
    assert deaf["start_monotonic_ns"] == 123456789
    assert deaf["start_realtime_ns"] == 1756000000000000000

    # Real audio, one intruder excised: succeeds, and still reports the excision.
    good_code, good = run(_lo_pair(_TONE) * 10 + _lo_pair(_FOREIGN))
    assert good_code == 0
    assert good["pcm"]["all_zero"] is False
    assert good["records"]["excised"] == 2
    assert good["clean_timeline"] is False

    assert len({blind_code, deaf_code, good_code}) == 3


def test_an_off_period_record_is_excised_and_counted() -> None:
    """#3509: the tap emits one DAC period per datagram, so a record of any
    other length cannot be concatenated into the stream -- an odd byte count
    swaps the channels of everything after it. Excision runs on the WIRE
    records, ahead of the loopback collapse, so an intruder seen twice on lo
    counts twice."""

    def pair(payload: bytes) -> bytes:
        rec = struct.pack("<I", len(payload)) + payload
        return rec + rec

    period = b"\x01\x02" * 256  # 512 B, the modal length below
    stream = pair(period) * 3 + pair(period[:-1]) + pair(period)
    result = jasper_pipe_probe.reassemble_records(stream)

    assert result.pairing_ok is True
    assert result.period_bytes == 512
    assert result.excised == 2
    assert result.excised_by_length == {511: 2}
    assert len(result.payloads) == 4
    assert all(len(p) == 512 for p in result.payloads)


def test_an_all_zero_capture_is_distinguishable_from_a_quiet_one() -> None:
    assert jasper_pipe_probe.is_all_zero(b"\x00" * 4096) is True
    assert jasper_pipe_probe.is_all_zero(b"") is True
    assert jasper_pipe_probe.is_all_zero(b"\x00" * 4095 + b"\x01") is False


def test_locate_click_sample_finds_prominent_click_over_near_silence() -> None:
    """The tap sits after master volume (commonly ~-15 dB) and the
    crossover, so a healthy click can arrive at only a few hundred LSB out
    of 32768 -- well under a full-scale-relative absolute threshold. A
    ~1%-FS click over a near-silent background must still be located at
    its exact sample."""
    n = RATE  # 1 s
    rng = np.random.default_rng(7)
    background = rng.integers(-3, 4, size=n)  # bounded: max magnitude 3 LSB
    ch0 = background.astype(np.int16).copy()
    ch1 = background.astype(np.int16).copy()
    click_index = 40_000
    click_amplitude = round(0.01 * 32768)  # ~1% FS
    ch0[click_index] = click_amplitude
    ch1[click_index] = click_amplitude

    location = jasper_pipe_probe.locate_click_sample(ch0, ch1)

    assert location.sample_index == click_index


def test_locate_click_sample_reports_not_found_on_low_level_noise() -> None:
    """Below CLICK_DETECT_FLOOR_LSB, nothing is ever a click -- the
    absolute floor exists so pure noise cannot self-trigger no matter how
    prominently it ratios against its own (even quieter) background."""
    n = RATE
    rng = np.random.default_rng(11)
    ch0 = rng.integers(-10, 11, size=n).astype(np.int16)  # bounded: max 10 LSB < floor
    ch1 = rng.integers(-10, 11, size=n).astype(np.int16)

    assert jasper_pipe_probe.locate_click_sample(ch0, ch1).sample_index is None


def test_locate_click_sample_reports_not_found_on_capture_shorter_than_exclusion_window() -> None:
    """A capture shorter than 2xCLICK_RMS_EXCLUSION_MS (e.g. truncated by a
    daemon restart) leaves no background to compare the peak against -- the
    ratio check would be vacuously true for ANY signal, so this must refuse
    rather than fabricate an anchor from a lone loud sample."""
    n = 500  # ~10.4 ms at 48 kHz -- shorter than the +/-20 ms exclusion window
    ch0 = np.zeros(n, dtype=np.int16)
    ch1 = np.zeros(n, dtype=np.int16)
    ch0[250] = 20_000  # a huge, unambiguous "click" -- must still be rejected

    location = jasper_pipe_probe.locate_click_sample(ch0, ch1)

    assert location.sample_index is None


def test_locate_click_sample_rejects_peak_before_min_index() -> None:
    """A device-close pop from the PREVIOUS repeat can land earlier in
    THIS repeat's capture than the click could ever physically arrive (see
    _run_one_latency_repeat's min_index derivation) -- min_index must
    reject it even though it is the loudest, most "prominent" thing in the
    buffer, rather than anchor a negative delta on it."""
    n = RATE  # 1 s
    rng = np.random.default_rng(13)
    background = rng.integers(-3, 4, size=n)
    ch0 = background.astype(np.int16).copy()
    ch1 = background.astype(np.int16).copy()
    early_pop_index = 100  # well before any legitimate min_index bound
    ch0[early_pop_index] = 30_000  # louder than any real click -- must still be rejected
    ch1[early_pop_index] = 30_000

    location = jasper_pipe_probe.locate_click_sample(ch0, ch1, min_index=24_000)

    assert location.sample_index is None


def test_ssh_control_path_stays_short_regardless_of_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """ControlPath must not depend on TMPDIR: macOS's default TMPDIR is a
    deep per-user path, and AF_UNIX sun_path caps at ~104 bytes. Reload the
    module under a deliberately deep TMPDIR and confirm the constructed
    path does not embed TMPDIR, and stays short enough to survive %C's own
    EXPANSION, not just as written: %C is unexpanded here (ssh expands it,
    not us) and replaces its own 2 literal characters with a 40-hex-char
    SHA1 digest at connect time, so expanded_length = len(control_path) +
    38. To keep the EXPANDED path under the ~104-byte sun_path cap, the
    unexpanded string must stay under 104 - 38 = 66."""
    deep_tmpdir = "/private/var/folders/" + "x" * 40 + "/T/" + "y" * 40
    monkeypatch.setenv("TMPDIR", deep_tmpdir)
    monkeypatch.setattr(tempfile, "tempdir", None)  # force TMPDIR to be re-read, not cached

    reloaded = _load_module()

    control_path_opt = next(opt for opt in reloaded._SSH_OPTS if opt.startswith("ControlPath="))
    control_path = control_path_opt.removeprefix("ControlPath=")
    assert len(control_path) < 66
    assert deep_tmpdir not in control_path


@pytest.mark.parametrize(
    ("invoke", "program"),
    [
        pytest.param(lambda m: m.push_sniffer("pi.example", "pi", "sess"), "ssh", id="push_sniffer"),
        pytest.param(lambda m: m.ssh_run("pi.example", "pi", "true", timeout=5), "ssh", id="ssh_run"),
        pytest.param(lambda m: m.ssh_popen("pi.example", "pi", "true"), "ssh", id="ssh_popen"),
        pytest.param(lambda m: m.scp_push("pi.example", "pi", Path("/dev/null"), "/tmp/x"), "scp", id="scp_push"),
        pytest.param(lambda m: m.scp_pull("pi.example", "pi", "/tmp/x", Path("/dev/null")), "scp", id="scp_pull"),
    ],
)
def test_every_ssh_entry_point_creates_control_dir_before_spawning(
    invoke, program: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ControlPath dir is created lazily, so EVERY path that builds an
    ssh/scp argv must create it first: ssh binds its control socket there
    on the first connection, and a missing dir fails the whole run with
    `unix_listener: cannot bind` (exit 255) -- proven live on `capture`,
    whose first ssh is push_sniffer."""
    control_dir = tmp_path / "cm"
    monkeypatch.setattr(jasper_pipe_probe, "_SSH_CONTROL_DIR", control_dir)
    spawns: list[tuple[str, bool]] = []

    def _record(cmd, *_args, **_kwargs):
        spawns.append((cmd[0], control_dir.is_dir()))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(jasper_pipe_probe.subprocess, "run", _record)
    monkeypatch.setattr(jasper_pipe_probe.subprocess, "Popen", _record)

    invoke(jasper_pipe_probe)

    assert spawns == [(program, True)]
