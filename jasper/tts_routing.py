# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared assistant-TTS socket/env contract.

The TTS wire protocol is outputd-compatible, but the default solo owner is
``jasper-fanin`` so assistant audio enters before CamillaDSP. Multiroom can
temporarily point voice at ``jasper-outputd`` for member-local playout.
"""
from collections.abc import Mapping

FANIN_TTS_SOCKET_ENV = "JASPER_FANIN_TTS_SOCKET"
FANIN_TTS_SOCKET = "/run/jasper-fanin/tts.sock"

OUTPUTD_TTS_SOCKET_ENV = "JASPER_OUTPUTD_TTS_SOCKET"
OUTPUTD_TTS_SOCKET = "/run/jasper-outputd/tts.sock"

VOICE_TTS_SOCKET_ENV = "JASPER_TTS_OUTPUTD_SOCKET"
TTS_TRANSPORT_ENV = "JASPER_TTS_TRANSPORT"
DUCK_TRANSPORT_ENV = "JASPER_DUCK_TRANSPORT"

# Grouping may point the voice daemon at outputd, whose TTS lane is mixed
# after CamillaDSP.  The socket path itself is configurable, so consumers must
# not infer signal-chain position from a pathname.  The grouping reconciler is
# the single writer of this explicit topology fact; absent means the normal
# solo, pre-DSP fan-in route.
TTS_MIX_STAGE_ENV = "JASPER_TTS_MIX_STAGE"
TTS_MIX_STAGE_PRE_DSP = "pre_dsp"
TTS_MIX_STAGE_POST_DSP = "post_dsp"
GROUPING_VOICE_ENV_FILE = "/var/lib/jasper/grouping-voice.env"


def resolved_tts_routing_env(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
) -> dict[str, str]:
    """Overlay reconciler-owned topology facts onto process environment.

    Voice receives this file through systemd. Control and mux are not
    restarted for every bond transition, so publisher construction reads the
    same small, non-secret file instead of retaining stale process state.
    """
    resolved = dict(env)
    if grouping_env_path is not None:
        from .env_load import parse_env_file

        resolved.update(parse_env_file(grouping_env_path))
    return resolved


def tts_socket_feeds_pre_dsp_fanin(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
) -> bool:
    """Whether voice's resolved TTS socket feeds the pre-DSP fan-in.

    Defaulting to pre-DSP preserves the solo route and old installations.  A
    reconciled passive multiroom member explicitly says ``post_dsp``. Unknown
    values fail closed: publishing pre-DSP compensation to an uncertain mix
    stage can create a large level error.
    """
    resolved = resolved_tts_routing_env(
        env,
        grouping_env_path=grouping_env_path,
    )
    stage = resolved.get(TTS_MIX_STAGE_ENV, TTS_MIX_STAGE_PRE_DSP)
    return str(stage).strip().lower() == TTS_MIX_STAGE_PRE_DSP
