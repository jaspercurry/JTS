# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-usbsink.service contract: it MUST ride the zram-shielded audio tier.

usbsink is a real-time music SOURCE — its capture/playback run on fixed-deadline
(~10 ms) PortAudio callbacks. Diagnosed on 2026-06-28: usbsink was the only
music-path daemon left OUTSIDE ``jts-audio.slice``, so its pages could swap to
zram and zram-decompression jitter made the callback miss deadlines in bursts ->
snd-aloop xruns -> the bounded queue overflowed (``dropped_full``), the dominant
cause of the bursty USB-drop tail. Slice membership (the ``MemorySwapMax=0`` swap
shield) is a MEMORY policy, not a CPU cap, so it respects the no-CPU-caps rule.

This pins the membership + the OOM band so a future unit edit can't silently drop
usbsink off the audio tier (the regression that caused the drops).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbsink.service"


def _value_for(unit_text: str, key: str) -> str | None:
    """Last value for ``key=`` in the unit's [Service] section (last wins, as
    systemd resolves it). Ignores comment lines."""
    val: str | None = None
    for ln in unit_text.splitlines():
        s = ln.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            val = v.strip()
    return val


def test_unit_file_exists():
    assert UNIT_PATH.exists(), f"jasper-usbsink.service missing at {UNIT_PATH}"


def test_rides_zram_shielded_audio_slice():
    # The swap shield is the load-bearing fix for the bursty USB-drop tail.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "Slice") == "jts-audio.slice", (
        "jasper-usbsink.service must set Slice=jts-audio.slice (MemorySwapMax=0 "
        "swap shield) — off it, zram jitter makes the RT callback miss deadlines "
        "and the queue overflows. See the 2026-06-28 USB-drop diagnosis."
    )


def test_oom_band_below_the_output_chain():
    # A killed source restarts and only USB stops; a killed output (outputd -950
    # / camilla -900 / fanin -800) stops all audio — so usbsink sits ABOVE them
    # (less negative = less protected), but still protected vs the default 0.
    body = UNIT_PATH.read_text()
    raw = _value_for(body, "OOMScoreAdjust")
    assert raw is not None, "jasper-usbsink.service must set OOMScoreAdjust"
    val = int(raw)
    assert -800 < val < 0, (
        f"usbsink OOMScoreAdjust={val} must be negative (protected) but above the "
        f"output chain's -800 (fanin) — a music source is less critical than output."
    )


def test_no_unit_level_rt_scheduling():
    # Unit-level CPUSchedulingPolicy=fifo is what SIGKILL-crash-looped the AEC
    # bridge on 2026-06-27; RT priority, if ever needed, is elected in-thread
    # after start(). Guard against it regressing into this unit.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "CPUSchedulingPolicy") is None, (
        "Do NOT set CPUSchedulingPolicy at the unit level (AEC-bridge crash-loop "
        "2026-06-27). Elect RT in-thread after start() if ever required."
    )
