# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure multiroom assistant-TTS route matrix."""
from __future__ import annotations

from dataclasses import dataclass

from ..tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from .config import GroupingConfig, is_active_member


VOICE_PARK_ENV = "JASPER_GROUPING_VOICE_PARK"


@dataclass(frozen=True)
class GroupingTtsRoute:
    """Expected TTS route for one resolved grouping state.

    ``voice_env_socket`` is what the grouping reconciler writes into
    ``grouping-voice.env``. ``expected_voice_socket`` is what jasper-voice should
    resolve at runtime after systemd has layered the unit default and grouping
    override. It is ``None`` only when voice is intentionally parked and the
    playout socket is not meaningful for current runtime safety.
    """

    kind: str
    voice_env_socket: str | None
    expected_voice_socket: str | None
    outputd_tts_socket: str
    voice_parked: bool
    ok_detail: str

    @property
    def outputd_tts_armed(self) -> bool:
        return bool(self.outputd_tts_socket)


def expected_grouping_tts_route(
    cfg: GroupingConfig, *, active_endpoint: bool = False,
) -> GroupingTtsRoute:
    """Return the intended voice/outputd TTS route for ``cfg``.

    Matrix:
      - solo/off/invalid: voice uses the fan-in unit default; outputd TTS off
      - passive bonded non-sub: voice targets outputd; outputd TTS armed;
        followers also park voice/AEC through the shared park flag
      - active endpoint: voice uses fan-in; outputd TTS off
      - sub follower: voice is parked; outputd TTS off so full-range speech can
        never reach the subwoofer through outputd's post-low-pass mixer
        (the voice socket override still targets the unarmed outputd socket so
        an unexpected unpark fails silent instead of falling back to fan-in)
    """
    if not is_active_member(cfg):
        return GroupingTtsRoute(
            kind="solo",
            voice_env_socket=None,
            expected_voice_socket=FANIN_TTS_SOCKET,
            outputd_tts_socket="",
            voice_parked=False,
            ok_detail="solo / not an active bond member (n/a)",
        )

    voice_parked = cfg.role == "follower"

    if active_endpoint:
        return GroupingTtsRoute(
            kind="active_endpoint",
            voice_env_socket=None,
            expected_voice_socket=FANIN_TTS_SOCKET,
            outputd_tts_socket="",
            voice_parked=voice_parked,
            ok_detail="active endpoint TTS uses fan-in upstream of crossover",
        )

    if cfg.channel == "sub":
        if voice_parked:
            return GroupingTtsRoute(
                kind="parked_sub",
                voice_env_socket=OUTPUTD_TTS_SOCKET,
                expected_voice_socket=None,
                outputd_tts_socket="",
                voice_parked=True,
                ok_detail="parked sub follower TTS keeps outputd unarmed",
            )
        return GroupingTtsRoute(
            kind="sub",
            voice_env_socket=None,
            expected_voice_socket=FANIN_TTS_SOCKET,
            outputd_tts_socket="",
            voice_parked=False,
            ok_detail="sub TTS uses fan-in; outputd TTS is unarmed",
        )

    return GroupingTtsRoute(
        kind="passive_member",
        voice_env_socket=OUTPUTD_TTS_SOCKET,
        expected_voice_socket=OUTPUTD_TTS_SOCKET,
        outputd_tts_socket=OUTPUTD_TTS_SOCKET,
        voice_parked=voice_parked,
        ok_detail=f"member-local TTS wired ({OUTPUTD_TTS_SOCKET})",
    )
