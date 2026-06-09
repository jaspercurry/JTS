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

This module RAISES on failure (``OSError``) and cleans up the tempfile on any
exception. Callers that want fail-soft behaviour (log-and-continue, as several
``/var/lib/jasper`` writers do) wrap the call themselves — error handling is a
caller policy decision, not swallowed here. Stdlib-only (``os``, ``tempfile``)
so it stays import-cheap for the daemons that pull it in at startup.
"""
from __future__ import annotations

import os
import tempfile

__all__ = ["atomic_write_text"]


def atomic_write_text(
    path: str | os.PathLike, text: str, *, mode: int = 0o644,
) -> None:
    """Atomically write ``text`` to ``path`` as UTF-8, then ``chmod`` to ``mode``.

    Writes to a tempfile in the same directory as ``path`` and ``os.replace``s
    it into place, so a concurrent reader sees either the old file or the
    complete new one — never a partial write. The parent directory is created
    if missing. ``mode`` is applied to the tempfile BEFORE the rename, so the
    published file never appears with a wider permission window than requested.

    Raises ``OSError`` on any I/O failure; the tempfile is unlinked
    (best-effort) before the error propagates. Does NOT swallow errors — a
    caller wanting fail-soft semantics wraps this itself.
    """
    fspath = os.fspath(path)
    parent = os.path.dirname(fspath) or "."
    os.makedirs(parent, exist_ok=True)
    # Tempfile in the SAME directory => os.replace is an atomic same-FS rename.
    # Prefix with "." + basename so a directory listing groups it with the
    # target and a stray temp (e.g. on a crash mid-write) is recognisable.
    basename = os.path.basename(fspath)
    fd, tmp = tempfile.mkstemp(prefix="." + basename + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)  # before the rename: no wider-permission window
        os.replace(tmp, fspath)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
