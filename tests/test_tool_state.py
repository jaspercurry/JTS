"""jasper.tool_state — wizard-owned SSOT for the DISABLED-tools set.

Fail-safe (missing/unreadable/malformed -> nothing disabled), parse,
and deterministic round-trip. Mirrors the mic_mute_persistence posture:
fail toward MORE functionality.
"""
from __future__ import annotations

import os
import stat

from jasper.tool_state import read_disabled_tools, write_disabled_tools


def test_missing_file_is_none_disabled(tmp_path):
    assert read_disabled_tools(tmp_path / "absent.env") == frozenset()


def test_malformed_files_are_none_disabled(tmp_path, caplog):
    # No `=`, wrong key, pure garbage — all resolve to nothing disabled.
    for body in ("garbage with no equals\n", "SOME_OTHER_KEY=a,b\n", "######\n", ""):
        p = tmp_path / "tool_state.env"
        p.write_text(body)
        assert read_disabled_tools(p) == frozenset()


def test_unreadable_path_is_none_disabled_and_warns(tmp_path, caplog):
    # A directory at the path => OSError (IsADirectoryError) on read_text.
    d = tmp_path / "as_dir.env"
    d.mkdir()
    with caplog.at_level("WARNING"):
        assert read_disabled_tools(d) == frozenset()
    assert any("tool_state" in r.message for r in caplog.records)


def test_non_utf8_file_is_none_disabled_and_warns(tmp_path, caplog):
    # A non-UTF-8/corrupt file (the FS-corruption class the fail-safe
    # exists for) must resolve to nothing disabled, not crash startup.
    # UnicodeDecodeError is NOT an OSError, so it needs its own guard.
    p = tmp_path / "tool_state.env"
    p.write_bytes(b"JASPER_DISABLED_TOOLS=\xff\xfe\x80bad\n")
    with caplog.at_level("WARNING"):
        assert read_disabled_tools(p) == frozenset()
    assert any("tool_state" in r.message for r in caplog.records)


def test_parse_trims_whitespace_and_drops_empties(tmp_path):
    p = tmp_path / "tool_state.env"
    p.write_text("JASPER_DISABLED_TOOLS=a,b , c\n")
    assert read_disabled_tools(p) == {"a", "b", "c"}


def test_parse_handles_quotes_comments_blanks(tmp_path):
    p = tmp_path / "tool_state.env"
    p.write_text(
        "# a comment\n"
        "\n"
        'JASPER_DISABLED_TOOLS="spotify_play, get_weather"\n'
    )
    assert read_disabled_tools(p) == {"spotify_play", "get_weather"}


def test_round_trip_sorted_deterministic_and_mode_0644(tmp_path):
    p = tmp_path / "tool_state.env"
    write_disabled_tools(p, {"b", "a"})
    assert read_disabled_tools(p) == {"a", "b"}
    # Deterministic, sorted, comma-joined content.
    assert p.read_text() == "JASPER_DISABLED_TOOLS=a,b\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644


def test_write_empty_set_reads_back_empty(tmp_path):
    p = tmp_path / "tool_state.env"
    write_disabled_tools(p, set())
    assert p.read_text() == "JASPER_DISABLED_TOOLS=\n"
    assert read_disabled_tools(p) == frozenset()


def test_write_accepts_list_and_dedups(tmp_path):
    p = tmp_path / "tool_state.env"
    write_disabled_tools(p, ["x", "x", " y ", ""])
    assert read_disabled_tools(p) == {"x", "y"}
    assert p.read_text() == "JASPER_DISABLED_TOOLS=x,y\n"
