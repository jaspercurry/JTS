# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""SSOT for the correction/commissioning ``aplay`` lane (``WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd``, snd-aloop on ``hw:Loopback,0,4``); drift guard ``tests/test_correction_substream_ssot.py``.

Stdlib-only, so the socket-activated wizard consumers never pay for numpy."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import os

CORRECTION_SUBSTREAM = "correction_substream"

# Where the lane's generated WAVs are cached. deploy/install.sh creates it and
# the correction unit's ReadWritePaths admits it; moving it means grepping the
# tree for the literal, which is spelled outside Python too.
CORRECTION_TONE_DIR = Path("/var/lib/jasper/correction/tones")


def correction_play_device() -> str:
    """The ALSA PCM the correction lane opens."""
    return CORRECTION_SUBSTREAM


def correction_play_argv(wav_path: str | os.PathLike[str]) -> list[str]:
    """The ``aplay`` argv that plays one WAV onto the correction lane."""
    return ["aplay", "-D", correction_play_device(), "-q", str(wav_path)]


def popen_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> subprocess.Popen[bytes]:
    """Spawn a correction-lane ``aplay`` for sync/thread callers."""
    return subprocess.Popen(
        correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
    )


async def exec_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> asyncio.subprocess.Process:
    """Spawn a correction-lane ``aplay`` for asyncio callers."""
    import asyncio

    return await asyncio.create_subprocess_exec(
        *correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
    )


def run_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Play one WAV on the correction lane, blocking until it exits."""
    return subprocess.run(
        correction_play_argv(wav_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
