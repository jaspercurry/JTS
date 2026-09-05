# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import errno
import os
import stat

import pytest

from jasper import atomic_io as atomic_io_module
from jasper.atomic_io import (
    advisory_file_lock,
    atomic_write_json,
    atomic_write_text,
    fsync_directory,
)


def test_write_read_round_trip(tmp_path):
    path = tmp_path / "state.txt"
    atomic_write_text(path, "hello world")
    assert path.read_text(encoding="utf-8") == "hello world"


def test_mode_is_applied(tmp_path):
    path = tmp_path / "secret.env"
    atomic_write_text(path, "JASPER_X=1\n", mode=0o600)
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_atomic_write_json_uses_canonical_encoding_and_policy(tmp_path):
    path = tmp_path / "state.json"

    atomic_write_json(
        path,
        {"z": 1, "a": {"ready": True}},
        mode=0o640,
        group_from_parent=True,
    )

    assert path.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "ready": true\n  },\n  "z": 1\n}\n'
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_durable_write_fsyncs_file_and_parent(tmp_path, monkeypatch):
    path = tmp_path / "boot-config.txt"
    calls: list[str] = []
    real_chmod = os.chmod
    real_replace = os.replace

    def recording_chmod(target, mode):
        calls.append("chmod")
        real_chmod(target, mode)

    def recording_replace(source, target):
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "fsync", lambda _fd: calls.append("fsync"))

    atomic_write_text(path, "safe\n", durable=True)

    assert path.read_text(encoding="utf-8") == "safe\n"
    assert calls == ["chmod", "fsync", "replace", "fsync"]


def test_post_publish_directory_fsync_failure_does_not_claim_cleanup_failed(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "boot-config.txt"
    fsync_calls = 0

    def fail_directory_fsync(_fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError(errno.EIO, "simulated directory fsync failure")

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError):
        atomic_write_text(path, "published\n", durable=True)

    assert path.read_text(encoding="utf-8") == "published\n"
    assert list(tmp_path.iterdir()) == [path]
    assert "event=atomic_io.temp_cleanup_failed" not in caplog.text


@pytest.mark.parametrize(
    ("code", "tolerated"),
    [
        (None, True),
        (errno.EINVAL, True),
        (errno.ENOTSUP, True),
        (errno.EOPNOTSUPP, True),
        (errno.EIO, False),
    ],
)
def test_fsync_directory_tolerance_contract(
    tmp_path, monkeypatch, caplog, code, tolerated
):
    closed: list[int] = []
    fsynced: list[int] = []
    real_close = os.close

    def recording_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    def recording_fsync(descriptor):
        fsynced.append(descriptor)
        if code is not None:
            raise OSError(code, "directory fsync refused")

    monkeypatch.setattr(atomic_io_module, "_unsupported_fsync_logged", False)
    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "fsync", recording_fsync)

    if tolerated:
        fsync_directory(tmp_path)
    else:
        with pytest.raises(OSError) as raised:
            fsync_directory(tmp_path)
        assert raised.value.errno == code

    assert closed == fsynced != []
    assert ("event=atomic_io.dir_fsync_unsupported" in caplog.text) is (
        tolerated and code is not None
    )


