# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins :mod:`jasper.audio_measurement.capture_integrity`'s clean-run contract.

Every defect class the campaign's own
``captures/linearization-night-2026-08-19/tools/integrity_summary.py`` script
detected gets one test here confirming this product port still detects it and
names it. The CLI-level tests pin the exit-code contract three fixture trees
deep: clean, dirty, unreadable.
"""

from __future__ import annotations

import json
from pathlib import Path

from jasper.audio_measurement.capture_integrity import (
    EXIT_CLEAN,
    EXIT_DIRTY,
    EXIT_UNREADABLE,
    check_sidecar,
    main,
)
from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAP_FRAMES,
    REPORT_KEY_RENDER_GAPS,
)
from jasper.audio_measurement.wired_capture import WIRED_CAPTURE_CHAIN


def _clean_capture_integrity(**overrides: object) -> dict:
    doc = {
        REPORT_KEY_FRAMES: 48000,
        REPORT_KEY_ENCODED_FRAMES: 48000,
        REPORT_KEY_RENDER_GAPS: 0,
        REPORT_KEY_RENDER_GAP_FRAMES: 0,
        "zero_run_count": 0,
        "capture_chain": WIRED_CAPTURE_CHAIN,
    }
    doc.update(overrides)
    return doc


def _clean_frame_ledger(**overrides: object) -> dict:
    doc = {
        "received_frames": 48000,
        "declared_frames": 48000,
        "encoded_frames": 48000,
        "render_gaps": 0,
        "render_gap_frames": 0,
        "lost_at": [],
    }
    doc.update(overrides)
    return doc


def _sidecar(*, capture_integrity: dict | None = None, frame_ledger: dict | None = None) -> dict:
    return {
        "phase": "verify",
        "capture_integrity": _clean_capture_integrity() if capture_integrity is None else capture_integrity,
        "frame_ledger": _clean_frame_ledger() if frame_ledger is None else frame_ledger,
    }


# --------------------------------------------------------------------------- #
# check_sidecar — pure function, one test per defect class
# --------------------------------------------------------------------------- #


def test_clean_sidecar_has_no_findings():
    assert check_sidecar(_sidecar()) == []


def test_missing_capture_integrity_and_frame_ledger_blocks_report_absence():
    findings = {f.code for f in check_sidecar({"phase": "verify"})}
    assert "counters_missing" in findings
    assert "frame_ledger_missing" in findings


def test_frame_mismatch_detected():
    doc = _sidecar(capture_integrity=_clean_capture_integrity(**{
        REPORT_KEY_FRAMES: 48000, REPORT_KEY_ENCODED_FRAMES: 47999,
    }))
    codes = {f.code for f in check_sidecar(doc)}
    assert "frame_mismatch" in codes


def test_block_gaps_unreported_detected():
    ci = _clean_capture_integrity()
    del ci[REPORT_KEY_RENDER_GAPS]
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert f"{REPORT_KEY_RENDER_GAPS}_unreported" in codes


def test_block_gaps_nonzero_detected():
    ci = _clean_capture_integrity(**{REPORT_KEY_RENDER_GAPS: 1})
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert f"{REPORT_KEY_RENDER_GAPS}_nonzero" in codes


def test_block_gap_frames_nonzero_detected():
    ci = _clean_capture_integrity(**{REPORT_KEY_RENDER_GAP_FRAMES: 512})
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert f"{REPORT_KEY_RENDER_GAP_FRAMES}_nonzero" in codes


def test_zero_run_count_nonzero_detected():
    ci = _clean_capture_integrity(zero_run_count=2)
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert "zero_run_count_nonzero" in codes


def test_truncated_detected():
    ci = _clean_capture_integrity(truncated=True)
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert "truncated" in codes


def test_wrong_capture_chain_detected():
    ci = _clean_capture_integrity(capture_chain="browser_relay")
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert "wrong_capture_chain" in codes


def test_missing_capture_chain_is_wrong_chain():
    # A browser-relay capture never stamps capture_chain at all — this
    # checker only vouches for the wired chain, so absence is a mismatch too.
    ci = _clean_capture_integrity()
    del ci["capture_chain"]
    codes = {f.code for f in check_sidecar(_sidecar(capture_integrity=ci))}
    assert "wrong_capture_chain" in codes


def test_frame_ledger_mismatch_detected():
    fl = _clean_frame_ledger(encoded_frames=47990)
    codes = {f.code for f in check_sidecar(_sidecar(frame_ledger=fl))}
    assert "frame_ledger_mismatch" in codes


def test_frame_ledger_render_gaps_nonzero_detected():
    fl = _clean_frame_ledger(render_gaps=1)
    codes = {f.code for f in check_sidecar(_sidecar(frame_ledger=fl))}
    assert "frame_ledger_render_gaps_nonzero" in codes


def test_frame_ledger_render_gap_frames_nonzero_detected():
    fl = _clean_frame_ledger(render_gap_frames=64)
    codes = {f.code for f in check_sidecar(_sidecar(frame_ledger=fl))}
    assert "frame_ledger_render_gap_frames_nonzero" in codes


def test_frame_ledger_lost_at_detected():
    fl = _clean_frame_ledger(lost_at=["render_graph"])
    codes = {f.code for f in check_sidecar(_sidecar(frame_ledger=fl))}
    assert "frame_ledger_lost_at" in codes


# --------------------------------------------------------------------------- #
# main() — the exit-code contract, over real files
# --------------------------------------------------------------------------- #


def _write_sidecar(directory: Path, name: str, doc: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(doc))


def test_main_exits_clean_when_every_sidecar_is_clean(tmp_path, capsys):
    d = tmp_path / "sidecar"
    _write_sidecar(d, "a.json", _sidecar())
    _write_sidecar(d, "b.json", _sidecar())

    assert main([str(d)]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "VERDICT: CLEAN" in out
    assert "2 capture(s) checked; 2 clean, 0 dirty" in out


def test_main_exits_dirty_and_names_every_finding(tmp_path, capsys):
    d = tmp_path / "sidecar"
    _write_sidecar(d, "a.json", _sidecar())
    ci = _clean_capture_integrity(**{REPORT_KEY_RENDER_GAPS: 1})
    _write_sidecar(d, "b.json", _sidecar(capture_integrity=ci))

    assert main([str(d)]) == EXIT_DIRTY
    out = capsys.readouterr().out
    assert "CLEAN sidecar=a.json" in out
    assert f"DIRTY sidecar=b.json finding={REPORT_KEY_RENDER_GAPS}_nonzero" in out
    assert "VERDICT: DIRTY" in out


def test_main_treats_unparseable_json_as_dirty(tmp_path, capsys):
    d = tmp_path / "sidecar"
    d.mkdir(parents=True)
    (d / "corrupt.json").write_text("{not valid json")

    assert main([str(d)]) == EXIT_DIRTY
    out = capsys.readouterr().out
    assert "DIRTY sidecar=corrupt.json finding=sidecar_unreadable" in out


def test_main_exits_unreadable_when_directory_is_absent(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    assert main([str(missing)]) == EXIT_UNREADABLE
    assert "UNREADABLE" in capsys.readouterr().out


def test_main_exits_unreadable_when_directory_has_no_sidecars(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty)]) == EXIT_UNREADABLE
    assert "UNREADABLE" in capsys.readouterr().out


def test_main_exits_unreadable_with_no_arguments(capsys):
    assert main([]) == EXIT_UNREADABLE


def test_main_checks_multiple_directories(tmp_path, capsys):
    d1 = tmp_path / "attempt-01"
    d2 = tmp_path / "attempt-02"
    _write_sidecar(d1, "a.json", _sidecar())
    _write_sidecar(d2, "b.json", _sidecar(capture_integrity=_clean_capture_integrity(truncated=True)))

    assert main([str(d1), str(d2)]) == EXIT_DIRTY
    out = capsys.readouterr().out
    assert "CLEAN sidecar=a.json" in out
    assert "DIRTY sidecar=b.json finding=truncated" in out
    assert "2 capture(s) checked; 1 clean, 1 dirty" in out
