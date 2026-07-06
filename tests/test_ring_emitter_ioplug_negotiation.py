# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emitter-config ↔ ring-ioplug NEGOTIATION contract (defect B, test-only).

Defect B was resolved EMPIRICALLY on jts.local (2026-07-05): the emitted CamillaDSP
capture config (chunksize 256, from the Apple-dongle latency floor) negotiates CLEAN
against the product Ring-A ioplug (period_frames 128, n_slots 8 → a 1024-frame ALSA
buffer) — `reconcile-current-dsp --force` then camilla active, zero capture errors.
No code change. This is the CONSTRAINT-MATH contract that would catch a FUTURE emitter
or plugin geometry change that breaks that negotiation (e.g. a chunksize bump, a
period_frames change, or a smaller Ring-A slot count).

It tests the MATH, not a mocked pass: every input is parsed from the REAL source of
truth — the Apple floor in ``jasper/audio_hardware/dac.py``, the conf.d in
``deploy/alsa/conf.d/60-jts-ring.conf``, and the ioplug constants in
``c/jts-ring-ioplug/pcm_jts_ring.c`` — and the CamillaDSP capture ``BufferManager``
negotiation formula is applied to them.

THE CONTRACT. The ``jts_ring`` capture ioplug advertises a FIXED period and period
count to CamillaDSP (``snd_pcm_ioplug_set_param_minmax`` with min==max):
  - ``SND_PCM_IOPLUG_HW_PERIOD_BYTES`` = ``period_frames * channels * 2`` (one slot)
  - ``SND_PCM_IOPLUG_HW_PERIODS``      = ``n_slots``
