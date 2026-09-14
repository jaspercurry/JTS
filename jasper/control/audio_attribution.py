# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AirPlay drop attribution: network vs internal:receiver.

Evaluated only for an ``airplay.input_unavailable`` incident on a ring-armed
lane (jts4-class Zero 2 W). A leaf on purpose (see ``_health_fields``'s
docstring for the pattern): :data:`ATTRIBUTION_VERDICTS` is the one place the
verdict vocabulary is defined, so :mod:`audio_incidents` validates persisted
records against it instead of hand-syncing a second copy.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..music_sources import Source
from ._health_fields import _detail, _finite_number, _mapping

ATTRIBUTION_NETWORK_RATIO = 0.35
ATTRIBUTION_RECEIVER_RATIO = 0.70
_ATTRIBUTION_RECEIVER_STOPPED_STATES = frozenset({"T", "D", "Z"})
_ATTRIBUTION_LABELS = {
    "network": (
        "Audio stopped arriving over Wi-Fi (sender paused, or the link dropped)"
    ),
    "internal:receiver": (
        "Audio arrived but the receiver on this speaker did not play it"
    ),
    "unknown": "Not enough evidence to say",
}
# A closed token set -- "unknown" is a first-class, expected answer, not a
# failure to classify. audio_incidents.py's IncidentStore validates a
# persisted verdict against this set rather than keeping its own copy.
ATTRIBUTION_VERDICTS = frozenset(_ATTRIBUTION_LABELS)


def _input_attribution(
    airplay: Mapping[str, Any],
    active_source: str | None,
) -> dict[str, Any] | None:
    """Network vs internal:receiver verdict, evaluated only for AirPlay on a
    ring-armed lane. Rules, first match wins:

    1. no ring block, no baseline, baseline <= 0, or no link sample ->
       unknown
    2. receiver stopped/swapping (state T/D/Z, or majflt this tick) ->
       internal:receiver (checked first: a stalled receiver in TCP mode
       also collapses rx, so this must outrank the rate rule)
    3. rx rate < NETWORK_RATIO * baseline -> network
    4. rx rate >= RECEIVER_RATIO * baseline -> internal:receiver
    5. else -> unknown

    UDP RcvbufErrors is system-wide (any process' socket can overflow it)
    and cannot implicate shairport-sync specifically, so its delta is
    surfaced as evidence only, never a verdict rule.
    """
    if active_source != Source.AIRPLAY.value:
        return None
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = _mapping(_mapping(fanin.get("inputs")).get(Source.AIRPLAY.value))
    ring = _mapping(source_input.get("ring"))
    link = _mapping(current.get("link"))
    receiver = _mapping(link.get("receiver"))

    baseline = _finite_number(link.get("rx_bytes_per_sec_baseline"))
    rx_rate = _finite_number(link.get("rx_bytes_per_sec"))
    state = receiver.get("state")
    majflt_rate = _finite_number(receiver.get("majflt_per_sec"))
    rcvbuf_delta = _finite_number(link.get("udp_rcvbuf_errors_delta"))

    if not ring or baseline is None or baseline <= 0 or rx_rate is None:
        verdict = "unknown"
    elif state in _ATTRIBUTION_RECEIVER_STOPPED_STATES or (
        majflt_rate is not None and majflt_rate > 0
    ):
        verdict = "internal:receiver"
    elif rx_rate < ATTRIBUTION_NETWORK_RATIO * baseline:
        verdict = "network"
    elif rx_rate >= ATTRIBUTION_RECEIVER_RATIO * baseline:
        verdict = "internal:receiver"
    else:
        verdict = "unknown"

    details = [_detail("Verdict", _ATTRIBUTION_LABELS[verdict])]
    if rx_rate is not None and baseline:
        details.append(_detail(
            "Link rate",
            f"{rx_rate:.0f} B/s (baseline {baseline:.0f} B/s)",
        ))
    if state is not None:
        details.append(_detail("Receiver state", state))
    packet_rate = _finite_number(link.get("udp_in_datagrams_per_sec"))
    if packet_rate is not None:
        details.append(_detail("Packets in", f"{float(packet_rate):.0f}/s"))
    if rcvbuf_delta is not None and rcvbuf_delta > 0:
        details.append(_detail("UDP recv buffer errors", f"+{int(rcvbuf_delta)}"))
    return {"verdict": verdict, "details": details[:5]}