@pytest.mark.parametrize("writer", ["text", "json", "update", "transform"])
def test_writers_publish_parent_group_by_default(tmp_path, monkeypatch, writer):
    """Every publishing writer defaults to ``group_from_parent=True``, so a
    root-run wizard or installer cannot publish root:root into a state
    directory a non-root daemon reads. Omitting the kwarg must behave exactly
    like passing True. Asserting the CALL, not the resulting stat: on a dev box
    the tempfile already inherits the test user's gid, so a filesystem check
    cannot fail when the group step is skipped."""
    from jasper.atomic_io import locked_transform_env_file, locked_update_env_file

    path = tmp_path / "wizard.env"
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        os, "chown", lambda target, uid, gid: calls.append((target, uid, gid))
    )

    {
        "text": lambda: atomic_write_text(path, "JASPER_X=1\n"),
        "json": lambda: atomic_write_json(path, {"JASPER_X": "1"}),
        "update": lambda: locked_update_env_file(path, {"JASPER_X": "1"}),
        "transform": lambda: locked_transform_env_file(
            path, lambda cur: {**cur, "JASPER_X": "1"}
        ),
    }[writer]()

    assert calls, "the writers must set the temp file group by default"
    target, uid, gid = calls[-1]
    assert os.path.dirname(target) == str(tmp_path)
    assert os.path.basename(target).startswith(".wizard.env.")
    assert (uid, gid) == (-1, os.stat(tmp_path).st_gid)


def test_advisory_lock_publishes_parent_group_by_default(tmp_path, monkeypatch):
    """The lock the env writers serialize on carries the same default: without
    the kwarg it takes the descriptor path and chgrps, so a root-held lock does
    not lock a group peer out of a shared state directory. The parent group is
    forced to differ because the real one already matches on a dev box."""
    foreign_gid = os.stat(tmp_path).st_gid + 1
    real_stat = os.stat

    def parent_in_a_foreign_group(target, *args, **kwargs):
        st = real_stat(target, *args, **kwargs)
        if os.fspath(target) == str(tmp_path):
            return os.stat_result(tuple(st)[:5] + (foreign_gid,) + tuple(st)[6:])
        return st

    fchowns: list[tuple[int, int]] = []
    monkeypatch.setattr(atomic_io_module.os, "stat", parent_in_a_foreign_group)
    monkeypatch.setattr(
        atomic_io_module.os, "fchown", lambda fd, uid, gid: fchowns.append((uid, gid))
    )

    with advisory_file_lock(tmp_path / "wizard.env.lock"):
        pass

    assert fchowns == [(-1, foreign_gid)]


