# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R6: runner-owned lead-in/lead-out silence padding.

Wraps the artifact emitted by the EXISTING stimulus generators
(``jasper.audio_measurement.playback.ensure_sine_wav`` /
``ensure_bandlimited_noise_wav``) with embedded lead-in/lead-out silence,
WITHOUT modifying those generators. Live playback and offline renders must
share one input identity (R6), so the padding lives in the artifact bytes
themselves, never synthesized at play time.

``stimulus_generator_identity`` (the manifest field) keeps its frozen meaning
— the operator-authorized generator identity. This module's output carries a
SEPARATE identity, ``padded_stimulus_assembly_identity``, computed by the
caller over the padded bytes via the existing
:mod:`jasper.audio_measurement.evidence_identity` /
:mod:`jasper.bass_extension.bench.sink` machinery — this module only produces
the padded bytes and the computed-minima receipt, and performs no bundle I/O
itself (the sink is the only I/O surface in the bench package).

Lead-in/lead-out minima (R6):

* Lead-in >= max(10 * slowest owner-path IIR time constant, longest
  owner-path Conv impulse response length).
* Lead-out >= owner path's total group delay (summed ``Delay`` stage amounts
  + longest Conv impulse response length).

**Time-constant approximation is an explicit engineering judgment call**, not
a value read from existing code or the contract (no prior-art computation of
IIR settling time exists in this repo). For each Biquad/BiquadCombo filter on
the owner path, this module reads whichever frequency-bearing parameter key
is present (``freq``, ``freq_act``, ``freq_target``) and treats the LOWEST
such frequency across all owner-path Biquad/BiquadCombo filters as the
dominant (slowest-settling) pole, approximating its single-pole time constant
as ``1 / (2*pi*f)`` — a standard, conservative (i.e. generously long)
approximation for a single real pole; a true multi-pole cascade's exact
settling time is not computed. A Biquad/BiquadCombo filter with no
recognized frequency-bearing key (e.g. a raw ``Free`` coefficient filter)
REFUSES rather than silently contributing zero — this DSP judgment call
should be sanity-checked by a reviewer against the specific owner-path
filters an actual bench campaign uses.
"""

from __future__ import annotations

import io
import math
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SPEED_OF_SOUND_M_S = 343.0
_FREQ_KEYS: tuple[str, ...] = ("freq", "freq_act", "freq_target")


class StimulusAssemblyError(ValueError):
    """A lead-in/lead-out minimum could not be computed, or padding failed."""


def _biquad_time_constant_s(parameters: Mapping[str, Any]) -> float:
    freqs = [
        float(parameters[key])
        for key in _FREQ_KEYS
        if key in parameters and isinstance(parameters[key], (int, float))
    ]
    if not freqs:
        raise StimulusAssemblyError(
            "owner-path Biquad/BiquadCombo filter has no recognized "
            f"frequency parameter among {_FREQ_KEYS!r}: {dict(parameters)!r}"
        )
    slowest_freq = min(freqs)
    if slowest_freq <= 0.0:
        raise StimulusAssemblyError(
            f"owner-path filter frequency must be positive, got {slowest_freq}"
        )
    return 1.0 / (2.0 * math.pi * slowest_freq)


def _conv_impulse_length_samples(
    parameters: Mapping[str, Any], *, sample_rate_hz: int
) -> int:
    kind = parameters.get("type")
    if kind == "Values":
        values = parameters.get("values")
        if not isinstance(values, list):
            raise StimulusAssemblyError("Conv Values parameters missing 'values' list")
        return len(values)
    if kind == "Dummy":
        length = parameters.get("length")
        if type(length) is not int or length <= 0:
            raise StimulusAssemblyError("Conv Dummy parameters missing positive 'length'")
        return length
    if kind == "Wav":
        filename = parameters.get("filename")
        if not isinstance(filename, str) or not filename:
            raise StimulusAssemblyError("Conv Wav parameters missing 'filename'")
        try:
            with wave.open(filename, "rb") as source:
                return source.getnframes()
        except (OSError, wave.Error) as exc:
            raise StimulusAssemblyError(
                f"could not read Conv Wav coefficient file {filename!r}: {exc}"
            ) from exc
    if kind == "Raw":
        filename = parameters.get("filename")
        if not isinstance(filename, str) or not filename:
            raise StimulusAssemblyError("Conv Raw parameters missing 'filename'")
        fmt = str(parameters.get("format") or "TEXT")
        try:
            data = Path(filename).read_bytes()
        except OSError as exc:
            raise StimulusAssemblyError(
                f"could not read Conv Raw coefficient file {filename!r}: {exc}"
            ) from exc
        if fmt == "TEXT":
            return len([line for line in data.decode("utf-8", errors="strict").splitlines() if line.strip()])
        # CamillaDSP v4.1.3's real FileSampleFormat spellings
        # (src/config/mod.rs) — "S16LE"/"FLOAT64LE" etc. are retired v1/v2
        # names this pinned build's deny_unknown_fields parser rejects.
        bytes_per_sample = {
            "S16_LE": 2,
            "S24_3_LE": 3,
            "S24_4_RJ_LE": 4,
            "S24_4_LJ_LE": 4,
            "S32_LE": 4,
            "F32_LE": 4,
            "F64_LE": 8,
        }.get(fmt)
        if bytes_per_sample is None:
            raise StimulusAssemblyError(f"unrecognized Conv Raw format {fmt!r}")
        return len(data) // bytes_per_sample
    raise StimulusAssemblyError(f"unrecognized Conv parameters type {kind!r}")


def _delay_seconds(parameters: Mapping[str, Any], *, sample_rate_hz: int) -> float:
    delay = parameters.get("delay")
    if not isinstance(delay, (int, float)):
        raise StimulusAssemblyError("Delay filter missing numeric 'delay'")
    unit = parameters.get("unit") or "ms"
    if unit == "ms":
        return float(delay) / 1000.0
    if unit == "us":
        return float(delay) / 1_000_000.0
    if unit == "samples":
        return float(delay) / sample_rate_hz
    if unit == "mm":
        return (float(delay) / 1000.0) / _SPEED_OF_SOUND_M_S
    raise StimulusAssemblyError(f"unrecognized Delay time unit {unit!r}")


@dataclass(frozen=True, slots=True)
class PaddingMinima:
    """R6's computed lead-in/lead-out minima, and the breakdown that produced them."""

    lead_in_samples: int
    lead_out_samples: int
    lead_in_time_constant_component_samples: int
    lead_in_conv_component_samples: int
    lead_out_delay_component_samples: int
    lead_out_conv_component_samples: int

    def to_receipt(self) -> dict[str, Any]:
        return {
            "lead_in_samples": self.lead_in_samples,
            "lead_out_samples": self.lead_out_samples,
            "lead_in_time_constant_component_samples": (
                self.lead_in_time_constant_component_samples
            ),
            "lead_in_conv_component_samples": self.lead_in_conv_component_samples,
            "lead_out_delay_component_samples": self.lead_out_delay_component_samples,
            "lead_out_conv_component_samples": self.lead_out_conv_component_samples,
        }


