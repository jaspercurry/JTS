# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R6a (ingress transparency) and R4(a) (fader bracket) — pure predicates.

Every function here operates on ALREADY-FETCHED status payloads (mux STATUS,
fan-in STATUS, an :class:`~jasper.bass_extension.bench.derivation.ArtifactHeader`,
and read main-volume/mute pairs) — the live I/O (``_mux_socket_command``,
``jasper.fanin.status.read_fanin_status``, ``CamillaController.get_volume_and_mute``)
is the executor's concern, not this module's. This keeps every proof here
hardware-free testable by construction.

R6a's element (i) reuses
``jasper.correction.coordinator._measurement_gate_held`` verbatim — the exact
predicate the amendment names — rather than re-deriving the mux-label /
gate-owner comparison. Elements (ii)-(iv) are this module's own, over the
fan-in STATUS field names verified against
``rust/jasper-fanin/src/state.rs`` / ``mixer.rs``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jasper.correction.coordinator import _measurement_gate_held

from .derivation import ArtifactHeader

# rust/jasper-fanin/src/mixer.rs: `pub const CHANNELS: u32 = 2;` /
# `pub const FORMAT: Format = Format::S16LE;` — the mix is always 2-channel
# S16LE, regardless of any one lane's own source geometry.
FANIN_MIX_CHANNELS = 2
FANIN_MIX_BITS_PER_SAMPLE = 16

# jasper/correction/coordinator.py: the bench runner reuses measurement_window()
# unchanged, so it reuses this exact fan-in label too — there is no
# caller-specific owner/label parameter on measurement_window().
MEASUREMENT_FANIN_LABEL = "correction"


class IngressProofError(ValueError):
    """One of R6a's four ingress-transparency elements could not be proved."""


class FaderDriftError(ValueError):
    """R4(a): the main fader or mute state drifted during a stimulus role's
    playback."""


def prove_isolation(mux_status: Mapping[str, Any]) -> None:
    """R6a element (i): mux STATUS confirms the isolated measurement lane."""

    if not _measurement_gate_held(dict(mux_status)):
        raise IngressProofError(
            "mux STATUS does not confirm test_source == active_source == "
            f"{MEASUREMENT_FANIN_LABEL!r} with the bench gate owner held"
        )


def _measurement_lane(
    fanin_status: Mapping[str, Any], *, label: str = MEASUREMENT_FANIN_LABEL
) -> Mapping[str, Any]:
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        raise IngressProofError("fan-in STATUS has no 'inputs' list")
    for entry in inputs:
        if isinstance(entry, Mapping) and entry.get("label") == label:
            return entry
    raise IngressProofError(f"fan-in STATUS has no {label!r} lane")


def prove_lane_state(fanin_status: Mapping[str, Any]) -> None:
    """R6a element (ii): the measurement lane is unmuted, unarmed, untrimmed."""

    lane = _measurement_lane(fanin_status)
    if lane.get("muted") is not False:
        raise IngressProofError("measurement lane is not muted:false")
    resampler = lane.get("resampler")
    if isinstance(resampler, Mapping) and resampler.get("armed") is True:
        raise IngressProofError("measurement lane resampler.armed is true")
    trim = lane.get("trim")
    if not isinstance(trim, Mapping) or trim.get("pending") is not False:
        raise IngressProofError("measurement lane trim.pending is not false")


def prove_lane_counters_unchanged(
    status_start: Mapping[str, Any], status_end: Mapping[str, Any]
) -> None:
    """R6a element (ii): xrun/catch-up counters unchanged across the pass."""

    before = _measurement_lane(status_start)
    after = _measurement_lane(status_end)
    for key in ("xrun_count", "catchup_resync_frames", "catchup_events"):
        if before.get(key) != after.get(key):
            raise IngressProofError(
                f"measurement lane {key} changed during the pass "
                f"({before.get(key)!r} -> {after.get(key)!r})"
            )


