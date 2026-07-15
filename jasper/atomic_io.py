# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The single home for atomic text-file writes in JTS.

The codebase persists small bits of runtime state to disk all over
(``mic_mute.env``, ``speaker_volume.json``, the multiroom reconciler's
derived-args env file, …). Every one of those wants the SAME guarantee: a
reader either sees the OLD file or the COMPLETE new file, never a torn or
half-written one. That is a tempfile-in-the-same-directory + ``os.replace``
rename, which is atomic on a POSIX same-filesystem rename. This module is the
canonical implementation; call it instead of hand-rolling the pattern.

Two properties are load-bearing and easy to get subtly wrong by hand:

  - **Same-filesystem rename.** The tempfile is created in the SAME directory
    as the target (``dir=parent``), not ``/tmp``. ``os.replace`` is only
    atomic within one filesystem; a cross-FS rename degrades to copy+unlink,
    which is not atomic.
  - **No wider-permission window.** ``os.chmod`` is applied to the tempfile
    BEFORE the rename, so the file is never visible at the final path with a
    broader mode than requested (``mkstemp`` creates 0600, then we widen to
    ``mode`` only after, and the published name appears already-correct).
  - **Optional parent-group publishing.** Some shared state files are written by
    root during install and by non-root daemons at runtime. When requested, the
    tempfile is chowned to the parent directory's group before chmod+rename, so a
    root-run atomic replace does not publish ``root:root 0640`` into a
    group-readable state directory.

This module RAISES on failure (``OSError``) and cleans up the tempfile on any
exception. Callers that want fail-soft behaviour (log-and-continue, as several
``/var/lib/jasper`` writers do) wrap the call themselves — error handling is a
caller policy decision, not swallowed here. Stdlib-only (``os``, ``tempfile``)
so it stays import-cheap for the daemons that pull it in at startup.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from io import TextIOWrapper
from typing import Callable

import fcntl

__all__ = [
    "advisory_file_lock",
    "atomic_write_text",
    "locked_transform_env_file",
    "locked_update_env_file",
    "read_regular_bytes_nofollow",
]


