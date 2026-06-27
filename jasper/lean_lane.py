# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lean low-latency lane — the pure routing decision (Stage 4b).

The lean lane is the File-capture CamillaDSP path that sheds the fan-in
input ring for a single, exclusive, wired source (USB audio input): the
source writes a named pipe, CamillaDSP File-captures it (rate_adjust +
async resampler), and the rest of the chain (outputd content lane → DAC,
plus the AEC reference) is unchanged. See
docs/HANDOFF-audio-latency-foundation.md.

This module owns ONLY the decision — "does USB qualify for the lean path
right now?" — as a pure function of mux state + a default-OFF feature flag.
All the I/O (arming the FIFO output, swapping CamillaDSP to the File-capture
config) lives in mux/the reconciler and is gated on this decision. Splitting
the hard-to-get-right exclusivity logic into a pure, fully-tested function
mirrors how ``restore_action`` is split out of ``restore_solo_config`` in
``jasper.multiroom.leader_config``.

Default-OFF and inert: until ``JASPER_LEAN_LANE=enabled`` AND a caller wires
this in, ``decide_lean_route`` returns ``"buffered"`` for every input, so the
buffered fan-in path (today's behavior) is byte-identical.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .music_sources import Source


@dataclass(frozen=True)
class LeanDecision:
    """Result of :func:`decide_lean_route`.

    ``route`` is ``"lean"`` or ``"buffered"``; ``reason`` is a machine-stable
    tag for logs/tests (``"usb_exclusive"`` | ``"flag_off"`` | ``"idle"`` |
    ``"not_exclusive"`` | ``"non_usb_winner"``).
    """

    route: str
    reason: str


def decide_lean_route(
    *,
    active_sources: tuple[Source, ...],
    winner: Source | None,
    lean_enabled: bool,
) -> LeanDecision:
    """Pure: does the USB lane qualify for the lean File-capture path now?

    ``"lean"`` iff the feature flag is ON **and** USB is the SOLE active
    source **and** it is the audible winner. Everything else — flag off, more
    than one source mixing, a non-USB winner, or idle — routes ``"buffered"``,
    the always-safe fan-in path that needs the WiFi-burst absorber a wired USB
    source does not.

    No I/O, no CamillaDSP, no daemon calls. ``active_sources`` is the playing
    set (``Mux._active_sources(current)``); ``winner`` is ``Mux._winner``;
    ``lean_enabled`` is the parsed default-OFF flag (:func:`lean_lane_enabled`).
    """
    if not lean_enabled:
        return LeanDecision("buffered", "flag_off")
    if not active_sources:
        return LeanDecision("buffered", "idle")
    if active_sources != (Source.USBSINK,):
        return LeanDecision("buffered", "not_exclusive")
    if winner != Source.USBSINK:
        return LeanDecision("buffered", "non_usb_winner")
    return LeanDecision("lean", "usb_exclusive")


def lean_lane_enabled() -> bool:
    """``JASPER_LEAN_LANE`` — default OFF, opt-IN.

    Only the exact literal ``enabled`` (case-insensitive, stripped) turns it
    on; everything else (unset, ``disabled``, ``1``, ``true``, …) stays off.
    Opt-IN polarity (the inverse of mux's opt-OUT ``=disabled`` escape
    hatches) because the lean lane is new/experimental: an unset flag must be
    inert until the on-device 24 h soak gate passes.
    """
    return os.environ.get("JASPER_LEAN_LANE", "").strip().lower() == "enabled"
