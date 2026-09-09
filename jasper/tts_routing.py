# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared assistant-TTS socket/env contract.

The TTS wire protocol is outputd-compatible, but the default solo owner is
``jasper-fanin`` so assistant audio enters before CamillaDSP. Multiroom can
temporarily point voice at ``jasper-outputd`` for member-local playout.
"""
from collections.abc import Mapping

from jasper.env_load import VOICE_GROUPING_ENV_FILE, parse_env_file

FANIN_TTS_SOCKET_ENV = "JASPER_FANIN_TTS_SOCKET"
FANIN_TTS_SOCKET = "/run/jasper-fanin/tts.sock"

OUTPUTD_TTS_SOCKET_ENV = "JASPER_OUTPUTD_TTS_SOCKET"
OUTPUTD_TTS_SOCKET = "/run/jasper-outputd/tts.sock"

VOICE_TTS_SOCKET_ENV = "JASPER_TTS_OUTPUTD_SOCKET"

# Grouping may point the voice daemon at outputd, whose TTS lane is mixed
# after CamillaDSP.  The socket path itself is configurable, so consumers must
# not infer signal-chain position from a pathname.  The grouping reconciler is
# the single writer of this explicit topology fact, and writes it together with
# the socket override.  Absence means the normal solo, pre-DSP fan-in route.
TTS_MIX_STAGE_ENV = "JASPER_TTS_MIX_STAGE"
TTS_MIX_STAGE_PRE_DSP = "pre_dsp"
TTS_MIX_STAGE_POST_DSP = "post_dsp"


def resolve_tts_routing_snapshot(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = VOICE_GROUPING_ENV_FILE,
) -> dict[str, str]:
    """Read one coherent route snapshot: process env under the grouping file."""
    resolved = dict(env)
    if grouping_env_path is not None:
        resolved.update(parse_env_file(grouping_env_path))
    return resolved


def resolved_tts_socket_feeds_pre_dsp_fanin(resolved: Mapping[str, str]) -> bool:
    """Classify one already-resolved routing snapshot."""
    stage = resolved.get(TTS_MIX_STAGE_ENV)
    if stage is not None:
        return str(stage).strip().lower() == TTS_MIX_STAGE_PRE_DSP
    socket = str(resolved.get(VOICE_TTS_SOCKET_ENV, "")).strip()
    return not socket or socket == FANIN_TTS_SOCKET


def resolved_tts_socket_feeds_post_dsp_outputd(
    resolved: Mapping[str, str],
) -> bool:
    """Classify one already-resolved snapshot as CONFIRMED post-DSP outputd.

    Only an explicit ``JASPER_TTS_MIX_STAGE=post_dsp`` qualifies. Outputd
    interprets ``VOLUME_CONTEXT`` (issue #1547), so the SAME wire message is
    published to its socket — but only when the reconciler has stated the mix
    stage. A missing stage is the solo/pre-DSP default, never post-DSP.
    """
    stage = resolved.get(TTS_MIX_STAGE_ENV)
    if stage is None:
        return False
    return str(stage).strip().lower() == TTS_MIX_STAGE_POST_DSP


def tts_socket_feeds_pre_dsp_fanin(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = VOICE_GROUPING_ENV_FILE,
) -> bool:
    """Whether voice's resolved TTS socket feeds the pre-DSP fan-in.

    A reconciled passive multiroom member explicitly says ``post_dsp``. Unknown
    stage values fail closed: publishing pre-DSP compensation to an uncertain
    mix stage can create a large level error.
    """
    return resolved_tts_socket_feeds_pre_dsp_fanin(
        resolve_tts_routing_snapshot(env, grouping_env_path=grouping_env_path),
    )


def tts_socket_feeds_post_dsp_outputd(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = VOICE_GROUPING_ENV_FILE,
) -> bool:
    """Whether voice's resolved TTS socket feeds the post-DSP outputd mixer.

    True only for a reconciled passive multiroom member that explicitly says
    ``post_dsp``. Since outputd consumes ``VOLUME_CONTEXT`` (#1547), voice and
    the coordinator publish the same absolute wire message on this path — the
    post-DSP consumer owns the structural fact that its downstream attenuation
    is zero.
    """
    return resolved_tts_socket_feeds_post_dsp_outputd(
        resolve_tts_routing_snapshot(env, grouping_env_path=grouping_env_path),
    )
