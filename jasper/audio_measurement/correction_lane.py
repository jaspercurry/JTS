# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""SSOT for the correction/commissioning ``aplay`` lane (``WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd``, snd-aloop on ``hw:Loopback,0,4``); drift guard ``tests/test_correction_substream_ssot.py``.

Stays outside ``jasper.correction`` (import cycle) and stdlib-only (socket-activated wizard consumers)."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import os

# Lane name on an unarmed box.
CORRECTION_SUBSTREAM = "correction_substream"

# 0o007: shared ring HEADER must land 0660 (pinned `UMask=0007` in tests/test_renderer_ring_lanes.py); root's inherited 0022 breaks it.
CORRECTION_PLAY_UMASK = 0o007


def correction_play_device() -> str:
    """The ALSA PCM the correction lane opens right now; resolved fresh per call so an arm/disarm change takes effect on the next spawn."""
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label("correction")
    if lane is None:  # fail-safe; provably dead while the registry row exists
        return CORRECTION_SUBSTREAM
    return rl.device_for(lane, "correction" in rl.read_armed_labels())


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
        umask=CORRECTION_PLAY_UMASK,
    )


async def exec_correction_play(
    wav_path: str | os.PathLike[str],
    *,
    stdout: int | None,
    stderr: int | None,
) -> asyncio.subprocess.Process:
    """Spawn a correction-lane ``aplay`` for asyncio callers; ``umask=`` reaches the child via ``create_subprocess_exec``'s pass-through to :class:`subprocess.Popen`."""
    import asyncio

    return await asyncio.create_subprocess_exec(
        *correction_play_argv(wav_path),
        stdout=stdout,
        stderr=stderr,
        umask=CORRECTION_PLAY_UMASK,
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
        umask=CORRECTION_PLAY_UMASK,
    )
