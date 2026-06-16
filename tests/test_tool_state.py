"""jasper.tool_state — wizard-owned SSOT for tool UI state.

Fail-safe (missing/unreadable/malformed -> nothing disabled for configured
runtime tools), parse, and deterministic round-trip. Mirrors the
mic_mute_persistence posture: fail toward MORE configured functionality.
"""
from __future__ import annotations

import os
import stat

from jasper.tool_state import (
    ToolState,
    read_disabled_packs,
    read_setup_enabled_packs,
    read_disabled_tools,
    read_tool_state,
    write_disabled_packs,
    write_disabled_tools,
    write_tool_state,
)


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
    p.write_text(
        "JASPER_DISABLED_TOOL_PACKS=spotify, google\n"
        "JASPER_ENABLED_SETUP_TOOL_PACKS=home-assistant, gmail\n"
        "JASPER_DISABLED_TOOLS=a,b , c\n"
    )
    assert read_disabled_tools(p) == {"a", "b", "c"}
    assert read_disabled_packs(p) == {"spotify", "google"}
    assert read_setup_enabled_packs(p) == {"home-assistant", "gmail"}


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
    write_tool_state(
        p,
        ToolState(
            disabled_tools=frozenset({"b", "a"}),
            disabled_packs=frozenset({"spotify"}),
            setup_enabled_packs=frozenset({"home-assistant"}),
        ),
    )
    assert read_disabled_tools(p) == {"a", "b"}
    assert read_disabled_packs(p) == {"spotify"}
    assert read_setup_enabled_packs(p) == {"home-assistant"}
    # Deterministic, sorted, comma-joined content.
    assert p.read_text() == (
        "JASPER_DISABLED_TOOL_PACKS=spotify\n"
        "JASPER_ENABLED_SETUP_TOOL_PACKS=home-assistant\n"
        "JASPER_DISABLED_TOOLS=a,b\n"
    )
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


def test_write_disabled_tools_preserves_disabled_packs(tmp_path):
    p = tmp_path / "tool_state.env"
    write_tool_state(
        p,
        ToolState(
            disabled_packs=frozenset({"spotify"}),
            setup_enabled_packs=frozenset({"home-assistant"}),
        ),
    )
    write_disabled_tools(p, {"spotify_play"})
    state = read_tool_state(p)
    assert state.disabled_packs == {"spotify"}
    assert state.disabled_tools == {"spotify_play"}
    assert state.setup_enabled_packs == {"home-assistant"}


def test_write_disabled_packs_preserves_setup_enabled_packs(tmp_path):
    p = tmp_path / "tool_state.env"
    write_tool_state(
        p,
        ToolState(
            disabled_tools=frozenset({"spotify_play"}),
            setup_enabled_packs=frozenset({"home-assistant"}),
        ),
    )
    write_disabled_packs(p, {"spotify"})
    state = read_tool_state(p)
    assert state.disabled_packs == {"spotify"}
    assert state.disabled_tools == {"spotify_play"}
    assert state.setup_enabled_packs == {"home-assistant"}
