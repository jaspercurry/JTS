"""Tests for jasper.mux_mode_persistence — the durable source-selection
mode file that lets a household's manual source pin survive jasper-mux's
Restart=always deploy/restart cycle.

The contract is fail-open to Auto (None): any missing, unreadable,
malformed, or unknown-source file resolves to "no pin" so mux degrades
to latest-source-wins rather than getting stuck on a bogus source.
"""
from __future__ import annotations

import json

import pytest

from jasper.mux_mode_persistence import read_manual_source, write_mode
from jasper.music_sources import Source


def test_missing_file_reads_as_auto(tmp_path):
    assert read_manual_source(tmp_path / "nope.json") is None


def test_write_manual_then_read_roundtrips(tmp_path):
    path = tmp_path / "mux_mode.json"
    write_mode(path, Source.AIRPLAY)
    assert read_manual_source(path) is Source.AIRPLAY
    # The on-disk shape is the documented {mode, selected_source} object.
    data = json.loads(path.read_text())
    assert data == {"mode": "manual", "selected_source": "airplay"}


def test_write_auto_then_read_is_none(tmp_path):
    path = tmp_path / "mux_mode.json"
    write_mode(path, Source.SPOTIFY)  # first pin manual
    assert read_manual_source(path) is Source.SPOTIFY
    write_mode(path, None)  # back to auto
    assert read_manual_source(path) is None
    assert json.loads(path.read_text()) == {"mode": "auto"}


def test_corrupt_json_reads_as_auto(tmp_path):
    path = tmp_path / "mux_mode.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_manual_source(path) is None


def test_non_object_json_reads_as_auto(tmp_path):
    path = tmp_path / "mux_mode.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_manual_source(path) is None


def test_manual_without_source_reads_as_auto(tmp_path):
    path = tmp_path / "mux_mode.json"
    path.write_text(json.dumps({"mode": "manual"}), encoding="utf-8")
    assert read_manual_source(path) is None


def test_unknown_source_label_reads_as_auto(tmp_path):
    path = tmp_path / "mux_mode.json"
    path.write_text(
        json.dumps({"mode": "manual", "selected_source": "bogus"}),
        encoding="utf-8",
    )
    assert read_manual_source(path) is None


def test_non_selectable_source_reads_as_auto(tmp_path):
    """IDLE is a valid Source enum value but not a selectable music
    source — it must not be restorable as a manual pin."""
    path = tmp_path / "mux_mode.json"
    path.write_text(
        json.dumps({"mode": "manual", "selected_source": Source.IDLE.value}),
        encoding="utf-8",
    )
    assert read_manual_source(path) is None


@pytest.mark.parametrize("source", [Source.AIRPLAY, Source.SPOTIFY, Source.BLUETOOTH, Source.USBSINK])
def test_all_selectable_sources_roundtrip(tmp_path, source):
    path = tmp_path / "mux_mode.json"
    write_mode(path, source)
    assert read_manual_source(path) is source


def test_write_is_atomic_no_partial_on_existing(tmp_path):
    """A successful write leaves exactly the target file (no stray
    .tmp), confirming the atomic tmp+rename path completed."""
    path = tmp_path / "mux_mode.json"
    write_mode(path, Source.AIRPLAY)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "mux_mode.json"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_write_failure_does_not_raise(tmp_path):
    """Best-effort write: an unwritable target logs but does not raise,
    so a source switch never crashes on a persistence failure."""
    # Point at a path whose parent is a file, not a directory — mkdir
    # of the parent fails with NotADirectoryError (an OSError subclass).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    path = blocker / "mux_mode.json"
    # Must not raise.
    write_mode(path, Source.AIRPLAY)