def test_group_from_parent_chown_failure_cleans_up_temp(tmp_path, monkeypatch):
    path = tmp_path / "secret.env"

    def boom(*_args, **_kwargs):
        raise PermissionError("simulated group assignment failure")

    monkeypatch.setattr(os, "chown", boom)

    with pytest.raises(PermissionError):
        atomic_write_text(path, "doomed", mode=0o640, group_from_parent=True)

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_best_effort_group_failure_still_publishes_requested_mode(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Availability-sensitive state may keep writing after a chgrp denial."""
    path = tmp_path / "shared-state.json"

    def deny_chown(*_args, **_kwargs):
        raise PermissionError("simulated group assignment failure")

    monkeypatch.setattr(os, "chown", deny_chown)

    atomic_write_text(
        path,
        '{"status":"ready"}\n',
        mode=0o640,
        group_from_parent=True,
        best_effort_group=True,
    )

    assert path.read_text(encoding="utf-8") == '{"status":"ready"}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [path]
    assert "event=atomic_io.group_publish_failed" in caplog.text


def test_shared_lock_does_not_chmod_an_already_correct_root_owned_mode(
    tmp_path, monkeypatch
):
    """A group writer must not need owner privilege to open a healed lock."""

    lock_path = tmp_path / "shared.lock"
    lock_path.touch(mode=0o660)
    os.chmod(lock_path, 0o660)

    def unexpected_chmod(*_args, **_kwargs):
        raise AssertionError("an already-correct shared lock must not be chmodded")

    monkeypatch.setattr(os, "fchmod", unexpected_chmod)
    with advisory_file_lock(
        lock_path,
        mode=0o660,
        group_from_parent=True,
    ):
        pass


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires O_NOFOLLOW")
def test_shared_lock_refuses_symlink_without_mutating_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("sentinel", encoding="utf-8")
    os.chmod(target, 0o600)
    lock_path = tmp_path / "shared.lock"
    lock_path.symlink_to(target)

    with pytest.raises(OSError):
        with advisory_file_lock(
            lock_path,
            mode=0o660,
            group_from_parent=True,
        ):
            pass

    assert target.read_text(encoding="utf-8") == "sentinel"
    assert (os.stat(target).st_mode & 0o777) == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires O_NOFOLLOW")
@pytest.mark.parametrize("operation", ["update", "transform"])
def test_locked_env_writer_refuses_symlinked_data_without_disclosing_target(
    tmp_path, operation,
):
    from jasper.atomic_io import locked_transform_env_file, locked_update_env_file

    target = tmp_path / "root-secret.env"
    target.write_text("API_SECRET=sentinel\n", encoding="utf-8")
    os.chmod(target, 0o600)
    path = tmp_path / "source_intent.env"
    path.symlink_to(target)

    with pytest.raises(OSError):
        if operation == "update":
            locked_update_env_file(path, {"SAFE": "enabled"})
        else:
            locked_transform_env_file(
                path,
                lambda state: {**state, "SAFE": "enabled"},
            )

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "API_SECRET=sentinel\n"
    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_locked_env_writer_rejects_fifo_without_blocking(tmp_path):
    from jasper.atomic_io import locked_update_env_file

    path = tmp_path / "source_intent.env"
    os.mkfifo(path)

    with pytest.raises(OSError) as raised:
        locked_update_env_file(path, {"SAFE": "disabled"})
    assert raised.value.errno == errno.EINVAL


def test_locked_env_writer_byte_cap_rejects_without_replacing_file(tmp_path):
    from jasper.atomic_io import locked_update_env_file

    path = tmp_path / "source_intent.env"
    original = b"A=" + b"x" * 64
    path.write_bytes(original)

    with pytest.raises(OSError) as raised:
        locked_update_env_file(path, {"SAFE": "disabled"}, max_bytes=32)
    assert raised.value.errno == errno.EFBIG

    assert path.read_bytes() == original


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

    with pytest.raises(OSError):
        atomic_write_text(path, "doomed")

    # The target was never created, and the temp file was unlinked — the
    # directory is left exactly as it was.
    assert list(tmp_path.iterdir()) == []


def test_cleanup_failure_is_observable_without_masking_publish_error(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "state.txt"

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated rename failure")

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(os, "unlink", fail_cleanup)

    with pytest.raises(OSError):
        atomic_write_text(path, "doomed")

    assert "event=atomic_io.temp_cleanup_failed" in caplog.text
    assert list(tmp_path.glob(".state.txt.*.tmp"))


def test_chmod_failure_also_cleans_up_temp(tmp_path, monkeypatch):
    # A failure at chmod (inside the try, after the write, BEFORE the rename)
    # is a different raise point than os.replace but the same cleanup branch:
    # the temp must still be unlinked and the error propagate.
    path = tmp_path / "state.txt"

    def boom(*_args, **_kwargs):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", boom)

    with pytest.raises(OSError):
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


def test_preserve_target_stat_keeps_the_existing_mode(tmp_path):
    """The replace-in-place case: a repair that rewrites somebody else's file
    must not re-permission it. The existing file's mode WINS over ``mode=``.
    """
    p = tmp_path / "owned.json"
    p.write_text("old", encoding="utf-8")
    p.chmod(0o600)

    atomic_write_text(p, "new", mode=0o644, preserve_target_stat=True)

    assert p.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_preserve_target_stat_falls_back_to_mode_when_absent(tmp_path):
    """No target yet => nothing to preserve, so ``mode=`` keeps its normal
    meaning and the flag is a no-op rather than an error."""
    p = tmp_path / "fresh.json"

    atomic_write_text(p, "x", mode=0o640, preserve_target_stat=True)

    assert p.read_text(encoding="utf-8") == "x"
    assert stat.S_IMODE(p.stat().st_mode) == 0o640


def test_preserve_target_stat_copies_owner_when_it_can(tmp_path, monkeypatch):
    """uid/gid are copied from the target before the rename — the property a
    root-run migration over a daemon-owned file depends on. Real chown needs
    root, so this pins the CALL (uid and gid, on the tempfile, pre-rename)."""
    p = tmp_path / "owned.json"
    p.write_text("old", encoding="utf-8")
    target = p.stat()

    chowns: list[tuple[int, int]] = []
    real_chown = os.chown

    def spy(path, uid, gid):
        chowns.append((uid, gid))
        return real_chown(path, uid, gid)

    monkeypatch.setattr(atomic_io_module.os, "chown", spy)
    atomic_write_text(p, "new", preserve_target_stat=True)

    assert chowns == [(target.st_uid, target.st_gid)]
    assert p.read_text(encoding="utf-8") == "new"


def test_preserve_target_stat_survives_a_chown_permission_error(tmp_path, monkeypatch):
    """A non-root caller cannot chown to another uid — but it is normally
    already the owner, so the write must still land rather than fail."""
    p = tmp_path / "owned.json"
    p.write_text("old", encoding="utf-8")
    p.chmod(0o600)

    def deny(path, uid, gid):
        raise PermissionError("not root")

    monkeypatch.setattr(atomic_io_module.os, "chown", deny)
    atomic_write_text(p, "new", preserve_target_stat=True)

    assert p.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_locked_transform_replaces_and_can_drop_keys(tmp_path):
    from jasper.atomic_io import locked_transform_env_file

    path = tmp_path / "weather.env"
    path.write_text("A=1\nB=2\nC=3\n", encoding="utf-8")

    def transform(cur):
        # drop B, keep A, change C, add D
        return {"A": cur["A"], "C": "changed", "D": "new"}

    result = locked_transform_env_file(path, transform)
    assert result == {"A": "1", "C": "changed", "D": "new"}
    text = path.read_text(encoding="utf-8")
    assert "B=" not in text  # a merge-only helper could not drop this
    assert "A=1" in text and "C=changed" in text and "D=new" in text


def test_locked_transform_none_deletes_file(tmp_path):
    from jasper.atomic_io import locked_transform_env_file

    path = tmp_path / "weather.env"
    path.write_text("A=1\n", encoding="utf-8")
    assert locked_transform_env_file(path, lambda cur: None) is None
    assert not path.exists()


def test_locked_transform_absent_file_is_empty_dict(tmp_path):
    from jasper.atomic_io import locked_transform_env_file

    path = tmp_path / "weather.env"
    seen = {}

    def transform(cur):
        seen["cur"] = dict(cur)
        return {"X": "1"}

    locked_transform_env_file(path, transform)
    assert seen["cur"] == {}  # missing file -> empty parsed dict
    assert path.read_text(encoding="utf-8") == "X=1\n"


def test_locked_transform_serializes_concurrent_read_modify_writes(tmp_path):
    """N threads each add a distinct key via a read-modify-write with a sleep
    between the (internal) read and write. The shared flock must serialize
    them so NO update is lost — this is the exact two-writer shape weather.env
    faces from weather_setup + transit_setup."""
    import threading
    import time

    from jasper.atomic_io import locked_transform_env_file

    path = tmp_path / "weather.env"
    path.write_text("", encoding="utf-8")
    n = 12

    def add_key(i):
        def transform(cur):
            result = dict(cur)
            # Sleep INSIDE the critical section: an unlocked read-modify-write
            # would interleave here and lose updates; the flock must not.
            time.sleep(0.005)
            result[f"K{i}"] = str(i)
            return result
        locked_transform_env_file(path, transform)

    threads = [threading.Thread(target=add_key, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    from jasper.atomic_io import _parse_env_text
    final = _parse_env_text(path.read_text(encoding="utf-8"))
    assert final == {f"K{i}": str(i) for i in range(n)}  # nothing lost
