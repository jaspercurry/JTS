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

from jasper.route_latency import ref9891_pcap


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


# jts3's tap geometry (outputd period_frames=128, S16 stereo). The probe reads
# this from outputd's STATUS; the test supplies it directly.
_PERIOD = 512
# The 548 B pseudo-datagram of #3509 and a torn 511 B record: neither is one
# tap period, and neither is matched by shape -- only by length.
_FOREIGN = b"\x45\x00" + bytes(546)
_TORN = bytes(511)
_TONE = bytes(range(256)) * 2
_OTHER = bytes(reversed(_TONE))
_SILENT = bytes(_PERIOD)


@pytest.mark.parametrize(
    "stream, payloads, pairing_ok, unexpected_by_length",
    [
        # Every record is one tap period, byte-identical pairs.
        pytest.param(_lo_pair(_TONE) * 100, 100, True, {}, id="well_formed"),
        # A foreign-LENGTH datagram arrives twice, like everything on lo, so it
        # pairs perfectly -- length is what rejects it.
        pytest.param(
            _lo_pair(_TONE) * 50 + _lo_pair(_FOREIGN) + _lo_pair(_TONE) * 50,
            100, True, {len(_FOREIGN): 2}, id="foreign_length_paired",
        ),
        # The same record seen ONCE. Excising before pairing restores the
        # parity it would otherwise flip for every later record.
        pytest.param(
            _lo_pair(_TONE) * 50 + _record(_FOREIGN) + _lo_pair(_TONE) * 50,
            100, True, {len(_FOREIGN): 1}, id="foreign_length_odd",
        ),
        # A torn record is off-period too, and goes the same way.
        pytest.param(
            _lo_pair(_TONE) * 3 + _lo_pair(_TORN) + _lo_pair(_TONE),
            4, True, {len(_TORN): 2}, id="torn_record",
        ),
        # Content at exactly one period's length is accepted: the tap's own
        # bytes are not distinguishable from anything else that size.
        pytest.param(
            _lo_pair(_TONE) * 100 + _lo_pair(_OTHER),
            101, True, {}, id="period_length_content_accepted",
        ),
        # Valid but silent: kept, so a silent tap stays reportable as silence.
        pytest.param(_lo_pair(_SILENT) * 100, 100, True, {}, id="all_zero_valid"),
        # A pair that does not byte-match is never guessed at.
        pytest.param(
            _lo_pair(_TONE) * 50 + _record(_TONE) + _record(_SILENT),
            102, False, {}, id="pair_mismatch",
        ),
        # A capture cut mid-pair: the sniffer's deadline landed between one
        # datagram's two copies on lo (seen on a 30 s jts3 run). The unpaired
        # tail is dropped and the rest still has to pair -- condemning the
        # whole capture would report a 30 s window as 60 s of duplicates.
        pytest.param(
            _lo_pair(_TONE) * 2 + _record(_TONE), 2, True, {}, id="truncated_tail",
        ),
        # A loss anywhere but the end is still a violation: dropping the tail
        # cannot repair it, so nothing is collapsed and `latency` is refused.
        pytest.param(
            _lo_pair(_TONE) + _record(_SILENT) + _lo_pair(_TONE),
            4, False, {}, id="odd_loss_mid_stream",
        ),
        # Excision AND a pairing violation together: the surviving records are
        # returned unmerged, but the foreign one is still not among them.
        pytest.param(
            _lo_pair(_TONE) * 2 + _lo_pair(_FOREIGN) + _record(_TONE) + _record(_SILENT),
            6, False, {len(_FOREIGN): 2}, id="excision_with_pair_mismatch",
        ),
        pytest.param(b"", 0, True, {}, id="empty"),
    ],
)
def test_capture_records_are_excised_against_the_tap_geometry(
    stream: bytes,
    payloads: int,
    pairing_ok: bool,
    unexpected_by_length: dict[int, int],
) -> None:
    result = jasper_pipe_probe.reassemble_records(stream, _PERIOD)
    assert len(result.payloads) == payloads
    # Only pairing can break the timeline: outputd publishes one period every
    # period, so an excised foreign record was never in it -- an intruded but
    # correctly-paired capture is still usable.
    assert result.pairing_ok is pairing_ok
    assert result.period_bytes == _PERIOD
    assert result.unexpected_by_length == unexpected_by_length
    assert result.excised == sum(unexpected_by_length.values())
    # Nothing off-period ever reaches OUT.raw, however the pairing went.
    assert all(len(p) == _PERIOD for p in result.payloads)
    assert result.detail


def _frame(payload: bytes, *, proto: int = 17, dport: int = 9891, frag: int = 0) -> bytes:
    """A loopback Ethernet frame carrying `payload` to `dport`."""
    udp_len = len(payload) + 8
    ip = (
        bytes([0x45, 0x00])
        + struct.pack(">H", 20 + 8 + len(payload))
        + b"\x00\x00"
        + struct.pack(">H", frag)
        + bytes([64, proto])
        + b"\x00\x00"
        + bytes([127, 0, 0, 1]) * 2
    )
    udp = struct.pack(">HHH", 40000, dport, udp_len) + b"\x00\x00"
    return bytes(12) + b"\x08\x00" + ip + udp + payload


