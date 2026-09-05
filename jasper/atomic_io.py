# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The single home for atomic text-file writes in JTS.

The codebase persists small bits of runtime state to disk all over
(``mic_mute.env``, the volume-state file, the multiroom reconciler's
derived-args env file, …). Every one of those wants the SAME guarantee: a
reader either sees the OLD file or the COMPLETE new file, never a torn or
half-written one. That is a tempfile-in-the-same-directory + ``os.replace``
rename, which is atomic on a POSIX same-filesystem rename. This module is the
canonical implementation; call it instead of hand-rolling the pattern.

These properties are load-bearing and easy to get subtly wrong by hand:

  - **Same-filesystem rename.** The tempfile is created in the SAME directory
    as the target (``dir=parent``), not ``/tmp``. ``os.replace`` is only
    atomic within one filesystem; a cross-FS rename degrades to copy+unlink,
    which is not atomic.
  - **No wider-permission window.** ``os.chmod`` is applied to the tempfile
    BEFORE the rename, so the file is never visible at the final path with a
    broader mode than requested (``mkstemp`` creates 0600, then we widen to
    ``mode`` only after, and the published name appears already-correct).
  - **Parent-group publishing, by default.** Some shared state files are written
    by root during install and by non-root daemons at runtime. The unpublished
    file — the tempfile, or a freshly opened lock — is chgrped to the parent
    directory's group before it becomes visible, so a root-run atomic replace
    does not publish ``root:root 0640`` into a group-readable state directory.
    Publication is BEST EFFORT and has no strict mode: a writer that may not
    chgrp (a non-root process outside the target group — every CLI writing to
    an operator-named path) keeps its own group, which is never wider than the
    parent's, logs one line, and publishes the file anyway.
    ``group_from_parent=False`` opts out a root-only file that must keep
    root's group.
  - **Optional target-stat preservation.** A repair or migration that rewrites a
    file it does not own must not re-own it. ``preserve_target_stat=True`` copies
    the EXISTING file's uid/gid/mode onto the tempfile before the rename — the
    stricter form of the bullet above, which sets the group only.
  - **One shared lock mode.** Advisory locks — including the ones the env
    writers take — default to ``SHARED_LOCK_MODE``, group-writable, so two
    units running as different service users can share one lock.

This module RAISES on failure (``OSError``) and cleans up the tempfile on any
exception. Callers that want fail-soft behaviour (log-and-continue, as several
``/var/lib/jasper`` writers do) wrap the call themselves — error handling is a
caller policy decision, not swallowed here. It stays import-cheap for daemons:
the only project import is the stdlib-only structured-log emitter.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from io import TextIOWrapper
from typing import Any, Callable

import fcntl

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_FILE_MODE",
    "SHARED_LOCK_MODE",
    "advisory_file_lock",
    "advisory_file_lock_async",
    "atomic_write_json",
    "atomic_write_text",
    "fsync_directory",
    "locked_transform_env_file",
    "locked_update_env_file",
    "read_regular_bytes_nofollow",
]


_UNSUPPORTED_DIR_FSYNC = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_unsupported_fsync_logged = False


