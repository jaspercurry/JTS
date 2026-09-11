# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Speak one cue from a daemon that is about to park, then let it exit.

A park holds the unit down (`RestartPreventExitStatus`), so a wake-blocking
fault must still announce itself here per AGENTS.md non-negotiable 6.
Nothing here reads a microphone or the AEC bridge.
"""
from __future__ import annotations

import asyncio
import logging
import os

from ..tts_playout import TtsPlayout
from ..tts_routing import FANIN_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
from .factory import build_env_cue_manager

# Bound on a park cue: the 4.65 s cue plus drain, plus TtsPlayout's own 1.0 s
# connect timeout. Past this the daemon is holding systemd's start timeout
# (READY=1 was never sent) for a cue nobody will hear.
PARK_CUE_TIMEOUT_SEC = 12.0


def play_park_cue(slug: str, *, logger: logging.Logger) -> str:
    """Play `slug` through the fan-in TTS socket. Never raises.

    Returns the result code the caller logs: ``ok``, ``play_failed``,
    ``play_error``, ``timeout`` or ``interrupted``. The ``event=`` line stays
    with the caller so each park keeps its own package's event prefix.

    Owns its own event loop, so callers must not be inside a running one.
    Nothing escapes — not even a ``BaseException``: this is called from inside
    the caller's ``except`` handler or return path, where an escape would skip
    the park's exit code and the process would exit 1, which is neither a park
    nor a success code for systemd.
    """
    # The cap is held out here so the classifier below can ask which bound
    # fired: TtsPlayout's own 1.0 s connect timeout raises TimeoutError too,
    # and `asyncio.TimeoutError is TimeoutError` on 3.11+, so the exception
    # type alone cannot tell the two apart. `asyncio.timeout()` reads the
    # running loop's clock, so it can only be built inside the coroutine.
    cap: asyncio.Timeout | None = None

    async def _play() -> str:
        nonlocal cap
        socket_path = os.environ.get(VOICE_TTS_SOCKET_ENV, FANIN_TTS_SOCKET)
        async with (
            asyncio.timeout(PARK_CUE_TIMEOUT_SEC) as cap,
            TtsPlayout(socket_path=socket_path) as tts,
        ):
            manager = build_env_cue_manager(tts_playout=tts)
            return "ok" if await manager.play(slug) else "play_failed"

    try:
        return asyncio.run(_play())
    except TimeoutError:
        if cap is not None and cap.expired():
            return "timeout"
        logger.exception("park cue play failed")
        return "play_error"
    except Exception:  # noqa: BLE001
        logger.exception("park cue play failed")
        return "play_error"
    except BaseException:  # noqa: BLE001
        return "interrupted"
