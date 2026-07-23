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
# the single writer of this explicit topology fact.  Absence means the normal
# solo, pre-DSP fan-in route only when no legacy grouping socket override is
# present; socket-only grouping files from an older build are ambiguous and
# must fail closed during rolling upgrade.
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
    resolved, _grouping_socket_override = resolve_tts_routing_snapshot(
        env,
        grouping_env_path=grouping_env_path,
    )
    return resolved


def resolve_tts_routing_snapshot(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
) -> tuple[dict[str, str], bool]:
    """Read one coherent route snapshot plus legacy-file provenance."""
    resolved = dict(env)
    grouping_env: dict[str, str] = {}
    if grouping_env_path is not None:
        from .env_load import parse_env_file

        grouping_env = parse_env_file(grouping_env_path)
        resolved.update(grouping_env)
    return resolved, VOICE_TTS_SOCKET_ENV in grouping_env


def resolved_tts_socket_feeds_pre_dsp_fanin(
    resolved: Mapping[str, str],
    *,
    grouping_socket_override: bool,
) -> bool:
    """Classify one already-resolved routing snapshot."""
    stage = resolved.get(TTS_MIX_STAGE_ENV)
    if stage is not None:
        return str(stage).strip().lower() == TTS_MIX_STAGE_PRE_DSP

    if grouping_socket_override:
        return False
    socket = str(resolved.get(VOICE_TTS_SOCKET_ENV, "")).strip()
    return not socket or socket == FANIN_TTS_SOCKET


def resolved_tts_socket_feeds_post_dsp_outputd(
    resolved: Mapping[str, str],
    *,
    grouping_socket_override: bool,
) -> bool:
    """Classify one already-resolved snapshot as CONFIRMED post-DSP outputd.

    Only an explicit ``JASPER_TTS_MIX_STAGE=post_dsp`` qualifies. Outputd now
    interprets ``VOLUME_CONTEXT`` (issue #1547), so the SAME wire message is
    published to its socket — but only when the reconciler has stated the mix
    stage. A legacy socket-only grouping override (no stage) stays ambiguous
    and fails closed, the mirror of the pre-DSP classifier's fail-closed
    posture; a missing stage is the solo/pre-DSP default, never post-DSP.
    """
    # ``grouping_socket_override`` is accepted for signature symmetry with the
    # pre-DSP classifier; post-DSP requires the explicit stage regardless.
    del grouping_socket_override
    stage = resolved.get(TTS_MIX_STAGE_ENV)
    if stage is None:
        return False
    return str(stage).strip().lower() == TTS_MIX_STAGE_POST_DSP


def tts_socket_feeds_pre_dsp_fanin(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
) -> bool:
    """Whether voice's resolved TTS socket feeds the pre-DSP fan-in.

    A reconciled passive multiroom member explicitly says ``post_dsp``.
    Unknown values and legacy socket-only grouping overrides fail closed:
    publishing pre-DSP compensation to an uncertain mix stage can create a
    large level error.  A missing stage with no grouping override remains the
    normal solo, pre-DSP default.
    """
    resolved, grouping_socket_override = resolve_tts_routing_snapshot(
        env,
        grouping_env_path=grouping_env_path,
    )
    # Before JASPER_TTS_MIX_STAGE existed, grouping-voice.env carried only
    # the outputd socket override. Treat any file-owned socket override as
    # ambiguous rather than sending pre-DSP compensation into a post-DSP
    # mixer during an upgrade window. The canonical outputd path check also
    # covers a process that loaded the old file before it was removed.
    return resolved_tts_socket_feeds_pre_dsp_fanin(
        resolved,
        grouping_socket_override=grouping_socket_override,
    )


def tts_socket_feeds_post_dsp_outputd(
    env: Mapping[str, str],
    *,
    grouping_env_path: str | None = GROUPING_VOICE_ENV_FILE,
) -> bool:
    """Whether voice's resolved TTS socket feeds the post-DSP outputd mixer.

    True only for a reconciled passive multiroom member that explicitly says
    ``post_dsp``. Since outputd now consumes ``VOLUME_CONTEXT`` (#1547), voice
    and the coordinator publish the same absolute wire message on this path —
    the post-DSP consumer owns the structural fact that its downstream
    attenuation is zero. Legacy socket-only grouping overrides and a missing
    stage fail closed (they are handled by the pre-DSP/solo path instead).
    """
    resolved, grouping_socket_override = resolve_tts_routing_snapshot(
        env,
        grouping_env_path=grouping_env_path,
    )
    return resolved_tts_socket_feeds_post_dsp_outputd(
        resolved,
        grouping_socket_override=grouping_socket_override,
    )