def compute_padding_minima(
    owner_path_filter_defs: Sequence[Mapping[str, Any]],
    *,
    sample_rate_hz: int,
) -> PaddingMinima:
    """R6: compute the lead-in/lead-out minima for the owner path's filters.

    ``owner_path_filter_defs`` is the sequence of ``{"type": ..., "parameters":
    {...}}`` filter definitions for every Biquad/BiquadCombo/Conv/Delay filter
    named on :func:`jasper.bass_extension.bench.derivation.owner_path_stages`
    (Gain/Limiter/Mixer contribute no delay/settling component).
    """

    if sample_rate_hz <= 0:
        raise StimulusAssemblyError("sample_rate_hz must be positive")

    slowest_time_constant_s = 0.0
    longest_conv_samples = 0
    total_delay_s = 0.0
    for definition in owner_path_filter_defs:
        ftype = definition.get("type")
        parameters = definition.get("parameters")
        parameters = parameters if isinstance(parameters, Mapping) else {}
        if ftype in ("Biquad", "BiquadCombo"):
            slowest_time_constant_s = max(
                slowest_time_constant_s, _biquad_time_constant_s(parameters)
            )
        elif ftype == "Conv":
            longest_conv_samples = max(
                longest_conv_samples,
                _conv_impulse_length_samples(parameters, sample_rate_hz=sample_rate_hz),
            )
        elif ftype == "Delay":
            total_delay_s += _delay_seconds(parameters, sample_rate_hz=sample_rate_hz)
        # Gain / Limiter contribute no delay/settling component.

    time_constant_samples = math.ceil(10.0 * slowest_time_constant_s * sample_rate_hz)
    lead_in = max(time_constant_samples, longest_conv_samples)
    delay_samples = math.ceil(total_delay_s * sample_rate_hz)
    lead_out = max(delay_samples, longest_conv_samples)
    return PaddingMinima(
        lead_in_samples=lead_in,
        lead_out_samples=lead_out,
        lead_in_time_constant_component_samples=time_constant_samples,
        lead_in_conv_component_samples=longest_conv_samples,
        lead_out_delay_component_samples=delay_samples,
        lead_out_conv_component_samples=longest_conv_samples,
    )


@dataclass(frozen=True, slots=True)
class PaddedStimulus:
    """The padded WAV bytes plus the frame counts that locate the stimulus body."""

    wav_bytes: bytes
    lead_in_frames: int
    lead_out_frames: int
    body_frames: int
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int


def pad_stimulus_wav(source_wav_path: Path, *, minima: PaddingMinima) -> PaddedStimulus:
    """Prepend/append silence to the generator's WAV, embedding R6's padding.

    Reads the existing generator's output (never modifies it) and writes a new
    in-memory WAV: ``lead_in`` silent frames + the original body + ``lead_out``
    silent frames, in the SAME channel count / sample width / frame rate.
    """

    with wave.open(str(source_wav_path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        frame_rate = source.getframerate()
        body_frames = source.getnframes()
        body = source.readframes(body_frames)

    silence_frame = b"\x00" * (channels * sample_width)
    lead_in_bytes = silence_frame * minima.lead_in_samples
    lead_out_bytes = silence_frame * minima.lead_out_samples

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sample_width)
        out.setframerate(frame_rate)
        out.writeframes(lead_in_bytes + body + lead_out_bytes)

    return PaddedStimulus(
        wav_bytes=buffer.getvalue(),
        lead_in_frames=minima.lead_in_samples,
        lead_out_frames=minima.lead_out_samples,
        body_frames=body_frames,
        sample_rate_hz=frame_rate,
        channels=channels,
        sample_width_bytes=sample_width,
    )


__all__ = [
    "PaddedStimulus",
    "PaddingMinima",
    "StimulusAssemblyError",
    "compute_padding_minima",
    "pad_stimulus_wav",
]
