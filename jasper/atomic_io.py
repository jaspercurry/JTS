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

import os
import tempfile
from collections.abc import Mapping

import fcntl

__all__ = ["atomic_write_text", "locked_update_env_file"]


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


def locked_update_env_file(
    path: str | os.PathLike,
    updates: Mapping[str, str],
    *,
    mode: int = 0o644,
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
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                with open(fspath, encoding="utf-8") as existing:
                    state = _parse_env_text(existing.read())
            except FileNotFoundError:
                state = {}
            state.update(dict(updates))
            atomic_write_text(fspath, _format_env_text(state), mode=mode)
            return dict(state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
