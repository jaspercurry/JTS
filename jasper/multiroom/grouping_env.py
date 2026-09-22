# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The lane arming rule, and the per-service env derived from a resolved
``GroupingConfig``. PURE.

Split out of ``jasper.multiroom.reconcile`` (the single writer of these env
files); this module holds the derivation, never the write.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import tts_routing as _tts_routing
from ..env_load import AIRPLAY_BONDED_EXTRA_DELAY_ENV
from ..fanin_coupling import OUTPUTD_CONTENT_BRIDGE_ENV_VAR
from . import config
from .config import GroupingConfig
from .dac_content_ring import (
    DAC_CONTENT_LANE_ENV,
    OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
    OUTPUTD_DAC_CONTENT_TRIM_ENV,
    dac_content_ring_servable,
)
from .tts_route import VOICE_PARK_ENV, expected_grouping_tts_route

OUTPUTD_TTS_SOCKET_ENV = _tts_routing.OUTPUTD_TTS_SOCKET_ENV
VOICE_TTS_SOCKET_ENV = _tts_routing.VOICE_TTS_SOCKET_ENV
TTS_MIX_STAGE_ENV = _tts_routing.TTS_MIX_STAGE_ENV
TTS_MIX_STAGE_POST_DSP = _tts_routing.TTS_MIX_STAGE_POST_DSP

#: Why a bonded member is not on the dac-content return ring. Stable tokens:
#: they reach ``/state`` and the doctor through the follower STATUS file.
LANE_REFUSED_ACTIVE_ENDPOINT = "active_endpoint"
LANE_REFUSED_FLAT_OUTPUT_DENIED = "flat_output_not_allowed"
LANE_REFUSED_PERIOD = "dac_content_ring_period_mismatch"


@dataclass(frozen=True)
class LaneDecision:
    """Whether this box arms the dac-content return lane, and why not."""

    armed: bool
    #: One of the ``LANE_REFUSED_*`` tokens, or ``""`` when armed.
    reason: str = ""


def member_lane_decision(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
    outputd_period_frames: int | None = None,
) -> LaneDecision:
    """THE arming rule for the dumb-member round-trip lane. PURE.

    Four conditions, spelled once and consumed by everything that needs the
    answer — the env writer below, the reconciler's bond refusal, and the
    doctor's channel-pick check:

    - an ``is_active_member``-shaped config (enabled, no error);
    - not an ACTIVE endpoint: CamillaDSP owns that box's channel-pick and split
      (Layer A), so outputd runs its normal active sink and no lane;
    - a saved topology that permits a flat final-output graph, from the
      canonical output runtime contract;
    - an outputd period the ring's slot can carry
      (:func:`~jasper.multiroom.dac_content_ring.dac_content_ring_servable`).

    A disabled or invalid config is not refused — it is not a member at all —
    so it returns the same unarmed decision with no reason token.
    """
    if not (cfg.enabled and cfg.error is None):
        return LaneDecision(armed=False)
    if active_endpoint:
        return LaneDecision(armed=False, reason=LANE_REFUSED_ACTIVE_ENDPOINT)
    if not flat_output_allowed:
        return LaneDecision(armed=False, reason=LANE_REFUSED_FLAT_OUTPUT_DENIED)
    if not dac_content_ring_servable(outputd_period_frames):
        return LaneDecision(armed=False, reason=LANE_REFUSED_PERIOD)
    return LaneDecision(armed=True)


