# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the audio-hardware reconcile pass's step modules share.

A module of its own because the shim runs the pass as ``python -m
jasper.audio_hardware.reconcile``: that module is ``__main__`` there, so a
step importing it would load a second copy, and an ``_Abort`` raised from
that copy is not the class ``main()`` catches.
"""

from __future__ import annotations

import re

_LOG_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_.:,-]")


class _Abort(Exception):
    """Stop the pass and exit with ``status``."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _log_token(value: str) -> str:
    """``jasper_asound_log_token``: a value reduced to one grep-safe token."""
    if not value:
        return "direct"
    return _LOG_TOKEN_UNSAFE.sub("_", value)
