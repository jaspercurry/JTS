# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair tap-side (ingress) and mic-side (egress) impulse detections.

Both detection sequences are on ``CLOCK_MONOTONIC`` **of the same host** (the
Rust tap runs in the usbsink process on the Pi; this harness's mic reader
also runs on the Pi), which is the only reason subtracting one timeline from
the other is valid — do not compare these to a timestamp captured on the
playback host.

Pairing is nearest-match-within-a-window, not index-alignment: real playback
has variable click-to-click timing (the promotion preset is intentionally
jittered) and real capture can drop, duplicate-detect, or miss an impulse.
The algorithm:

1. Every mic detection must arrive *after* its tap detection — a click can't
   be heard before it was played — so only forward-in-time candidates within
   ``window_ms`` are eligible.
2. Among eligible candidates, the nearest one wins.
3. If a tap event has more than one eligible mic detection within the window,
   or a mic detection is the nearest candidate for more than one tap event,
   the match is **ambiguous** and rejected outright rather than guessed at —
   silently picking one guess is exactly the failure mode a latency
   measurement can't afford (see :func:`pair_events`).

The result reports matched, unmatched-tap, unmatched-mic, and
ambiguous-rejected counts so ``analyze`` can print an honest summary and
refuse to certify below the match-rate floor.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TapEvent:
    """One Rust-tap ingress detection (see the pinned JSONL schema)."""

    monotonic_ns: int
    frame_index: int
    ring_fill_frames: int
    peak: float


@dataclass(frozen=True)
class MicDetection:
    """One mic-side (egress) detection, already re-anchored to a packet
    arrival time per :mod:`jasper.route_latency.mic_readers`'s clock rule."""

    monotonic_ns: int
    peak: float


@dataclass(frozen=True)
class MatchedImpulse:
    """One paired tap/mic detection, ready for latency-math consumption."""

    tap: TapEvent
    mic: MicDetection

    @property
    def raw_delta_ns(self) -> int:
        """mic_detect - tap_detect, in nanoseconds (before ring-fill/
        distance-compensation terms — see route_latency_harness's latency
        math for the full formula)."""
        return self.mic.monotonic_ns - self.tap.monotonic_ns


@dataclass(frozen=True)
class PairingResult:
    matched: tuple[MatchedImpulse, ...]
    unmatched_tap: tuple[TapEvent, ...]
    unmatched_mic: tuple[MicDetection, ...]
    ambiguous_tap: tuple[TapEvent, ...]
    ambiguous_mic: tuple[MicDetection, ...]

    @property
    def tap_count(self) -> int:
        return len(self.matched) + len(self.unmatched_tap) + len(self.ambiguous_tap)

    @property
    def match_rate(self) -> float:
        """Fraction of tap events that produced an unambiguous match.

        Denominator is the tap side (the side with a known "how many
        impulses were tapped" ground truth) — the mic side can only ever
        produce a subset of that (or, on a noisy capture, spurious extras
        that become unmatched_mic and don't inflate this number)."""
        total = self.tap_count
        if total == 0:
            return 0.0
        return len(self.matched) / total


DEFAULT_WINDOW_MS = 200.0