So the ONLY ALSA buffer the ioplug will accept is exactly ``n_slots * period_frames``
frames. CamillaDSP's capture ``BufferManager`` independently negotiates a buffer of
``next_pow2(max(3 * chunksize, 4 * period_frames))`` (documented in
``scripts/ring-proto/arm.sh``, verified on hardware). The negotiation is CLEAN iff
those two agree — otherwise CamillaDSP's requested buffer is rejected by the ioplug's
fixed constraint and the capture open fails (hw_params EINVAL). This test pins that
agreement for the shipped numbers and the general relationship.
"""

from __future__ import annotations

import re
from pathlib import Path

from jasper import ring_assets
from jasper.audio_hardware.dac import by_id


ROOT = Path(__file__).resolve().parents[1]
CONF_D = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
IOPLUG_C = ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"


def _next_pow2(n: int) -> int:
    """Smallest power of two >= n (the CamillaDSP BufferManager rounding)."""
    p = 1
    while p < n:
        p <<= 1
    return p


def _camilla_capture_negotiated_buffer(chunksize: int, period_frames: int) -> int:
    """CamillaDSP's ALSA capture BufferManager negotiated buffer, in frames.

    ``next_pow2(max(3 * chunksize, 4 * min_period))`` — the formula documented in
    scripts/ring-proto/arm.sh and confirmed on hardware (chunksize 256, period 128 ->
    next_pow2(max(768, 512)) = 1024). This is CamillaDSP-side behaviour we can only
    pin by formula (CamillaDSP is a separate binary); the ring-proto arm scripts and
    this contract are the two places the formula lives, and they must agree.
    """
    return _next_pow2(max(3 * chunksize, 4 * period_frames))


def _apple_floor() -> tuple[int, int]:
    """The (chunksize, target_level) the Apple-dongle latency floor pins — the
    numbers the emitter uses on the low-latency ring path. Parsed from the DAC
    registry (the real source), not hardcoded."""
    profile = by_id("apple_usb_c_dongle")
    assert profile is not None, "the Apple dongle profile must exist in the registry"
    floor = profile.latency_floor
    assert floor is not None, "the Apple dongle profile must declare a latency floor"
    return floor.camilla_chunksize, floor.camilla_target_level


def _ioplug_default_period_frames() -> int:
    """The ioplug's compiled default period (JTS_RING_DEFAULT_PERIOD) — the outputd
    DAC-period contract. Parsed from the C source so a change there fails this test."""
    m = re.search(r"#define\s+JTS_RING_DEFAULT_PERIOD\s+(\d+)", IOPLUG_C.read_text())
    assert m is not None, "JTS_RING_DEFAULT_PERIOD must be defined in the ioplug"
    return int(m.group(1))


def _ioplug_advertises_period_and_periods_fixed() -> None:
    """Confirm the ioplug pins BOTH period_bytes and periods as min==max — the
    fixed-geometry premise the whole negotiation contract rests on. If a future
    edit widened either to a range, CamillaDSP could negotiate a buffer this test
    never checked, so pin the fixed-ness explicitly."""
    src = IOPLUG_C.read_text()
    # period_bytes: set_param_minmax(..., HW_PERIOD_BYTES, period_bytes, period_bytes)
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIOD_BYTES,\s*period_bytes,\s*\n?\s*period_bytes",
        src,
    ), "the ioplug must pin PERIOD_BYTES as min==max (one slot), the fixed geometry"
    # periods: set_param_minmax(..., HW_PERIODS, p->n_slots, p->n_slots)
    assert re.search(
        r"SND_PCM_IOPLUG_HW_PERIODS,\s*p->n_slots,\s*\n?\s*p->n_slots",
        src,
    ), "the ioplug must pin PERIODS as min==max (== n_slots), the fixed geometry"


def test_ioplug_geometry_is_fixed_min_equals_max():
    _ioplug_advertises_period_and_periods_fixed()


def test_ring_a_capture_chunksize_negotiates_clean_against_the_product_ring():
    # The shipped numbers: Apple floor chunksize 256, Ring A conf.d n_slots 8 /
    # period_frames 128. The CamillaDSP capture BufferManager must negotiate EXACTLY
    # the buffer the ioplug advertises, or the capture open fails.
    chunksize, _target = _apple_floor()
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))
    assert period_frames is not None and n_slots is not None, (
        "the shipped conf.d must declare a single period_frames + Ring-A n_slots"
    )

    ioplug_buffer = n_slots * period_frames
    camilla_buffer = _camilla_capture_negotiated_buffer(chunksize, period_frames)
    assert camilla_buffer == ioplug_buffer, (
        f"CLEAN-negotiation contract BROKEN: CamillaDSP capture would request a "
        f"{camilla_buffer}-frame buffer (next_pow2(max(3*{chunksize}, 4*{period_frames}))) "
        f"but the Ring-A ioplug pins exactly {ioplug_buffer} frames "
        f"({n_slots} slots * {period_frames}). The capture open would fail hw_params "
        f"EINVAL. Adjust chunksize / n_slots / period_frames so the two agree."
    )
    # The shipped intent, spelled out so a number change is loud.
    assert (chunksize, period_frames, n_slots, ioplug_buffer) == (256, 128, 8, 1024)


def test_chunksize_fits_within_the_advertised_buffer():
    # A necessary sub-constraint: one CamillaDSP read (chunksize) must fit within the
    # ioplug's buffer, and the buffer must be a whole number of chunks (the ioplug
    # period is one slot; chunksize must tile the buffer for clean period wakeups).
    chunksize, _target = _apple_floor()
    period_frames = ring_assets.ring_conf_period_frames(str(CONF_D))
    n_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, str(CONF_D))
    buffer_frames = n_slots * period_frames
    assert chunksize <= buffer_frames, (
        f"chunksize {chunksize} must fit within the {buffer_frames}-frame ring buffer"
    )
    assert buffer_frames % chunksize == 0, (
        f"the {buffer_frames}-frame buffer must be a whole number of {chunksize}-frame "
        "chunks so CamillaDSP's reads align on the ring's period boundaries"
    )
    # chunksize must also be a whole multiple of the ioplug period (one slot), so a
    # CamillaDSP read spans an integer number of ring slots.
    assert chunksize % period_frames == 0, (
        f"chunksize {chunksize} must be a whole multiple of the ring period "
        f"{period_frames} (one slot) for clean slot-aligned reads"
    )


def test_target_level_is_valid_against_chunksize():
    # CamillaDSP's own validity rule (LatencyFloor.__post_init__): target_level must
    # be >= 4 x chunksize so the rate adjuster has headroom. Pin it against the ring
    # numbers so a floor edit that violates it is caught here too (the emitter's
    # `target 1536` from the defect-B live verification).
    chunksize, target_level = _apple_floor()
    assert target_level >= 4 * chunksize, (
        f"target_level {target_level} must be >= 4 x chunksize ({4 * chunksize})"
    )
    assert (chunksize, target_level) == (256, 1536)


def test_a_smaller_ring_a_slot_count_would_break_negotiation():
    # The failure the contract guards, made explicit: if Ring A were narrowed below
    # what the negotiated buffer needs (e.g. a stale JASPER_FANIN_RING_SLOTS shrink),
    # the ioplug's fixed buffer would no longer equal CamillaDSP's negotiated one.
    # This asserts the contract is DISCRIMINATING (not vacuously true for any slots).
    chunksize, _target = _apple_floor()
    period_frames = 128
    camilla_buffer = _camilla_capture_negotiated_buffer(chunksize, period_frames)
    needed_slots = camilla_buffer // period_frames  # 1024 / 128 = 8
    # The shipped 8 slots is the exact fit; 4 slots (512 frames) would NOT match.
    assert (4 * period_frames) != camilla_buffer, (
        "a 4-slot Ring A (512 frames) must NOT satisfy the negotiated 1024-frame "
        "buffer — the contract would (correctly) fail for it"
    )
    assert needed_slots == 8
