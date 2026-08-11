# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test-only CamillaDSP <-> jts_ring ALSA negotiation model.

It models the source-derived geometry contract that the product ring emitter,
the jts_ring ioplug, and CamillaDSP v4.1.3 must all satisfy without opening
ALSA or touching /dev/shm. Production code does not import this model; the
contract suite owns it next to the assertions it supports.

The pinned device quantity is BYTES, not frames: the ioplug pins PERIOD_BYTES
min==max, and the frame count that follows from it depends on the wire
(``period_bytes / (channels * bytes_per_sample)``). Both wire axes are per-box —
:func:`jasper.fanin_coupling.resolve_ring_wire` answers the format and Ring B's
channel count — so the model carries them rather than answering the same frames
for rings that are 8x apart in bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

from jasper.fanin_coupling import (
    DEFAULT_FANIN_RING_SLOTS,
    RING_A_CHANNELS,
    RING_SLOT_FRAMES,
    RING_WIRE_FORMAT,
    RING_WIRE_FORMAT_WIDE,
    RING_WIRE_FORMATS,
)

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

# Bytes per sample for the two tokens the ring wire vocabulary defines, mirroring
# c/jts-ring-ioplug/jts_ring_shm.c::jts_ring_bytes_per_sample. Keyed by the ALSA
# token jasper.fanin_coupling owns rather than by the header's sample_format id,
# because the token is what the ioplug conf.d block declares and what this model
# is handed. A token outside this map has no stride here: the C default-arm's
# fallback to 2 is a defensive branch behind jts_ring_geometry_validate's
# two-format accept-set, not a width claim this model may repeat.
_RING_BYTES_PER_SAMPLE = {
    RING_WIRE_FORMAT: 2,
    RING_WIRE_FORMAT_WIDE: 4,
}


@dataclass(frozen=True)
class IoplugConstraints:
    """The fixed jts_ring ioplug hardware-parameter space.

    ``period_frames`` / ``periods`` / ``buffer_frames`` are the frame view;
    ``sample_format`` and ``channels`` are the wire that turns it into the BYTE
    quantity the ioplug actually pins. The byte properties are derived from that
    one wire, so the two views cannot disagree — what the space CAN fail on is a
    wire whose stride has no answer here (a format outside the ring's two, or a
    non-positive channel count).
    """

    period_frames: int
    periods: int
    buffer_frames: int
    sample_format: str
    channels: int

    @property
    def bytes_per_frame(self) -> int:
        """Bytes per interleaved frame; 0 for a wire this model cannot size.

        c/jts-ring-ioplug/pcm_jts_ring.c::frame_bytes —
        ``jts_ring_bytes_per_sample(sample_format) * channels``.
        """
        if self.channels <= 0:
            return 0
        return _RING_BYTES_PER_SAMPLE.get(self.sample_format, 0) * self.channels

    @property
    def period_bytes(self) -> int:
        """The pinned PERIOD_BYTES: one slot on this wire."""
        return self.period_frames * self.bytes_per_frame

    @property
    def buffer_bytes(self) -> int:
        """The whole ALSA buffer on this wire: ``periods`` slots."""
        return self.buffer_frames * self.bytes_per_frame

    @property
    def ok(self) -> bool:
        return (
            self.period_frames > 0
            and self.periods > 0
            and self.bytes_per_frame > 0
            and self.buffer_frames == self.period_frames * self.periods
        )

    def invalid_reason(self) -> str:
        if self.period_frames <= 0:
            return f"period_frames must be > 0, got {self.period_frames}"
        if self.periods <= 0:
            return f"periods must be > 0, got {self.periods}"
        if self.channels <= 0:
            return f"channels must be > 0, got {self.channels}"
        if self.sample_format not in _RING_BYTES_PER_SAMPLE:
            return (
                f"sample_format {self.sample_format!r} is not a ring wire format "
                f"({', '.join(RING_WIRE_FORMATS)}), so PERIOD_BYTES has no value"
            )
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
    sample_format: str = RING_WIRE_FORMAT,
    channels: int = RING_A_CHANNELS,
) -> IoplugConstraints:
    """Build the ioplug's fixed constraint space from product geometry.

    c/jts-ring-ioplug/pcm_jts_ring.c::jts_ring_set_hw_constraints pins
    PERIOD_BYTES min=max to ``slot_frames * frame_bytes`` and PERIODS min=max to
    n_slots, so the ALSA buffer is exactly slot_frames*n_slots frames. FORMAT and
    CHANNELS are pinned to single values in the same call, which is what lets the
    one pinned byte count name one frame count: the frames that follow from
    PERIOD_BYTES depend on the wire, and an S32_LE/8ch ring is 8x an S16_LE/2ch
    ring in bytes at identical frames. The defaults are the shipped wire. These
    values are not CamillaDSP choices; they are the device space that CamillaDSP
    negotiates against.
    """

    return IoplugConstraints(
        period_frames=slot_frames,
        periods=n_slots,
        buffer_frames=slot_frames * n_slots,
        sample_format=sample_format,
        channels=channels,
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
    sample_format: str = RING_WIRE_FORMAT,
    channels: int = RING_A_CHANNELS,
) -> NegotiationOutcome:
    """Model ALSA ``*_near`` negotiation against the jts_ring fixed space."""

    constraints = ioplug_constraints(
        slot_frames=slot_frames,
        n_slots=n_slots,
        sample_format=sample_format,
        channels=channels,
    )
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