def prove_no_foreign_audio(
    status_start: Mapping[str, Any], status_end: Mapping[str, Any]
) -> None:
    """R6a element (iii): no duck / pending / dropped / flushed TTS activity."""

    for status in (status_start, status_end):
        tts = status.get("tts")
        if not isinstance(tts, Mapping):
            raise IngressProofError("fan-in STATUS has no 'tts' block")
        if tts.get("program_duck_active") is not False:
            raise IngressProofError("tts.program_duck_active is not false")
        if tts.get("pending_frames") != 0:
            raise IngressProofError("tts.pending_frames is not zero")
    tts_before = status_start["tts"]
    tts_after = status_end["tts"]
    for key in ("flushed_frames", "dropped_audio_frames"):
        if tts_before.get(key) != tts_after.get(key):
            raise IngressProofError(
                f"tts.{key} changed during the pass "
                f"({tts_before.get(key)!r} -> {tts_after.get(key)!r})"
            )


def prove_bit_width_geometry(
    *, artifact_header: ArtifactHeader, fanin_status: Mapping[str, Any]
) -> None:
    """R6a element (iv): the stimulus artifact is 16-bit at the lane's rate/channels."""

    output = fanin_status.get("output")
    if not isinstance(output, Mapping):
        raise IngressProofError("fan-in STATUS has no 'output' block")
    lane_rate = output.get("sample_rate")
    if artifact_header.bits_per_sample != FANIN_MIX_BITS_PER_SAMPLE:
        raise IngressProofError(
            f"stimulus artifact is {artifact_header.bits_per_sample}-bit, not "
            f"{FANIN_MIX_BITS_PER_SAMPLE}-bit PCM"
        )
    if artifact_header.channels != FANIN_MIX_CHANNELS:
        raise IngressProofError(
            f"stimulus artifact has {artifact_header.channels} channels, fan-in "
            f"mixes {FANIN_MIX_CHANNELS}"
        )
    if artifact_header.sample_rate_hz != lane_rate:
        raise IngressProofError(
            f"stimulus artifact sample rate {artifact_header.sample_rate_hz} does "
            f"not match fan-in STATUS output.sample_rate {lane_rate!r}"
        )


@dataclass(frozen=True, slots=True)
class IngressProofInputs:
    """Everything R6a's four elements need, already fetched by the executor."""

    mux_status: Mapping[str, Any]
    fanin_status_start: Mapping[str, Any]
    fanin_status_end: Mapping[str, Any]
    artifact_header: ArtifactHeader


def prove_ingress_transparency(inputs: IngressProofInputs) -> None:
    """R6a: prove all four elements; raise :class:`IngressProofError` on the first miss."""

    prove_isolation(inputs.mux_status)
    prove_lane_state(inputs.fanin_status_start)
    prove_lane_state(inputs.fanin_status_end)
    prove_lane_counters_unchanged(inputs.fanin_status_start, inputs.fanin_status_end)
    prove_no_foreign_audio(inputs.fanin_status_start, inputs.fanin_status_end)
    prove_bit_width_geometry(
        artifact_header=inputs.artifact_header, fanin_status=inputs.fanin_status_start
    )


@dataclass(frozen=True, slots=True)
class FaderBracket:
    """R4(a)'s pre/post-playback main-fader + mute reads."""

    before_db: float
    before_muted: bool
    after_db: float
    after_muted: bool


def prove_fader_bracket(bracket: FaderBracket) -> None:
    """R4(a): mute must be false; the fader must not drift during playback."""

    if bracket.before_muted or bracket.after_muted:
        raise FaderDriftError(
            "main fader is muted around this stimulus role's playback "
            f"(before={bracket.before_muted}, after={bracket.after_muted})"
        )
    if bracket.before_db != bracket.after_db:
        raise FaderDriftError(
            "main fader drifted during this stimulus role's playback "
            f"({bracket.before_db} dB -> {bracket.after_db} dB)"
        )


__all__ = [
    "FANIN_MIX_BITS_PER_SAMPLE",
    "FANIN_MIX_CHANNELS",
    "FaderBracket",
    "FaderDriftError",
    "IngressProofError",
    "IngressProofInputs",
    "MEASUREMENT_FANIN_LABEL",
    "prove_bit_width_geometry",
    "prove_fader_bracket",
    "prove_ingress_transparency",
    "prove_isolation",
    "prove_lane_counters_unchanged",
    "prove_lane_state",
    "prove_no_foreign_audio",
]