def test_the_sniffers_jasper_imports_resolve_with_only_the_stdlib() -> None:
    """The sniffer runs under the box's system python3 -- no venv, nothing
    but PYTHONPATH=/opt/jasper -- so every module it imports has to be
    stdlib-only. `-I -S` drops PYTHONPATH, the user site AND site-packages,
    leaving only the stdlib plus the repo root inserted here, so a jasper
    module that grows a third-party dependency fails here rather than on the
    box, mid-capture, as a PERIOD_BYTES_ERROR nobody can read."""
    imports = [
        line for line in jasper_pipe_probe.SNIFFER_SOURCE.splitlines()
        if line.startswith("from jasper")
    ]
    assert imports, "the sniffer no longer imports the shared modules"
    proc = subprocess.run(
        [
            sys.executable, "-I", "-S", "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + "\n".join(imports),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_a_box_on_another_commit_is_refused_before_the_sniffer_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sniffer imports its parse from the BOX's /opt/jasper, so an
    install on another commit can read :9891 by another rule than this side
    reassembles by. That is refused under its own exit code -- not
    argparse's 2, not the blind-instrument 3 -- and nothing is written."""
    monkeypatch.setattr(jasper_pipe_probe, "resolve_target", lambda host: ("box", "pi"))
    monkeypatch.setattr(
        jasper_pipe_probe, "ssh_run",
        # CRLF: interactive-sudo deploys read this marker through `ssh -tt`,
        # which rewrites the line endings.
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "JASPER_GIT_SHA_FULL=deadbeef\r\nJASPER_INSTALL_STATUS=ok\r\n", ""
        ),
    )
    out = tmp_path / "cap.raw"

    with pytest.raises(SystemExit) as refusal:
        jasper_pipe_probe.main(["capture", "1", str(out)])

    assert refusal.value.code == jasper_pipe_probe.BUILD_SKEW_EXIT
    assert refusal.value.code not in (0, 1, 2, jasper_pipe_probe.CAPTURE_INSTRUMENT_BLIND_EXIT)
    assert not out.exists()
    # --allow-skew is the documented override: same mismatch, no refusal.
    jasper_pipe_probe.require_matching_build("box", "pi", allow_skew=True)


def test_the_sniffer_streams_payloads_while_tallying_the_rest() -> None:
    """#3509 item 1's evidence claim: the manifest's rejected_by_header is
    produced by the sniffer running over real frames, not asserted anywhere.
    Drives the shipped source's own loop -- so this also pins that the string
    still compiles and its imports resolve. What each key SAYS belongs to
    ref9891_pcap.classify_frame and is pinned there; what is pinned here is
    that payloads stream out one by one while rejects accumulate per key,
    and that traffic which never claimed the port does neither."""
    sniffer: dict = {}
    exec(compile(jasper_pipe_probe.SNIFFER_SOURCE, "sniffer", "exec"), sniffer)
    good = bytes(_PERIOD)
    frames = [
        _frame(good),                                  # the tap: stored
        _frame(bytes(534), proto=1),                   # ICMP quoting the port
        _frame(bytes(534), proto=1),                   # ...twice
        _frame(good, frag=0x0100),                     # a fragment
        _frame(good, dport=9888),                      # another port entirely
    ]

    tally: dict = {}
    stored = list(sniffer["tap_payloads"](iter(frames), 9891, tally))

    assert stored == [good]
    # Two distinct reject kinds, one of them seen twice; the :9888 mic stream
    # is not this port's business and must not be counted as reaching it.
    assert sorted(tally.values()) == [1, 2]


def test_capture_reports_what_it_saw_and_fails_only_when_it_saw_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`capture`'s observable contract through the real CLI entry point: the
    manifest carries the box anchor, the geometry and both reject tallies;
    a blind instrument exits nonzero; an all-zero tap does not."""
    monkeypatch.setattr(jasper_pipe_probe, "resolve_target", lambda host: ("box", "pi"))
    monkeypatch.setattr(jasper_pipe_probe, "push_sniffer", lambda *a, **k: None)
    monkeypatch.setattr(jasper_pipe_probe, "cleanup_session", lambda *a, **k: None)

    def run(stream: bytes, *, stderr: str) -> tuple[int, dict]:
        out = tmp_path / "cap.raw"
        monkeypatch.setattr(
            jasper_pipe_probe, "ssh_run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "", stderr),
        )
        monkeypatch.setattr(
            jasper_pipe_probe, "fetch_and_reassemble",
            lambda h, u, p, period: jasper_pipe_probe.reassemble_records(stream, period),
        )
        # --allow-skew: the stubbed ssh_run above answers every remote command
        # with the capture's stderr, so the build marker reads as unreadable.
        # This case is about what the manifest reports, not the skew guard.
        code = jasper_pipe_probe.main(["capture", "1", str(out), "--allow-skew"])
        manifest_path = jasper_pipe_probe.capture_manifest_path(out)
        return code, json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    reported = (
        f"PERIOD_BYTES={_PERIOD}\nSTART_MONOTONIC_NS=123456789\n"
        "START_REALTIME_NS=1756000000000000000\n"
        'REJECTED_BY_HEADER={"proto=1,frag=0,wire_len=576": 2}\n'
    )

    # Valid audio, one intruder excised, one non-datagram counted: succeeds,
    # and the manifest says all of it.
    good_code, good = run(_lo_pair(_TONE) * 10 + _lo_pair(_FOREIGN), stderr=reported)
    assert good_code == 0
    assert good["schema_version"] == 1
    assert good["kind"] == ref9891_pcap.CAPTURE_MANIFEST_KIND
    assert good["start_monotonic_ns"] == 123456789
    assert good["start_realtime_ns"] == 1756000000000000000
    assert good["tap"]["period_bytes"] == _PERIOD
    assert good["records"] == {
        "seen": 22, "stored": 10, "unexpected_by_length": {str(len(_FOREIGN)): 2},
    }
    assert good["wire"]["rejected_by_header"] == {"proto=1,frag=0,wire_len=576": 2}
    assert good["pcm"]["all_zero"] is False
    # pcm describes what was WRITTEN: 10 stored records, not the 22 seen.
    assert good["pcm"]["bytes"] == 10 * _PERIOD
    assert good["pcm"]["frames"] == 10 * _PERIOD // 4
    assert good["pcm"]["duration_s"] == (10 * _PERIOD // 4) / 48000
    # 10 periods is far short of the 1 s asked for: audio that stops
    # mid-window still writes a well-formed file, so the durations are
    # compared rather than assumed to match.
    assert good["pcm"]["short_of_requested"] is True

    # An all-zero tap is REPORTED, not failed: an idle box is validly silent.
    deaf_code, deaf = run(_lo_pair(_SILENT) * 10, stderr=reported)
    assert deaf_code == 0
    assert deaf["pcm"]["all_zero"] is True

    assert good["refusal"] is None
    assert deaf["refusal"] == ref9891_pcap.REFUSAL_ALL_ZERO

    # Nothing valid arrived: the instrument was blind.
    blind_code, blind = run(b"", stderr=reported)
    assert blind_code == jasper_pipe_probe.CAPTURE_INSTRUMENT_BLIND_EXIT
    assert blind["records"]["stored"] == 0
    assert blind["refusal"] == ref9891_pcap.REFUSAL_NO_PACKETS

    # outputd could not say what a tap datagram is. Also blind, but for a
    # different reason -- and the manifest must name WHICH, or the two are
    # indistinguishable to anything reading back the capture.
    no_geom_code, no_geom = run(_lo_pair(_TONE), stderr="PERIOD_BYTES_ERROR=nope\n")
    assert no_geom_code == jasper_pipe_probe.CAPTURE_INSTRUMENT_BLIND_EXIT
    assert no_geom["refusal"] == ref9891_pcap.REFUSAL_NO_GEOMETRY
    assert no_geom["tap"]["period_bytes"] is None
    # And it judges nothing: records it had no geometry to measure are not
    # reported as rejects, which would read as evidence about the wire.
    assert no_geom["records"] == {"seen": 0, "stored": 0, "unexpected_by_length": {}}
    # argparse owns exit 2, so a wrapper can attribute this code unambiguously.
    assert no_geom_code not in (0, 1, 2)


def test_analyze_reports_capture_provenance_from_the_sidecar(tmp_path: Path) -> None:
    """A saved capture keeps its honesty signals: without this, re-analyzing
    a .raw silently loses what was excised and whether it was all zeros."""
    raw = tmp_path / "cap.raw"
    _write_stereo_raw(raw, np.zeros(RATE))
    assert _analyze_json(raw)["capture"] is None

    # A sidecar that is not one of OUR manifests is not provenance for this
    # capture -- reading its fields would attribute another tool's numbers.
    jasper_pipe_probe.capture_manifest_path(raw).write_text(json.dumps({
        "kind": "some_other_tool", "tap": {"period_bytes": 4096},
    }))
    assert _analyze_json(raw)["capture"] is None

    jasper_pipe_probe.capture_manifest_path(raw).write_text(json.dumps({
        "kind": ref9891_pcap.CAPTURE_MANIFEST_KIND,
        "start_monotonic_ns": 42,
        "tap": {"period_bytes": _PERIOD},
        "records": {"unexpected_by_length": {"548": 2}},
        "wire": {"rejected_by_header": {"proto=1,frag=0,wire_len=576": 2}},
        "pcm": {"all_zero": True},
    }))
    provenance = _analyze_json(raw)["capture"]
    assert provenance["period_bytes"] == _PERIOD
    assert provenance["unexpected_by_length"] == {"548": 2}
    assert provenance["all_zero"] is True
    assert provenance["start_monotonic_ns"] == 42


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

    def _spawn(cmd, *_args, **_kwargs):
        spawns.append((cmd[0], control_dir.is_dir()))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(jasper_pipe_probe.subprocess, "run", _spawn)
    monkeypatch.setattr(jasper_pipe_probe.subprocess, "Popen", _spawn)

    invoke(jasper_pipe_probe)

    assert spawns == [(program, True)]
