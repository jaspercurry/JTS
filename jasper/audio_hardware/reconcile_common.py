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

import os
import re
from pathlib import Path

ENV_FILE_MODE = 0o640
ENV_DIR_MODE = 0o750

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


def _ensure_dir(path: Path, mode: int) -> None:
    """Create an absent directory at ``mode``; never re-mode an existing one.

    The installer owns each env directory's mode/group, and a blanket re-mode
    on every boot/udev reconcile re-strips them (#827).
    """
    if not path.is_dir():
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
