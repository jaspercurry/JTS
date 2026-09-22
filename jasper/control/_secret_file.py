# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared mechanics for a single-secret-file gate.

Both :mod:`jasper.control.control_token` (per-device CSRF token) and
:mod:`jasper.control.household_credential` (per-household M2M secret) are the
same primitive — read/is-set/current/ensure/verify (plus adopt/clear for the
household side) over one text file holding one secret — pointed at different
paths. This module owns that mechanics, parametrised on the path; the two
callers keep their own module-level path constant (with their own env
override and docstring) and their own function names/signatures, so no caller
moves and no behaviour changes.

Shared invariants (see the callers for the *why* specific to each trust
domain):

- **Fresh read per call.** The path is read on every call, never cached — an
  out-of-band CLI rotate or a re-bond must be visible without a daemon
  restart.
- **Read errors resolve to "" , never a raise.** Missing file, permission
  denied, a directory in its place — all "not configured / not paired", so a
  request handler never 500s because the optional secret file couldn't be
  read.
- **Fail-safe-OPEN when absent.** :func:`verify` returns True for any input
  when the file is absent/empty. Both callers rely on this exact direction;
  do not flip it.
- **Constant-time compare** via :func:`hmac.compare_digest` once a secret IS
  stored, so length/prefix never leaks through timing.
- **Mode 0640, group jasper.** Written via
  :func:`jasper.atomic_io.atomic_write_text` (tempfile + rename), never
  world-readable, always group-readable for the sibling non-root daemon.
"""
from __future__ import annotations

import hmac
import os
import secrets

from jasper.atomic_io import atomic_write_text

_MODE = 0o640


def read(path: str) -> str:
    """The stripped secret at *path*, or "" when absent/empty/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def is_set(path: str) -> bool:
    """True iff *path* holds a non-empty secret."""
    return bool(read(path))


def ensure(path: str) -> str:
    """Generate + persist a secret at *path* if none exists; return it.

    Idempotent: an existing secret is returned unchanged, never rotated.
    """
    existing = read(path)
    if existing:
        return existing
    value = secrets.token_urlsafe(32)
    atomic_write_text(path, value + "\n", mode=_MODE)
    return value


def adopt(path: str, value: str | None) -> bool:
    """Persist *value* at *path* iff none is stored yet. Returns True iff written.

    Refuses to overwrite an existing secret; an empty/None *value* is a no-op.
    """
    if not value:
        return False
    if read(path):
        return False
    atomic_write_text(path, value + "\n", mode=_MODE)
    return True


def clear(path: str) -> None:
    """Remove *path*. Idempotent and best-effort (a missing/unremovable file is fine)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def verify(path: str, provided: str | None) -> bool:
    """True iff *provided* matches the secret at *path*, fail-safe-open when absent."""
    stored = read(path)
    if not stored:
        return True
    return hmac.compare_digest(provided or "", stored)