def pair_events(
    tap_events: list[TapEvent],
    mic_detections: list[MicDetection],
    *,
    window_ms: float = DEFAULT_WINDOW_MS,
) -> PairingResult:
    """Nearest-match pairing within `window_ms`, rejecting ambiguous matches.

    `window_ms` bounds the plausible ingress-to-egress latency: real route
    latency is expected in the tens of ms (the p95 budget is 40 ms, p99 is
    42 ms), so a generous-but-bounded 200 ms window comfortably covers a
    slow/degraded route without pairing across two different impulses on a
    densely-spaced (jittered promotion) schedule.
    """

    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    window_ns = int(window_ms * 1_000_000)

    taps = sorted(tap_events, key=lambda e: e.monotonic_ns)
    mics = sorted(mic_detections, key=lambda e: e.monotonic_ns)

    # For each tap, collect eligible mic indices (mic time in
    # (tap_time, tap_time + window]); a mic detection strictly at or before
    # tap time cannot be a response to that tap (a click can't be heard
    # before it's played).
    candidates: dict[int, list[int]] = {}
    mic_idx_start = 0
    for tap_i, tap in enumerate(taps):
        lo = tap.monotonic_ns
        hi = tap.monotonic_ns + window_ns
        # Advance the low-water mark: mic detections before any past tap's
        # lower bound can never be eligible for a later (larger-timestamp)
        # tap either, since taps are sorted ascending.
        while mic_idx_start < len(mics) and mics[mic_idx_start].monotonic_ns <= lo:
            mic_idx_start += 1
        eligible: list[int] = []
        j = mic_idx_start
        while j < len(mics) and mics[j].monotonic_ns <= hi:
            eligible.append(j)
            j += 1
        if eligible:
            candidates[tap_i] = eligible

    # Nearest-candidate-per-tap, then detect any mic index claimed by more
    # than one tap (ambiguous on the mic side) or any tap with more than one
    # eligible mic (ambiguous on the tap side).
    nearest_mic_for_tap: dict[int, int] = {}
    for tap_i, eligible in candidates.items():
        tap_ns = taps[tap_i].monotonic_ns
        nearest_mic_for_tap[tap_i] = min(
            eligible, key=lambda mic_i: mics[mic_i].monotonic_ns - tap_ns
        )

    mic_claim_counts: dict[int, int] = {}
    for mic_i in nearest_mic_for_tap.values():
        mic_claim_counts[mic_i] = mic_claim_counts.get(mic_i, 0) + 1

    matched: list[MatchedImpulse] = []
    matched_tap_idx: set[int] = set()
    matched_mic_idx: set[int] = set()
    ambiguous_tap_idx: set[int] = set()
    ambiguous_mic_idx: set[int] = set()
    for tap_i, eligible in candidates.items():
        mic_i = nearest_mic_for_tap[tap_i]
        tap_ambiguous = len(eligible) > 1
        mic_ambiguous = mic_claim_counts.get(mic_i, 0) > 1
        if tap_ambiguous or mic_ambiguous:
            ambiguous_tap_idx.add(tap_i)
            # Every candidate that made this tap's match uncertain is part
            # of the ambiguity, not just the nearest one: a rival mic
            # detection at 35ms is exactly WHY a tap with a 30ms and a
            # 35ms candidate got rejected, so it belongs in ambiguous_mic
            # (a real rival candidate), never in unmatched_mic (which
            # implies "no tap detection is plausibly related to this").
            ambiguous_mic_idx.update(eligible)
            continue
        # Carry the indices the loop already has rather than re-deriving them
        # with taps.index()/mics.index() afterward — that would be O(n^2) and
        # would alias two detections that happen to share a timestamp/peak.
        matched.append(MatchedImpulse(tap=taps[tap_i], mic=mics[mic_i]))
        matched_tap_idx.add(tap_i)
        matched_mic_idx.add(mic_i)

    unmatched_tap = [
        taps[i]
        for i in range(len(taps))
        if i not in matched_tap_idx and i not in ambiguous_tap_idx
    ]
    unmatched_mic = [
        mics[i]
        for i in range(len(mics))
        if i not in matched_mic_idx and i not in ambiguous_mic_idx
    ]

    return PairingResult(
        matched=tuple(matched),
        unmatched_tap=tuple(unmatched_tap),
        unmatched_mic=tuple(unmatched_mic),
        ambiguous_tap=tuple(taps[i] for i in sorted(ambiguous_tap_idx)),
        ambiguous_mic=tuple(mics[i] for i in sorted(ambiguous_mic_idx)),
    )


__all__ = [
    "DEFAULT_WINDOW_MS",
    "MatchedImpulse",
    "MicDetection",
    "PairingResult",
    "TapEvent",
    "pair_events",
]
