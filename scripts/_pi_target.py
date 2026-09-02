#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared PI_HOST/PI_USER/JASPER_HOSTNAME resolution for laptop scripts.

Sources ``scripts/_lib.sh`` once -- the checkout's single ``.env.local``
reader (issue #2689) -- rather than each caller re-parsing the file itself.
``_lib.sh`` guarantees ``PI_HOST``/``PI_USER`` are non-empty after a
successful source, and exits non-zero when nothing names a target rather
than guessing ``jts.local`` (#3498), so this module treats a failed run OR
an empty result as "no target" and raises :class:`LibTargetError` rather
than guessing a value. Every caller refuses on that -- there is no
degraded default speaker left anywhere in the tree; what differs is only
how loudly (``scripts/jasper-pipe-probe`` exits with the message,
``scripts/run-crossover-round.py`` writes a Trail row first).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SH = REPO_ROOT / "scripts" / "_lib.sh"

_LIB_QUERY = (
    'set -u; . "$0" >/dev/null; '  # stderr survives: it carries _lib.sh's refusal
    'printf "%s\\n%s\\n%s\\n%s\\n" "$PI_HOST" "$PI_USER" '
    '"${JASPER_HOSTNAME:-}" "$JTS_TARGET_FROM"'
)


class LibTargetError(RuntimeError):
    """``scripts/_lib.sh`` could not be sourced or returned an empty target."""


def resolve_lib_target(
    *, host_override: str | None = None, timeout: float = 30,
) -> tuple[str, str, str, str]:
    """``(host, user, hostname, source)`` as ``scripts/_lib.sh`` itself
    resolves them: caller env, then ``.env.local``, then this box's own
    recorded identity, with ``source`` its own ``JTS_TARGET_FROM`` name for
    which of those answered. ``host_override`` is handed over as ``PI_HOST``
    so it takes that same caller branch -- the override then wins for the
    host inside ``_lib.sh``, where the file's ``PI_USER`` still applies, and
    an explicitly named target is never subject to its refusals.

    Raises :class:`LibTargetError` on any failure to run it, a nonzero exit
    (including its refusal to guess a target), or an empty
    ``PI_HOST``/``PI_USER`` in its output."""
    env = None
    if host_override:
        env = {**os.environ, "PI_HOST": host_override}
    try:
        proc = subprocess.run(
            ["bash", "-c", _LIB_QUERY, str(LIB_SH)],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LibTargetError(f"{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise LibTargetError(
            f"scripts/_lib.sh exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
        )
    lines = proc.stdout.splitlines()
    host, user, hostname, source = (lines + ["", "", "", ""])[:4]
    if not host or not user:
        raise LibTargetError(
            f"scripts/_lib.sh resolved an empty PI_HOST/PI_USER ({host!r}/{user!r}) "
            "-- this should be unreachable; see scripts/_lib.sh"
        )
    return host, user, hostname, source


def resolve_pi_target(*, host_override: str | None = None) -> tuple[str, str]:
    """``(host, user)`` the way every laptop script resolves them: an
    explicit override wins for HOST only -- ``.env.local``'s ``PI_USER``
    still applies even when only the host is overridden, because
    :func:`resolve_lib_target` resolves both regardless of which field the
    caller overrode (a per-field override, never all-or-nothing).
    Fails loud (lets :class:`LibTargetError` propagate) rather than
    silently defaulting."""
    lib_host, lib_user, _lib_hostname, _lib_source = resolve_lib_target(
        host_override=host_override)
    return lib_host, lib_user


__all__ = [
    "LIB_SH",
    "LibTargetError",
    "REPO_ROOT",
    "resolve_lib_target",
    "resolve_pi_target",
]
