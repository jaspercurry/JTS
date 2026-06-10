"""Tests for jasper.mic_mute_persistence.

Covers:
- read of a missing file returns False (failure mode = unmuted)
- round-trip write/read for both True and False
- atomic write doesn't leave .tmp files behind on success
- malformed / unknown values fall back to False
- write_mic_muted doesn't raise when the directory is unwritable
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jasper.mic_mute_persistence import (
    DEFAULT_PATH,
    read_mic_muted,
    write_mic_muted,
)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "mic_mute.env"


def test_default_path_constant():
    assert DEFAULT_PATH == "/var/lib/jasper/mic_mute.env"


def test_read_missing_returns_false(tmp_path):
    assert read_mic_muted(_path(tmp_path)) is False


def test_round_trip_true(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, True)
    assert read_mic_muted(p) is True


def test_round_trip_false(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, False)
    assert read_mic_muted(p) is False


def test_overwrite_changes_value(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, True)
    assert read_mic_muted(p) is True
    write_mic_muted(p, False)
    assert read_mic_muted(p) is False


def test_written_file_is_env_var_format(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, True)
    assert p.read_text().strip() == "JASPER_MIC_MUTED=1"
    write_mic_muted(p, False)
    assert p.read_text().strip() == "JASPER_MIC_MUTED=0"


def test_written_file_mode_is_world_readable(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, True)
    mode = os.stat(p).st_mode & 0o777
    # 0644 — jasper-doctor and other operator tooling should be able
    # to read the state without being root.
    assert mode == 0o644


def test_no_temp_files_leak_on_success(tmp_path):
    p = _path(tmp_path)
    write_mic_muted(p, True)
    leftover = [
        n for n in os.listdir(tmp_path) if n.startswith(".mic_mute.")
    ]
    assert leftover == []


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_parses_accepted_values(tmp_path, value, expected):
    p = _path(tmp_path)
    p.write_text(f"JASPER_MIC_MUTED={value}\n")
    assert read_mic_muted(p) is expected


def test_unknown_value_falls_back_to_false(tmp_path, caplog):
    p = _path(tmp_path)
    p.write_text("JASPER_MIC_MUTED=maybe\n")
    with caplog.at_level("WARNING"):
        assert read_mic_muted(p) is False
    assert any("unrecognised" in r.message for r in caplog.records)


def test_ignores_other_keys_and_comments(tmp_path):
    p = _path(tmp_path)
    p.write_text(
        "# saved by jasper-voice\n"
        "OTHER_KEY=99\n"
        "JASPER_MIC_MUTED=1\n"
    )
    assert read_mic_muted(p) is True


def test_quoted_value_is_accepted(tmp_path):
    p = _path(tmp_path)
    p.write_text('JASPER_MIC_MUTED="1"\n')
    assert read_mic_muted(p) is True


def test_no_matching_key_returns_false(tmp_path):
    p = _path(tmp_path)
    p.write_text("SOMETHING_ELSE=1\n")
    assert read_mic_muted(p) is False


def test_write_creates_parent_directory(tmp_path):
    p = tmp_path / "nested" / "deeper" / "mic_mute.env"
    write_mic_muted(p, True)
    assert read_mic_muted(p) is True


def test_write_failure_is_logged_not_raised(tmp_path, caplog, monkeypatch):
    # Simulate an OSError from the atomic writer. The write should
    # log a warning rather than propagate — the mute toggle must
    # not crash when /var/lib/jasper is unwritable for whatever
    # reason.
    import jasper.mic_mute_persistence as mod

    def boom(*args, **kwargs):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(mod, "atomic_write_text", boom)
    with caplog.at_level("WARNING"):
        write_mic_muted(_path(tmp_path), True)
    assert any(
        "write to" in r.message and "failed" in r.message
        for r in caplog.records
    )


def test_read_handles_pathlike(tmp_path):
    # Both str and Path should work since the type hint is
    # str | os.PathLike.
    p = _path(tmp_path)
    write_mic_muted(str(p), True)
    assert read_mic_muted(str(p)) is True
    assert read_mic_muted(p) is True
