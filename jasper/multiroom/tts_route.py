# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure multiroom assistant-TTS route matrix."""
from __future__ import annotations

from dataclasses import dataclass

from ..tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from . import config
from .config import GroupingConfig


VOICE_PARK_ENV = "JASPER_GROUPING_VOICE_PARK"

#: Route kind for a bonded member whose DAC output the CamillaDSP graph owns —
#: roleful, protected, subwoofer-bearing, unconfigured or invalid topologies.
#: Structured; the doctor renders it and tests match it.
TTS_ROUTE_GRAPH_OWNED_OUTPUT = "graph_owned_output"


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
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
) -> GroupingTtsRoute:
    """Return the intended voice/outputd TTS route for ``cfg``.

    Matrix:
      - solo/off/invalid: voice uses the fan-in unit default; outputd TTS off
      - passive bonded member: voice targets outputd; outputd TTS armed;
        followers also park voice/AEC through the shared park flag
      - active endpoint: voice uses fan-in; outputd TTS off
      - any other box whose DAC output the GRAPH owns: voice uses fan-in;
        outputd TTS off

    The outputd TTS mixer is a DIRECT DAC path — post-graph, inside outputd —
    so it consumes BOTH facts of the one topology read
    (``jasper.multiroom.reconcile.output_topology_state``), exactly as
    :func:`~jasper.multiroom.reconcile.member_lane_decision` does for the
    dac-content lane. ``active_endpoint`` alone is NARROWER than the topologies
    whose DAC outputs the graph owns: a subwoofer beside passive mains, and a
    protected full-range output, declare no active group yet still carry the
    crossover/protection the mixer would bypass (#2380).

    ``flat_output_allowed`` is
    :func:`~jasper.active_speaker.runtime_contract.topology_allows_flat_dac_graph`.
    Its default is FAIL-CLOSED: a caller that cannot answer gets fan-in.
    """
    if not config.is_active_member(cfg):
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

    if not flat_output_allowed:
        return GroupingTtsRoute(
            kind=TTS_ROUTE_GRAPH_OWNED_OUTPUT,
            voice_env_socket=None,
            expected_voice_socket=FANIN_TTS_SOCKET,
            outputd_tts_socket="",
            voice_parked=voice_parked,
            ok_detail=(
                "this box's saved topology does not permit a flat DAC output, "
                "so TTS uses fan-in upstream of the graph"
            ),
        )

    return GroupingTtsRoute(
        kind="passive_member",
        voice_env_socket=OUTPUTD_TTS_SOCKET,
        expected_voice_socket=OUTPUTD_TTS_SOCKET,
        outputd_tts_socket=OUTPUTD_TTS_SOCKET,
        voice_parked=voice_parked,
        ok_detail=f"member-local TTS wired ({OUTPUTD_TTS_SOCKET})",
    )