@contextmanager
def advisory_file_lock(
    path: str | os.PathLike,
    *,
    mode: int | None = None,
    group_from_parent: bool = False,
    timeout_sec: float | None = None,
):
    """Hold an exclusive advisory lock on ``path``.

    The default preserves the historical ``open(..., 'a+')`` ownership and
    umask behavior. Shared cross-user locks can opt into an explicit ``mode``
    and the parent directory's group; both are applied before the lock is made
    available to another process. Existing pre-upgrade ownership drift still
    requires an install-time heal because a non-owner cannot repair a lock it
    cannot open. ``timeout_sec`` adds bounded backpressure for request/deploy
    paths; the historical default remains a blocking lock for tiny internal
    state updates whose callers do not expose a latency contract.
    """

    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    if mode is None and not group_from_parent:
        lock: TextIOWrapper = open(fspath, "a+", encoding="utf-8")
    else:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(fspath, flags, 0o666)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "lock path is not a regular file", fspath)
            if group_from_parent:
                parent_gid = os.stat(parent).st_gid
                if os.fstat(fd).st_gid != parent_gid:
                    os.fchown(fd, -1, parent_gid)
            # A group writer can open a correctly provisioned root-owned lock
            # but cannot chmod it.  Avoid an unnecessary privileged mutation
            # when install has already published the requested mode.
            if mode is not None and stat.S_IMODE(os.fstat(fd).st_mode) != mode:
                os.fchmod(fd, mode)
            lock = os.fdopen(fd, "a+", encoding="utf-8")
        except (OSError, ValueError):
            os.close(fd)
            raise
    acquired = False
    try:
        if timeout_sec is None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            acquired = True
        else:
            if timeout_sec < 0:
                raise ValueError("timeout_sec must be non-negative")
            deadline = time.monotonic() + timeout_sec
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out waiting for lock {fspath}"
                        ) from None
                    time.sleep(min(0.05, remaining))
        yield lock
    finally:
        if acquired:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def atomic_write_text(
    path: str | os.PathLike,
    text: str,
    *,
    mode: int = 0o644,
    group_from_parent: bool = False,
) -> None:
    """Atomically write ``text`` to ``path`` as UTF-8, then ``chmod`` to ``mode``.

    Writes to a tempfile in the same directory as ``path`` and ``os.replace``s
    it into place, so a concurrent reader sees either the old file or the
    complete new one — never a partial write. The parent directory is created
    if missing. ``mode`` is applied to the tempfile BEFORE the rename, so the
    published file never appears with a wider permission window than requested.
    When ``group_from_parent`` is true, the tempfile's group is set to the
    parent directory's group before chmod+rename; this keeps root-run writers
    from publishing group-readable files under the wrong group.

    Raises ``OSError`` on any I/O failure; the tempfile is unlinked
    (best-effort) before the error propagates. Does NOT swallow errors — a
    caller wanting fail-soft semantics wraps this itself.
    """
    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    parent_gid = os.stat(parent).st_gid if group_from_parent else None
    # Tempfile in the SAME directory => os.replace is an atomic same-FS rename.
    # Prefix with "." + basename so a directory listing groups it with the
    # target and a stray temp (e.g. on a crash mid-write) is recognisable.
    basename = os.path.basename(fspath)
    fd, tmp = tempfile.mkstemp(prefix="." + basename + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if parent_gid is not None:
            os.chown(tmp, -1, parent_gid)
        os.chmod(tmp, mode)  # before the rename: no wider-permission window
        os.replace(tmp, fspath)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _format_env_text(values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"env value for {key} contains newline")
        lines.append(f"{key}={value}\n")
    return "".join(lines)


def _env_lock_path(path: str) -> str:
    parent = os.path.dirname(path) or "."
    basename = os.path.basename(path)
    return os.path.join(parent, f".{basename}.lock")


def read_regular_bytes_nofollow(
    path: str | os.PathLike,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read a bounded regular file by descriptor without following symlinks.

    ``O_NONBLOCK`` prevents a hostile FIFO from blocking before its type can be
    checked. It has no effect on regular-file reads. The byte cap is enforced
    while reading, not only from an initial size snapshot, so a concurrently
    growing inode cannot make a privileged reader allocate without bound.
    """

    fspath = os.fspath(path)
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be nonnegative")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(fspath, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "path is not a regular file", fspath)
        chunks: list[bytes] = []
        total = 0
        while True:
            read_size = 64 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes + 1 - total)
                if read_size <= 0:
                    raise OSError(
                        errno.EFBIG,
                        f"path exceeds the {max_bytes}-byte cap",
                        fspath,
                    )
            chunk = os.read(fd, read_size)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise OSError(
                    errno.EFBIG,
                    f"path exceeds the {max_bytes}-byte cap",
                    fspath,
                )
    finally:
        os.close(fd)


def _read_env_state_nofollow(
    path: str,
    *,
    max_bytes: int | None = None,
) -> dict[str, str]:
    """Read one regular env file without following a replaceable symlink.

    Locked env files often live in group-writable state directories and may be
    updated by a more-privileged peer. Opening the data path by name with plain
    ``open`` would let another group member redirect that peer to an arbitrary
    readable file. Hold the returned inode by descriptor, reject non-regular
    files, and parse only that verified descriptor.
    """

    return _parse_env_text(
        read_regular_bytes_nofollow(path, max_bytes=max_bytes).decode("utf-8")
    )


def locked_update_env_file(
    path: str | os.PathLike,
    updates: Mapping[str, str],
    *,
    mode: int = 0o644,
    group_from_parent: bool = False,
    lock_mode: int | None = None,
    max_bytes: int | None = None,
    lock_timeout_sec: float | None = None,
) -> dict[str, str]:
    """Serialize a read-modify-write update of a systemd EnvironmentFile.

    ``atomic_write_text`` protects readers from torn writes, but it cannot
    protect two writers that both read the old file, update different keys, and
    then publish whole-file replacements. This helper holds an advisory flock
    across the read, update, and atomic replace so cooperating writers preserve
    each other's keys.
    """
    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    lock_path = _env_lock_path(fspath)
    with advisory_file_lock(
        lock_path,
        mode=lock_mode,
        group_from_parent=group_from_parent,
        timeout_sec=lock_timeout_sec,
    ):
        try:
            state = _read_env_state_nofollow(fspath, max_bytes=max_bytes)
        except FileNotFoundError:
            state = {}
        state.update(dict(updates))
        atomic_write_text(
            fspath,
            _format_env_text(state),
            mode=mode,
            group_from_parent=group_from_parent,
        )
        return dict(state)


def locked_transform_env_file(
    path: str | os.PathLike,
    transform: Callable[[dict[str, str]], "dict[str, str] | None"],
    *,
    mode: int = 0o644,
    group_from_parent: bool = False,
    lock_mode: int | None = None,
    max_bytes: int | None = None,
    lock_timeout_sec: float | None = None,
) -> dict[str, str] | None:
    """Serialize a full read-transform-write (or delete) of an EnvironmentFile.

    Like :func:`locked_update_env_file`, but for writers that must DROP keys
    or DELETE the file — a merge-only ``updates`` mapping cannot express
    either. ``transform`` receives the current parsed dict (empty when the
    file is absent) and returns the COMPLETE new dict to write, or ``None`` to
    delete the file; returning the input unchanged is a no-op the caller can
    use for a read-decide-skip (its read then runs under the lock, closing the
    check-then-act race). Holds the SAME advisory flock as
    ``locked_update_env_file`` on the same path, so both helpers mutually
    exclude writers of one file. Returns the written dict, or ``None`` when the
    file was deleted or left absent.
    """
    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    lock_path = _env_lock_path(fspath)
    with advisory_file_lock(
        lock_path,
        mode=lock_mode,
        group_from_parent=group_from_parent,
        timeout_sec=lock_timeout_sec,
    ):
        try:
            state = _read_env_state_nofollow(fspath, max_bytes=max_bytes)
        except FileNotFoundError:
            state = {}
        new_state = transform(dict(state))
        if new_state is None:
            try:
                os.unlink(fspath)
            except FileNotFoundError:
                pass
            return None
        atomic_write_text(
            fspath,
            _format_env_text(new_state),
            mode=mode,
            group_from_parent=group_from_parent,
        )
        return dict(new_state)
