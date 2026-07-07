# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CamillaDSP <-> jts_ring ALSA negotiation model.

This is intentionally pure: it models the source-derived geometry contract that
the product ring emitter, the jts_ring ioplug, and CamillaDSP v4.1.3 must all
satisfy without opening ALSA or touching /dev/shm.
"""

from __future__ import annotations

from dataclasses import dataclass

from jasper.fanin_coupling import DEFAULT_FANIN_RING_SLOTS, RING_SLOT_FRAMES

# CamillaDSP v4.1.3 (05e9cfc) source constants:
# src/alsa_backend/threaded_buffermanager.rs:
# DeviceBufferManager::calculate_buffer_size requests next_pow2(max(3*chunk,
# 4*min_period)); DeviceBufferManager::apply_period_size then requests
# negotiated_buffer/8 as the period.
# The same request is the reason for scripts/ring-proto/arm-ring-a.sh's lab
# default note around RING_SLOTS: chunk 256 with 128-frame slots negotiates a
# 1024-frame request before the ioplug's fixed 8-slot geometry satisfies it.
_CAMILLA_BUFFER_CHUNK_FACTOR = 3
_CAMILLA_BUFFER_MIN_PERIODS = 4
_CAMILLA_PERIOD_REQUEST_DIVISOR = 8


@dataclass(frozen=True)
class IoplugConstraints:
    """The fixed jts_ring ioplug hardware-parameter space."""

    period_frames: int
    periods: int
    buffer_frames: int

    @property
    def ok(self) -> bool:
        return (
            self.period_frames > 0
            and self.periods > 0
            and self.buffer_frames == self.period_frames * self.periods
        )

    def invalid_reason(self) -> str:
        if self.period_frames <= 0:
            return f"period_frames must be > 0, got {self.period_frames}"
        if self.periods <= 0:
            return f"periods must be > 0, got {self.periods}"
        expected = self.period_frames * self.periods
        return (
            f"buffer_frames={self.buffer_frames} is inconsistent with "
            f"period_frames*periods={expected}"
        )


@dataclass(frozen=True)
class NegotiationOutcome:
    """Requested CamillaDSP values and negotiated ioplug outcome."""

    constraints: IoplugConstraints
    requested_buffer_frames: int
    requested_period_frames: int
    negotiated_buffer_frames: int
    negotiated_period_frames: int
    negotiated_periods: int


def ioplug_constraints(
    *,
    slot_frames: int = RING_SLOT_FRAMES,
    n_slots: int = DEFAULT_FANIN_RING_SLOTS,
) -> IoplugConstraints:
    """Build the ioplug's fixed constraint space from product geometry.

    c/jts-ring-ioplug/pcm_jts_ring.c::jts_ring_set_hw_constraints pins
    PERIOD_BYTES min=max to one slot and PERIODS min=max to n_slots, so the ALSA
    buffer is exactly slot_frames*n_slots. These values are not CamillaDSP
    choices; they are the device space that CamillaDSP negotiates against.
    """

    return IoplugConstraints(
        period_frames=slot_frames,
        periods=n_slots,
        buffer_frames=slot_frames * n_slots,
    )


def camilla_requested_buffer_frames(*, chunksize: int, min_period_frames: int) -> int:
    """CamillaDSP v4.1.3 threaded ALSA buffer request before ALSA clamps it."""

    frames_needed = max(
        _CAMILLA_BUFFER_CHUNK_FACTOR * chunksize,
        _CAMILLA_BUFFER_MIN_PERIODS * min_period_frames,
    )
    return _next_power_of_two(frames_needed)


def negotiate(
    *,
    chunksize: int,
    slot_frames: int = RING_SLOT_FRAMES,
    n_slots: int = DEFAULT_FANIN_RING_SLOTS,
) -> NegotiationOutcome:
    """Model ALSA ``*_near`` negotiation against the jts_ring fixed space."""

    constraints = ioplug_constraints(slot_frames=slot_frames, n_slots=n_slots)
    requested_buffer = camilla_requested_buffer_frames(
        chunksize=chunksize,
        min_period_frames=constraints.period_frames,
    )

    # With jts_ring's min==max constraints, snd_pcm_hw_params_*_near can only
    # return the fixed value (or fail if the space is internally inconsistent).
    negotiated_buffer = constraints.buffer_frames
    requested_period = negotiated_buffer // _CAMILLA_PERIOD_REQUEST_DIVISOR
    return NegotiationOutcome(
        constraints=constraints,
        requested_buffer_frames=requested_buffer,
        requested_period_frames=requested_period,
        negotiated_buffer_frames=negotiated_buffer,
        negotiated_period_frames=constraints.period_frames,
        negotiated_periods=constraints.periods,
    )


def accept(
    outcome: NegotiationOutcome,
    *,
    chunk: int,
    target_level: int,
) -> tuple[bool, str]:
    """Return whether CamillaDSP can run on the negotiated ring geometry.

    Source-derived hard facts for CamillaDSP v4.1.3 (05e9cfc), threaded ALSA:
    - src/alsa_backend/threaded_device.rs::open_pcm applies the buffer and period
      managers, then sw params.
    - threaded_buffermanager.rs::apply_avail_min sets capture/playback ALSA
      avail_min to the negotiated period, not to chunksize.
    - threaded_device.rs::AlsaCaptureDevice::start reads exactly one chunksize
      per inner capture loop and the outer thread assembles exactly one
      chunksize before forwarding it.
    - threaded_device.rs::prime_playback_delay clamps target_level to the
      negotiated playback buffer, so target_level is not a hard accept/reject
      bound (the 8-slot/target-1536 deployed anchor depends on this).

    The source does not contain an explicit "chunksize < buffer" guard. The
    zero-headroom predicate below is the conservative interpretation of that
    threaded read model, anchored by the rejected 2-slot/chunk-256 run. TODO:
    an on-device sweep of chunk sizes between one slot and the full buffer would
    refine whether the true margin must be one frame, one period, or larger.
    """

    del target_level  # documented above; not a hard v4.1.3 threaded-ALSA bound.

    if chunk <= 0:
        return False, f"chunksize must be > 0, got {chunk}"
    if not outcome.constraints.ok:
        return False, outcome.constraints.invalid_reason()

    buffer_frames = outcome.negotiated_buffer_frames
    if chunk > buffer_frames:
        return (
            False,
            f"chunksize {chunk} exceeds the negotiated ALSA buffer "
            f"({buffer_frames} frames)",
        )
    if chunk == buffer_frames:
        return (
            False,
            "chunk == entire buffer: chunksize "
            f"{chunk} leaves zero headroom in the negotiated ALSA buffer",
        )
    return (
        True,
        f"accepted: chunksize {chunk} leaves {buffer_frames - chunk} frames of "
        "negotiated ALSA buffer headroom",
    )


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()
