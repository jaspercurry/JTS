"""Tests for jasper.atomic_io.atomic_write_text — the canonical atomic
text-write helper that ~39 hand-rolled tempfile+chmod+os.replace sites are
being consolidated onto.

Pins the contract callers depend on: a clean write+read round-trip, the
requested mode landing on the published file, parent-dir creation, atomic
overwrite of an existing file, UTF-8 fidelity, and — the property the whole
pattern exists for — that a failed rename leaves no stray temp file behind AND
propagates the error (this helper does NOT swallow; fail-soft is a caller
policy).
"""
from __future__ import annotations

import os

import pytest

from jasper.atomic_io import atomic_write_text


def test_write_read_round_trip(tmp_path):
    path = tmp_path / "state.txt"
    atomic_write_text(path, "hello world")
    assert path.read_text(encoding="utf-8") == "hello world"


def test_mode_is_applied(tmp_path):
    path = tmp_path / "secret.env"
    atomic_write_text(path, "JASPER_X=1\n", mode=0o600)
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_group_from_parent_chowns_temp_before_publish(tmp_path, monkeypatch):
    path = tmp_path / "secret.env"
    calls: list[tuple[str, int, int]] = []

    def fake_chown(target: str, uid: int, gid: int) -> None:
        calls.append((target, uid, gid))

    monkeypatch.setattr(os, "chown", fake_chown)
    atomic_write_text(path, "JASPER_X=1\n", mode=0o640, group_from_parent=True)

    assert path.read_text(encoding="utf-8") == "JASPER_X=1\n"
    assert calls, "group_from_parent must set the temp file group"
    target, uid, gid = calls[-1]
    assert os.path.dirname(target) == str(tmp_path)
    assert os.path.basename(target).startswith(".secret.env.")
    assert uid == -1
    assert gid == os.stat(tmp_path).st_gid


def test_group_from_parent_chown_failure_cleans_up_temp(tmp_path, monkeypatch):
    path = tmp_path / "secret.env"

    def boom(*_args, **_kwargs):
        raise PermissionError("simulated group assignment failure")

    monkeypatch.setattr(os, "chown", boom)

    with pytest.raises(PermissionError, match="simulated group assignment failure"):
        atomic_write_text(path, "doomed", mode=0o640, group_from_parent=True)

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_mode_is_0644(tmp_path):
    path = tmp_path / "plain.env"
    atomic_write_text(path, "x")
    assert (os.stat(path).st_mode & 0o777) == 0o644


def test_parent_dir_created_when_missing(tmp_path):
    path = tmp_path / "nested" / "deeper" / "state.json"
    assert not path.parent.exists()
    atomic_write_text(path, "{}")
    assert path.read_text(encoding="utf-8") == "{}"


def test_overwrites_existing_file_atomically(tmp_path):
    path = tmp_path / "state.txt"
    atomic_write_text(path, "old contents")
    atomic_write_text(path, "new contents")
    assert path.read_text(encoding="utf-8") == "new contents"
    # No stray temp files left from either write.
    assert [p.name for p in tmp_path.iterdir()] == ["state.txt"]


def test_utf8_round_trip(tmp_path):
    path = tmp_path / "unicode.txt"
    value = "café — naïve — 日本語 — 🔊"
    atomic_write_text(path, value)
    assert path.read_text(encoding="utf-8") == value


def test_failure_cleans_up_temp_and_propagates(tmp_path, monkeypatch):
    path = tmp_path / "state.txt"

    def boom(*_args, **_kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_write_text(path, "doomed")

    # The target was never created, and the temp file was unlinked — the
    # directory is left exactly as it was.
    assert list(tmp_path.iterdir()) == []


def test_chmod_failure_also_cleans_up_temp(tmp_path, monkeypatch):
    # A failure at chmod (inside the try, after the write, BEFORE the rename)
    # is a different raise point than os.replace but the same cleanup branch:
    # the temp must still be unlinked and the error propagate.
    path = tmp_path / "state.txt"

    def boom(*_args, **_kwargs):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", boom)

    with pytest.raises(OSError, match="simulated chmod failure"):
        atomic_write_text(path, "doomed")
    assert list(tmp_path.iterdir()) == []


def test_accepts_str_path(tmp_path):
    # The API is str | os.PathLike; the round-trip tests use Path, so pin str.
    p = str(tmp_path / "as_str.txt")
    atomic_write_text(p, "y")
    with open(p, encoding="utf-8") as f:
        assert f.read() == "y"


def test_bare_filename_uses_cwd(tmp_path, monkeypatch):
    # No directory component => os.path.dirname(...) is "" and the `or "."`
    # fallback resolves the tempfile into the cwd. Pins that branch.
    monkeypatch.chdir(tmp_path)
    atomic_write_text("bare.txt", "x")
    assert (tmp_path / "bare.txt").read_text(encoding="utf-8") == "x"
    assert [p.name for p in tmp_path.iterdir()] == ["bare.txt"]
