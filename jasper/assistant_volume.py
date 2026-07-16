# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Effective speaker-volume facts published to the assistant mix owner.

``VolumeCoordinator`` remains the sole owner of the user's canonical
``listening_level`` and of which physical attenuator carries it.  Fan-in needs
only two derived facts in order to keep assistant loudness stable across
source handoffs:

* ``canonical_db`` — the calibrated dB representation of user intent;
* ``downstream_db`` — the actual CamillaDSP gain after fan-in.

The message is absolute and idempotent.  It intentionally contains no source
name and no LUFS value: source policy stays in ``VolumeCoordinator`` and all
loudness measurement stays in fan-in.
"""
from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

from .tts_routing import (
    DUCK_TRANSPORT_ENV,
    FANIN_TTS_SOCKET,
    GROUPING_VOICE_ENV_FILE,
    VOICE_TTS_SOCKET_ENV,
    resolved_tts_routing_env,
    tts_socket_feeds_pre_dsp_fanin,
)


@dataclass(frozen=True)
class EffectiveVolumeContext:
    canonical_db: float
    downstream_db: float
    tts_envelope_lufs: float
    muted: bool


VolumeContextPublisher = Callable[[EffectiveVolumeContext], Awaitable[None]]


def volume_context_stamp_boot_ns() -> int:
    """Timestamp an update in the boot-local clock domain used by fan-in."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime_ns(clock_id)


def serialize_volume_context(
    context: EffectiveVolumeContext,
    *,
    stamp_boot_ns: int | None = None,
) -> bytes:
    """Serialize the one canonical Python representation of this command."""
    stamp = volume_context_stamp_boot_ns() if stamp_boot_ns is None else stamp_boot_ns
    return (
        f"VOLUME_CONTEXT {context.canonical_db:.3f} "
        f"{context.downstream_db:.3f} {context.tts_envelope_lufs:.3f} "
        f"{1 if context.muted else 0} {int(stamp)}\n"
    ).encode("ascii")


def _send_volume_context(
    socket_path: str,
    context: EffectiveVolumeContext,
    *,
    timeout: float = 0.5,
) -> None:
    payload = serialize_volume_context(context) + b"CLOSE\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(payload)


def make_volume_context_publisher(
    socket_path: str = FANIN_TTS_SOCKET,
) -> VolumeContextPublisher:
    """Return a best-effort async publisher for one fan-in TTS socket."""

    async def publish(context: EffectiveVolumeContext) -> None:
        await asyncio.to_thread(_send_volume_context, socket_path, context)

    return publish


def volume_context_publisher_for_runtime(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
    dynamic_topology: bool = False,
) -> VolumeContextPublisher | None:
    """Build a publisher only when fan-in owns the pre-DSP speech mix.

    The legacy/outputd route sits at a different point in the signal chain,
    so Camilla gain is not its downstream attenuation and must not be sent as
    though it were.
    """
    process_env = dict(env)
    resolved = resolved_tts_routing_env(
        env,
        grouping_env_path=grouping_env_path,
    )
    if process_env.get(DUCK_TRANSPORT_ENV, "fanin").strip().lower() != "fanin":
        return None
    if dynamic_topology:
        async def publish(context: EffectiveVolumeContext) -> None:
            current = resolved_tts_routing_env(
                process_env,
                grouping_env_path=grouping_env_path,
            )
            if not tts_socket_feeds_pre_dsp_fanin(
                current,
                grouping_env_path=None,
            ):
                return
            await asyncio.to_thread(
                _send_volume_context,
                current.get(VOICE_TTS_SOCKET_ENV, FANIN_TTS_SOCKET),
                context,
            )

        return publish
    if not tts_socket_feeds_pre_dsp_fanin(
        resolved,
        grouping_env_path=None,
    ):
        return None
    return make_volume_context_publisher(
        resolved.get(VOICE_TTS_SOCKET_ENV, FANIN_TTS_SOCKET),
    )