def fsync_directory(path: str | os.PathLike) -> None:
    """Make a directory entry's creation or removal durable.

    A rename or unlink is metadata: without this the entry can still be
    present (or absent) after a dirty shutdown even though the file's own
    contents were synced. Filesystems that do not support directory fsync
    report it as an argument error rather than a fault, and are tolerated —
    every caller's durability is best-effort on those. The first tolerated
    call per process logs a WARNING; being a static property of the mount,
    repeats stay silent (they would evict real diagnostics from the flight
    recorder). Real faults (EIO, EACCES, a vanished path) always raise.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIR_FSYNC:
            raise
        global _unsupported_fsync_logged
        if not _unsupported_fsync_logged:
            _unsupported_fsync_logged = True
            log_event(
                logger,
                "atomic_io.dir_fsync_unsupported",
                level=logging.WARNING,
                path=path,
                error=exc,
                note="rename durability is best-effort on this filesystem",
            )
    finally:
        os.close(descriptor)


def _publish_parent_group(fd: int, parent_gid: int, *, path: str) -> None:
    """Best-effort chgrp of an unpublished file to its parent's group.

    See this module's docstring for the policy; a denial is logged, not raised.
    """

    if os.fstat(fd).st_gid == parent_gid:
        return
    try:
        os.fchown(fd, -1, parent_gid)
    except PermissionError:
        log_event(
            logger,
            "atomic_io.group_publish_failed",
            level=logging.WARNING,
            path=path,
            gid=parent_gid,
        )


# Taking an advisory lock opens the file O_RDWR, so a peer that has only group
# READ cannot take it at all; and units disagree on ``UMask=``, so the mode is
# asserted on every acquire rather than left to whichever unit creates the
# file. See ADR-0196.
SHARED_LOCK_MODE = 0o660

# Per-request web paths wait on these locks, so a bounded wait retries at this
# cadence rather than a coarser sleep that would round every handoff up.
_LOCK_POLL_SECONDS = 0.01

# An async acquire that settles inside this grace was never really contended;
# only a longer wait is worth announcing.
_LOCK_CONTENDED_AFTER_SECONDS = 0.01


@contextmanager
def advisory_file_lock(
    path: str | os.PathLike,
    *,
    mode: int = SHARED_LOCK_MODE,
    group_from_parent: bool = True,
    timeout_sec: float | None = None,
):
    """Hold an exclusive advisory lock on ``path``.

    ``mode`` defaults to :data:`SHARED_LOCK_MODE`; pass a narrower one only for
    a lock no second service user may take. ``group_from_parent`` follows the
    module docstring's parent-group publishing rule, so a root-run holder
    cannot lock a non-root peer out of a shared state directory. Mode and group
    are both applied before the lock is made available to another process, and
    both are BEST EFFORT: a non-owner cannot repair either, so a denial is
    logged and the acquire proceeds on the descriptor it already opened, while
    pre-upgrade drift is repaired by the install heal. ``timeout_sec`` adds
    bounded backpressure for request/deploy paths; the historical default
    remains a blocking lock for tiny internal state updates whose callers do
    not expose a latency contract.
    """

    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(fspath, flags, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "lock path is not a regular file", fspath)
        if group_from_parent:
            _publish_parent_group(fd, os.stat(parent).st_gid, path=fspath)
        # A group writer can open a correctly provisioned root-owned lock
        # but cannot chmod it.  Avoid an unnecessary privileged mutation
        # when install has already published the requested mode.
        if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
            try:
                os.fchmod(fd, mode)
            except PermissionError:
                # The open already succeeded, so the lock is functional; only a
                # non-owner lands here, and the install heal repairs the mode.
                log_event(
                    logger,
                    "atomic_io.lock_mode_failed",
                    level=logging.WARNING,
                    path=fspath,
                    mode=f"0o{mode:o}",
                )
        lock: TextIOWrapper = os.fdopen(fd, "a+", encoding="utf-8")
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
                    time.sleep(min(_LOCK_POLL_SECONDS, remaining))
        yield lock
    finally:
        if acquired:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


@asynccontextmanager
async def advisory_file_lock_async(
    path: str | os.PathLike,
    *,
    timeout_sec: float,
    mode: int = SHARED_LOCK_MODE,
    group_from_parent: bool = True,
    on_contended: Callable[[], None] | None = None,
) -> AsyncIterator[TextIOWrapper]:
    """Hold :func:`advisory_file_lock` without blocking the event loop.

    ``asyncio.to_thread`` cannot be cancelled, so a waiter that leaves — a
    cancelled task, or one whose deadline passed — can still have a worker
    that wins the flock afterwards. The acquire is therefore awaited under
    ``asyncio.shield`` and, when it is still pending on the way out, a
    done-callback closes the held stack once the worker settles: a lock won
    after the caller left is released, never stranded behind an ownerless
    holder. The deadline is re-checked on the loop when the worker returns
    because a saturated executor delays the worker's own clock; admission
    always means "inside ``timeout_sec``", and a lock handed back late is
    released and reported as ``TimeoutError``. ``on_contended``, when given,
    is called once on the loop if the acquire has not settled within
    :data:`_LOCK_CONTENDED_AFTER_SECONDS`, so a caller can announce a wait
    while it is still happening.
    """

    # Deferred: importing asyncio costs ~60 ms, and this module is imported by
    # short-lived synchronous env writers on a 415 MB Pi (ADR-0226).
    import asyncio

    deadline = time.monotonic() + timeout_sec
    held = ExitStack()
    # One open per acquire: the primitive's own bounded retry runs on the
    # worker, so a contended caller neither reopens the lock file per poll nor
    # blocks the event loop while it waits.
    acquire = asyncio.ensure_future(
        asyncio.to_thread(
            held.enter_context,
            advisory_file_lock(
                path,
                mode=mode,
                group_from_parent=group_from_parent,
                timeout_sec=timeout_sec,
            ),
        )
    )

    def _release_when_settled(settled: asyncio.Future[Any]) -> None:
        if not settled.cancelled():
            settled.exception()
        held.close()

    try:
        if on_contended is not None:
            finished, _pending = await asyncio.wait(
                {acquire}, timeout=_LOCK_CONTENDED_AFTER_SECONDS
            )
            if not finished:
                on_contended()
        lock = await asyncio.shield(acquire)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for lock {os.fspath(path)}")
        yield lock
    finally:
        if acquire.done():
            held.close()
        else:
            acquire.add_done_callback(_release_when_settled)


def atomic_write_text(
    path: str | os.PathLike,
    text: str,
    *,
    mode: int = 0o644,
    group_from_parent: bool = True,
    preserve_target_stat: bool = False,
    durable: bool = False,
) -> None:
    """Atomically write ``text`` to ``path`` as UTF-8, then ``chmod`` to ``mode``.

    Writes to a tempfile in the same directory as ``path`` and ``os.replace``s
    it into place, so a concurrent reader sees either the old file or the
    complete new one — never a partial write. The parent directory is created
    if missing. ``mode`` is applied to the tempfile BEFORE the rename, so the
    published file never appears with a wider permission window than requested.
    ``group_from_parent`` follows the module docstring's parent-group
    publishing rule, applied to the tempfile before chmod+rename.

    ``preserve_target_stat=True`` is the REPLACE-IN-PLACE case: when the target
    already exists, its uid, gid, and mode are copied onto the tempfile before
    the rename, so an atomic replace does not re-own or re-permission a file
    somebody else created. Use it for repairs/migrations that rewrite a file
    the writing process does not own — a root-run migration over a daemon's
    own state files is the motivating case (``group_from_parent`` cannot cover
    it: that sets the GID only, leaving the UID as root). The existing file's
    stat WINS over ``mode`` and ``group_from_parent``; when the target does not
    exist yet, both fall back to their normal meaning. The chown is
    best-effort — a non-root caller cannot chown to another uid, and in that
    case it already owns the file.

    ``durable=True`` flushes and fsyncs the tempfile before publication, then
    fsyncs the parent directory where the platform supports directory fsync.
    Boot-critical callers use this stronger contract; ordinary runtime state
    keeps the cheaper default.

    Raises ``OSError`` on any I/O failure; the tempfile is unlinked
    (best-effort) before the error propagates. Does NOT swallow errors — a
    caller wanting fail-soft semantics wraps this itself.
    """
    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    parent_gid = os.stat(parent).st_gid if group_from_parent else None
    target_stat = None
    if preserve_target_stat:
        try:
            target_stat = os.stat(fspath)
        except FileNotFoundError:
            target_stat = None
    if target_stat is not None:
        mode = stat.S_IMODE(target_stat.st_mode)
        parent_gid = None  # the target's own gid is more specific
    # Tempfile in the SAME directory => os.replace is an atomic same-FS rename.
    # Prefix with "." + basename so a directory listing groups it with the
    # target and a stray temp (e.g. on a crash mid-write) is recognisable.
    basename = os.path.basename(fspath)
    fd, tmp = tempfile.mkstemp(prefix="." + basename + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if parent_gid is not None:
                _publish_parent_group(f.fileno(), parent_gid, path=fspath)
        if target_stat is not None:
            try:
                os.chown(tmp, target_stat.st_uid, target_stat.st_gid)
            except PermissionError:
                # Not root: this caller cannot chown to another uid, and a
                # non-root caller replacing a file it can write is normally
                # already the owner. Mode below still applies.
                pass
        os.chmod(tmp, mode)  # before the rename: no wider-permission window
        if durable:
            # Sync after ownership/mode changes so the durability promise
            # covers both file contents and the metadata published at rename.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(tmp, flags)
            try:
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        os.replace(tmp, fspath)
        if durable:
            fsync_directory(parent)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            # A durable write may fail while syncing the parent directory
            # after ``os.replace`` has already published the target. In that
            # case the tempfile no longer exists; cleanup is complete.
            pass
        except OSError as cleanup_exc:
            log_event(
                logger,
                "atomic_io.temp_cleanup_failed",
                level=logging.WARNING,
                path=tmp,
                error=cleanup_exc,
            )
        raise


# Group-readable so the non-root jasper-control reader can read sound
# configs; group jasper comes from the target directory's setgid bit, not
# from this mode.
CONFIG_FILE_MODE = 0o640


def atomic_write_json(
    path: str | os.PathLike,
    payload: Any,
    *,
    mode: int = 0o644,
    group_from_parent: bool = True,
    preserve_target_stat: bool = False,
    durable: bool = False,
) -> None:
    """Serialize ``payload`` deterministically and publish it atomically.

    This is the JSON form of :func:`atomic_write_text`; it deliberately exposes
    the same ownership and durability policy knobs so state owners choose those
    once without reimplementing tempfile publication. The canonical encoding is
    UTF-8, two-space indentation, sorted keys, and one trailing newline.
    """

    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        mode=mode,
        group_from_parent=group_from_parent,
        preserve_target_stat=preserve_target_stat,
        durable=durable,
    )


def read_json_mapping(path: str | os.PathLike) -> dict[str, Any] | None:
    """One JSON object off disk, or ``None`` — never raises. ``None`` covers
    every way the artifact can fail to be a mapping: missing, unreadable, not
    UTF-8, unparsable, or a non-object top level."""
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


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
    group_from_parent: bool = True,
    lock_mode: int = SHARED_LOCK_MODE,
    max_bytes: int | None = None,
    lock_timeout_sec: float | None = None,
) -> dict[str, str]:
    """Serialize a read-modify-write update of a systemd EnvironmentFile.

    ``atomic_write_text`` protects readers from torn writes, but it cannot
    protect two writers that both read the old file, update different keys, and
    then publish whole-file replacements. This helper holds an advisory flock
    across the read, update, and atomic replace so cooperating writers preserve
    each other's keys. ``lock_mode`` is the lock's own mode; see
    :data:`SHARED_LOCK_MODE`.
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
        text = _format_env_text(state)
        atomic_write_text(
            fspath, text, mode=mode, group_from_parent=group_from_parent
        )
        return dict(state)


def locked_transform_env_file(
    path: str | os.PathLike,
    transform: Callable[[dict[str, str]], "dict[str, str] | None"],
    *,
    mode: int = 0o644,
    group_from_parent: bool = True,
    lock_mode: int = SHARED_LOCK_MODE,
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
    exclude writers of one file. ``lock_mode`` is the lock's own mode; see
    :data:`SHARED_LOCK_MODE`. Returns the written dict, or ``None`` when the
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
        text = _format_env_text(new_state)
        atomic_write_text(
            fspath, text, mode=mode, group_from_parent=group_from_parent
        )
        return dict(new_state)
