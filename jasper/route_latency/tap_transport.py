# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the ingress tap the route-latency harness arms.

There is ONE USB ingress tap now: **fan-in DIRECT capture**. jasper-fanin opens
``hw:UAC2Gadget`` itself and taps the impulse there
(``rust/jasper-fanin/src/impulse_tap.rs``), armed over the fan-in control UDS
(``TAP_ARM`` verb) writing ``/run/jasper-fanin/impulse-tap.jsonl``.

The historical usbsink-bridge tap (armed over ``127.0.0.1:8781``) was removed with
the aloop solo capture path (2026-07-10): the jasper-usbsink daemon is standby-only
and opens no capture, so it has no tap to arm. ``--tap-transport`` is retained
(``auto`` == ``fanin``) only so existing invocations keep working.

This module is deliberately a thin resolver (no I/O) plus a client constructor,
mirroring :mod:`jasper.fanin.coupling_auto`'s pure-decision idiom.
"""
from __future__ import annotations

from dataclasses import dataclass

from jasper.route_latency.tap_client import (
    FANIN_CONTROL_SOCKET,
    FANIN_DEFAULT_TAP_PATH,
    FaninTapClient,
    TapArmer,
)

# The transport tokens the CLI's --tap-transport accepts. Both resolve to the
# fan-in DIRECT-capture tap — the only USB ingress tap since the aloop solo path
# was removed. ``auto`` is kept as the default for invocation compatibility.
TAP_TRANSPORT_AUTO = "auto"
TAP_TRANSPORT_FANIN = "fanin"
TAP_TRANSPORTS = (TAP_TRANSPORT_AUTO, TAP_TRANSPORT_FANIN)


def resolve_tap_transport(choice: str) -> str:
    """Resolve a ``--tap-transport`` choice to a concrete transport (pure).

    Always ``fanin`` — it is the only USB ingress tap. ``auto`` and any
    unrecognized value resolve to ``fanin`` too.
    """

    return TAP_TRANSPORT_FANIN


@dataclass(frozen=True)
class ResolvedTap:
    """A concrete tap target: which transport, its arm/disarm client, the JSONL
    path the tap writes (and the harness reads back), and a log-friendly reason.

    ``tap_path`` is authoritative for BOTH ends of the run: it is passed to the
    arm request (so the daemon writes exactly there) AND used as ``--tap-events``
    for the offline analyze, so a run reads the fan-in JSONL the fan-in tap just
    wrote.
    """

    transport: str
    client: TapArmer
    tap_path: str
    reason: str


def build_resolved_tap(
    *,
    transport_choice: str,
    explicit_tap_path: str | None,
    fanin_socket: str = FANIN_CONTROL_SOCKET,
) -> ResolvedTap:
    """Resolve the transport choice into a concrete tap target.

    Always the fan-in DIRECT-capture tap (the sole USB ingress). ``transport_choice``
    is accepted for CLI compatibility but does not change the outcome.
    ``explicit_tap_path`` (the operator's ``--tap-path``, or ``None``) overrides the
    fan-in default JSONL path.
    """

    transport = resolve_tap_transport(transport_choice)
    reason = (
        "fan-in DIRECT-capture tap (the only USB ingress tap; the usbsink bridge "
        "tap was removed with the aloop solo path)"
    )
    return ResolvedTap(
        transport=transport,
        client=FaninTapClient(socket_path=fanin_socket),
        tap_path=explicit_tap_path or FANIN_DEFAULT_TAP_PATH,
        reason=reason,
    )


__all__ = [
    "TAP_TRANSPORTS",
    "TAP_TRANSPORT_AUTO",
    "TAP_TRANSPORT_FANIN",
    "ResolvedTap",
    "build_resolved_tap",
    "resolve_tap_transport",
]
