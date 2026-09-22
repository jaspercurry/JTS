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