def outputd_grouping_env(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
    outputd_period_frames: int | None = None,
) -> dict[str, str]:
    """The outputd round-trip lane env derived from a GroupingConfig. PURE.

    Whether the lane arms is :func:`member_lane_decision`'s answer, never a
    second rule; what the lane IS is
    :mod:`jasper.multiroom.dac_content_ring`'s module docstring.

    Every non-arming shape gets EMPTY strings rather than absent keys — outputd
    reads empty as unset (``env_optional``) and as disarmed (``env_bool``), so a
    stale file can never half-configure the lane.

    ``active_endpoint`` (the ACTIVE follower, plus the active leader's own
    drivers) DISABLES the ``dac_content`` ChannelPick on this box: CamillaDSP
    owns both the channel-pick and the ``2->N`` split (Layer A), so outputd just
    runs its normal active sink fed by camilla.

    THE ARMED BRANCH WRITES A BLANK ``JASPER_OUTPUTD_CONTENT_BRIDGE``, and every
    other branch OMITS the key. outputd refuses the marker beside a DECLARED
    bridge of any value (``rust/jasper-outputd/src/config.rs``), and its
    ``env_optional`` read counts blank as undeclared — so blank is what
    overrides the ``shm_ring`` that ``jasper-fanin-coupling-auto`` writes into
    the FIRST env layer on every pass. Omitting the key there would leave that
    value standing and park the daemon at EX_CONFIG under
    ``RestartPreventExitStatus=78``. The unarmed branches must NOT write blank:
    without the marker outputd reads this key with ``env_str``, whose blank is a
    value it parks on, so an unarmed box has to inherit layer 1 verbatim.

    Active-mode TTS stays upstream of the crossover in fan-in: the outputd TTS
    mixer is stereo-only and post-crossover, and on an active lane a 2-way
    speaker is also "2 channels", so arming that socket would send full-range
    assistant audio to the tweeter. Active endpoints therefore clear the outputd
    TTS socket along with the lane — and so does every other box whose DAC
    outputs the graph owns, which is why the route reads ``flat_output_allowed``
    from the same decision the lane does (#2380).
    """
    route = expected_grouping_tts_route(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
    )

    if cfg.enabled and cfg.error is None:
        if not member_lane_decision(
            cfg,
            active_endpoint=active_endpoint,
            flat_output_allowed=flat_output_allowed,
            outputd_period_frames=outputd_period_frames,
        ).armed:
            return {
                DAC_CONTENT_LANE_ENV: "",
                OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
                OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
                # Empty = unset to outputd's env_f32 (default 0.0).
                OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
            }  # no CONTENT_BRIDGE key: layer 1's value must stand
        return {
            # The BARE marker outputd's env_bool reads (never a path — outputd
            # derives the ring file from its own DEFAULT_DAC_CONTENT_RING_PATH,
            # so the two ends have no second spelling to disagree on).
            DAC_CONTENT_LANE_ENV: "1",
            # BLANK, not absent: this layer loads AFTER outputd.env, where
            # jasper-fanin-coupling-auto writes shm_ring on every pass.
            OUTPUTD_CONTENT_BRIDGE_ENV_VAR: "",
            OUTPUTD_DAC_CONTENT_CHANNEL_ENV: cfg.channel or "stereo",
            OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
            # Pair-balance trim (validated <= 0 by load_config; outputd
            # re-validates fail-closed). Always written while bonded so
            # a cleared trim converges back to 0.0.
            OUTPUTD_DAC_CONTENT_TRIM_ENV: f"{cfg.trim_db:.1f}",
        }
    return {
        DAC_CONTENT_LANE_ENV: "",
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
        OUTPUTD_TTS_SOCKET_ENV: "",
        # Empty = unset to outputd's env_f32 (default 0.0).
        OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
    }


def voice_grouping_env(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
) -> dict[str, str]:
    """jasper-voice's grouping-derived env. PURE.

    The route matrix owns the policy. Passive members point voice's TTS
    playout socket at outputd so each member's OWN replies mix at its OWN final
    output; inv-3 keeps the leader's TTS out of the SHARED stream. Active
    endpoints fail closed to fan-in or park, with outputd TTS unarmed. Solo also
    returns an EMPTY dict — the key is omitted, never present-but-empty (a
    set-empty value would be read as a real, invalid socket path).

    Takes the SAME two route facts as :func:`outputd_grouping_env`: the two
    files are one route, and a caller that answered them differently would aim
    voice at a socket outputd does not serve.
    """
    route = expected_grouping_tts_route(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
    )
    if cfg.enabled and cfg.error is None:
        env = (
            {}
            if route.voice_env_socket is None
            else {
                VOICE_TTS_SOCKET_ENV: route.voice_env_socket,
                TTS_MIX_STAGE_ENV: TTS_MIX_STAGE_POST_DSP,
            }
        )
        if route.voice_parked:
            # Parked routes stop voice (and the AEC stack) through the flag
            # jasper-aec-reconcile gates on; the route matrix owns any socket
            # override separately.
            env[VOICE_PARK_ENV] = "1"
        return env
    return {}


def airplay_grouping_env(cfg: GroupingConfig) -> dict[str, str]:
    """shairport's bonded-leader AirPlay latency-offset delta. PURE.

    Only an ACTIVE bonded LEADER both receives AirPlay AND plays its own channel
    through the Snapcast round-trip, so only a leader's shairport must fold the
    Snapcast playout buffer into its backend latency offset to keep the leader's
    OWN output landing on the AirPlay anchor (lip-sync). Everyone else — solo,
    follower (shairport parked), invalid — gets an EMPTY dict, which clears the
    file to the byte-identical solo offset.

    The value is the Snapcast buffer in SECONDS — the dominant new delay the
    bonded leader's own output gains over solo, and deliberately a first-order
    estimate: the solo offset's Ring A / CamillaDSP / Ring B / outputd terms
    still apply in the bonded path, and the residual (CamillaDSP pipe-sink fill,
    the member content FIFO) is second-order and acoustically calibrated
    alongside snapclient --latency. jasper-apply-airplay-mode ADDS this to the
    solo-derived offset.
    """
    if config.is_active_leader(cfg):
        return {AIRPLAY_BONDED_EXTRA_DELAY_ENV: f"{cfg.buffer_ms / 1000:.6f}"}
    return {}
