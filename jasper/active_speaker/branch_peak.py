# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What peak ONE driver's branch actually receives for ONE stimulus.

The un-segmented volume ceiling
(:func:`jasper.active_speaker.session_volume_plan.unsegmented_stimulus_ceiling_db`)
has always had to assume every driver sees the whole stimulus peak, because it
had no way to know what a crossed-over branch really gets. On a two-way that is
wrong by the crossover: a full-band program reaches the tweeter through a
high-pass, the woofer through a low-pass, and neither through the input split
mixer at unity. The gap is not small — measured on JTS3's basin-2 graph against
its session check program (2026-08-19), the binding branch sat 10.7 dB under the
full-band peak — and it was paid as a speaker refusing to reach a 75-80 dB SPL
seat target it is physically able to reach. The magnitude is a property of one
stimulus through one graph, never a constant.

This module closes it by RENDERING: take the actual stimulus WAV, push it
through the actual applied CamillaDSP graph, and report each output channel's
true peak with the main fader at 0 dB. The answer is a claim about one WAV
through one graph and is never reusable — a different stimulus, or the same
stimulus after the graph is re-applied, needs a fresh render.

**It refuses rather than approximates.** Every raise here becomes the caller's
fall back to the conservative full-band bound, so an unmodelled graph makes the
speaker quieter, never louder. Nothing is skipped silently: a filter type this
module does not model exactly is a refusal, not a no-op, because a skipped
attenuator would under-report a branch peak and that is the one direction that
raises a ceiling unsafely.

**One biquad evaluator, and this is not a second one.**
:func:`jasper.active_speaker.branch_chain.chain_response` and
:func:`~jasper.active_speaker.branch_chain.crossover_response_complex` — which
bottom out in :func:`jasper.sound.profile._filter_response_complex`, the
codebase's single RBJ evaluator — supply every filter response. This module owns
only the pipeline walk and the overlap-save render around them, so the graph it
models is bit-for-bit the graph the emitter charges and the runtime contract
proves.

**Why not** :class:`jasper.active_speaker.graph_safety.GraphView`: that view
drops ``Mixer`` pipeline steps by construction (it keeps only ``Filter`` steps),
and the input split mixer is both a real gain — -6.02 dB per leg — and the
boundary where program channels become driver channels. A branch peak cannot be
derived without it, so the walk below reads the config mapping directly.

**The 48 kHz constraint is inherited, not invented.** The shared evaluator
prewarps at :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`, so a graph or a
stimulus at any other rate is refused rather than modelled at the wrong corner
frequencies.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

# Overlap-save geometry. The block is what the shared evaluator is sampled
# across (a Python-level loop, so a coarse-enough block keeps the render an
# operator-CLI cost); the overlap is the tail every block discards, and is
# therefore also the longest branch delay + filter ringing this render can model
# without wraparound. 683 ms and 171 ms at 48 kHz: an LR4 rings for a few
# milliseconds and a driver alignment delay is well under one, so both are
# generous by orders of magnitude — and a graph that exceeds the overlap is
# refused below rather than modelled wrong.
_BLOCK_SAMPLES = 32768
_OVERLAP_SAMPLES = 8192

# The longest stimulus this will render. A seat-leveling stimulus is seconds
# long; ten minutes at 48 kHz is far past any legitimate one and is the bound
# that keeps an accidental hour-long WAV from putting a 1 GB Pi under memory
# pressure. Over it, the caller falls back to the conservative bound.
_MAX_STIMULUS_SAMPLES = 48_000 * 600

# CamillaDSP filter types this module models EXACTLY. Anything else refuses.
_MODELLED_FILTER_TYPES = frozenset({"Biquad", "BiquadCombo", "Gain", "Delay", "Limiter"})

# Biquad shapes the shared RBJ evaluator implements (jasper.sound.profile.
# _biquad_coeffs). A shape outside this set would fall through that function's
# `Peaking` default and be modelled as the wrong filter, so it refuses instead.
_MODELLED_BIQUAD_TYPES = frozenset(
    {"Lowpass", "Highpass", "Notch", "Lowshelf", "Highshelf", "Peaking"}
)

# BiquadCombo shapes branch_chain.crossover_response_complex realises.
_MODELLED_COMBO_TYPES = {
    "LinkwitzRileyHighpass": True,
    "LinkwitzRileyLowpass": False,
}

_DELAY_UNIT_SECONDS = {
    "ms": 1e-3,
    "s": 1.0,
}


class BranchPeakError(RuntimeError):
    """This graph or stimulus cannot be rendered exactly.

    Always fail-conservative at the call site: the caller drops back to a bound
    that assumes every driver sees the full stimulus peak.
    """


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BranchPeakError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise BranchPeakError(f"{what} must be finite, got {value!r}")
    return number


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BranchPeakError(f"{what} is not a mapping")
    return value


def read_stimulus_samples(wav_path: str | Path) -> tuple[Any, int]:
    """``(float64 samples shaped (frames, channels), sample_rate_hz)``.

    Integer PCM is normalised by its dtype's own maximum — the SAME convention
    :func:`jasper.cli.seat_level.stimulus_peak_dbfs` uses, so a branch peak and
    the full-band peak it is compared against are on one scale by construction
    rather than by coincidence.
    """
    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(str(wav_path))
    array = np.asarray(data)
    if array.ndim == 1:
        array = array[:, None]
    if array.ndim != 2:
        raise BranchPeakError(f"{wav_path} is not a mono or multichannel WAV")
    samples = array.astype(np.float64)
    if np.issubdtype(array.dtype, np.integer):
        samples = samples / float(np.iinfo(array.dtype).max)
    if samples.shape[0] > _MAX_STIMULUS_SAMPLES:
        raise BranchPeakError(
            f"{wav_path} carries {samples.shape[0]} frames, past the "
            f"{_MAX_STIMULUS_SAMPLES}-frame render bound"
        )
    if not samples.size:
        raise BranchPeakError(f"{wav_path} carries no frames")
    return samples, int(rate)


def _filter_records(
    names: Sequence[str], filters: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[Any], complex, float]:
    """One pipeline step's names reduced to ``(biquads, sections, scale, delay_s)``.

    A transfer function is a product, so the four accumulators may be collected
    in any order and multiplied once — which is also what keeps the shared
    evaluator to a handful of calls per branch instead of one per filter.
    """
    from jasper.active_speaker.branch_chain import CrossoverSection

    biquads: list[dict[str, Any]] = []
    sections: list[Any] = []
    scale = 1.0 + 0.0j
    delay_s = 0.0
    for name in names:
        spec = filters.get(name)
        if not isinstance(spec, Mapping):
            raise BranchPeakError(f"pipeline names filter {name!r}, which is undefined")
        kind = str(spec.get("type") or "")
        if kind not in _MODELLED_FILTER_TYPES:
            raise BranchPeakError(f"filter {name!r} is a {kind or 'typeless'} filter")
        params = _mapping(spec.get("parameters"), f"filter {name!r} parameters")
        if kind == "Limiter":
            # Deliberately modelled as a pass-through. A limiter is the one
            # non-linear stage here and it can only ever REDUCE a peak, so
            # ignoring it over-reports the branch and binds the ceiling tighter
            # — the safe direction. Modelling its soft-clip curve would only
            # ever raise the ceiling, which is not a trade worth taking.
            continue
        if kind == "Gain":
            if params.get("mute") is True:
                scale = 0.0 + 0.0j
                continue
            gain_scale = str(params.get("scale") or "dB")
            gain = _finite(params.get("gain"), f"filter {name!r} gain")
            if gain_scale in ("dB", "decibel"):
                linear = 10.0 ** (gain / 20.0)
            elif gain_scale == "linear":
                linear = gain
            else:
                raise BranchPeakError(
                    f"filter {name!r} uses gain scale {gain_scale!r}"
                )
            if params.get("inverted") is True:
                linear = -linear
            scale = scale * linear
            continue
        if kind == "Delay":
            unit = str(params.get("unit") or "ms")
            if unit == "samples":
                seconds = (
                    _finite(params.get("delay"), f"filter {name!r} delay")
                    / float(RESPONSE_SAMPLE_RATE_HZ)
                )
            elif unit in _DELAY_UNIT_SECONDS:
                seconds = (
                    _finite(params.get("delay"), f"filter {name!r} delay")
                    * _DELAY_UNIT_SECONDS[unit]
                )
            else:
                raise BranchPeakError(f"filter {name!r} delays in {unit!r}")
            delay_s += seconds
            continue
        if kind == "BiquadCombo":
            combo = str(params.get("type") or "")
            if combo not in _MODELLED_COMBO_TYPES:
                raise BranchPeakError(f"filter {name!r} is a {combo!r} combo")
            order = params.get("order")
            if isinstance(order, bool) or not isinstance(order, int) or order < 1:
                raise BranchPeakError(f"filter {name!r} has order {order!r}")
            sections.append(
                CrossoverSection(
                    fc_hz=_finite(params.get("freq"), f"filter {name!r} freq"),
                    order=int(order),
                    highpass=_MODELLED_COMBO_TYPES[combo],
                )
            )
            continue
        # Biquad
        shape = str(params.get("type") or "")
        if shape not in _MODELLED_BIQUAD_TYPES:
            raise BranchPeakError(f"filter {name!r} is a {shape!r} biquad")
        biquads.append(
            {
                "biquad_type": shape,
                "freq": _finite(params.get("freq"), f"filter {name!r} freq"),
                "q": float(params.get("q") or 0.0),
                "gain": float(params.get("gain") or 0.0),
            }
        )
    return biquads, sections, scale, delay_s


def _step_transfer(
    names: Sequence[str], filters: Mapping[str, Any], freqs: Any
) -> Any:
    """The complex response one pipeline step applies across ``freqs``."""
    import numpy as np

    from jasper.active_speaker.branch_chain import (
        chain_response,
        crossover_response_complex,
    )

    biquads, sections, scale, delay_s = _filter_records(names, filters)
    if delay_s * RESPONSE_SAMPLE_RATE_HZ > _OVERLAP_SAMPLES:
        raise BranchPeakError(
            f"a step delays {delay_s * 1e3:.1f} ms, past the "
            f"{_OVERLAP_SAMPLES / RESPONSE_SAMPLE_RATE_HZ * 1e3:.1f} ms render overlap"
        )
    response = np.full(freqs.shape, scale, dtype=np.complex128)
    if biquads:
        response = response * chain_response(biquads, freqs)
    if sections:
        response = response * crossover_response_complex(freqs, sections)
    if delay_s:
        response = response * np.exp(-2j * np.pi * freqs * delay_s)
    return response


def _pipeline_operations(
    config: Mapping[str, Any], freqs: Any, capture_channels: int
) -> tuple[list[tuple[str, Any]], int]:
    """The applied pipeline reduced to ordered spectrum operations.

    Returns the operations plus the channel count the pipeline ends with — the
    playback width the requested output indexes are validated against.
    """
    filters = config.get("filters")
    filters = filters if isinstance(filters, Mapping) else {}
    mixers = config.get("mixers")
    mixers = mixers if isinstance(mixers, Mapping) else {}
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, list):
        raise BranchPeakError("the applied config carries no pipeline list")

    operations: list[tuple[str, Any]] = []
    width = int(capture_channels)
    for index, step in enumerate(pipeline):
        if not isinstance(step, Mapping):
            raise BranchPeakError(f"pipeline step {index} is not a mapping")
        if step.get("bypassed") is True:
            # A bypassed step is inert in CamillaDSP, but reading it as inert
            # here means trusting a second semantics to stay true; refusing
            # costs a fallback to the conservative bound and nothing else.
            raise BranchPeakError(f"pipeline step {index} is bypassed")
        kind = str(step.get("type") or "")
        if kind == "Filter":
            channels = _step_channels(step, width, index)
            names = [str(name) for name in step.get("names") or [] if name is not None]
            if not names:
                continue
            operations.append(
                ("filter", (channels, _step_transfer(names, filters, freqs)))
            )
            continue
        if kind == "Mixer":
            name = str(step.get("name") or "")
            mixer = mixers.get(name)
            if not isinstance(mixer, Mapping):
                raise BranchPeakError(f"pipeline step {index} names mixer {name!r}")
            width, mapping = _mixer_mapping(mixer, width, name)
            operations.append(("mixer", (width, mapping)))
            continue
        raise BranchPeakError(f"pipeline step {index} is a {kind or 'typeless'} step")
    return operations, width


def _step_channels(step: Mapping[str, Any], width: int, index: int) -> tuple[int, ...]:
    """A Filter step's channel list, in either CamillaDSP spelling.

    Accepts the ``channels: [..]`` list the JTS emitter writes and the scalar
    ``channel: N`` sugar CamillaDSP's own read-back adds — the same two dialects
    :func:`jasper.active_speaker.graph_safety.view_from_camilla_dict` reads. An
    absent channel selector means "every channel", which is CamillaDSP's own
    default and is how a pre-split stereo step is spelled in the readback.
    """
    raw = step.get("channels")
    if isinstance(raw, list):
        found = [c for c in raw if isinstance(c, int) and not isinstance(c, bool)]
        if len(found) != len(raw):
            raise BranchPeakError(f"pipeline step {index} has a non-integer channel")
    elif isinstance(raw, int) and not isinstance(raw, bool):
        found = [int(raw)]
    else:
        channel = step.get("channel")
        if isinstance(channel, int) and not isinstance(channel, bool):
            found = [int(channel)]
        elif raw is None and channel is None:
            found = list(range(width))
        else:
            raise BranchPeakError(f"pipeline step {index} has no readable channels")
    for channel in found:
        if not 0 <= channel < width:
            raise BranchPeakError(
                f"pipeline step {index} touches channel {channel} of {width}"
            )
    return tuple(found)


def _mixer_mapping(
    mixer: Mapping[str, Any], width: int, name: str
) -> tuple[int, list[tuple[int, list[tuple[int, complex]]]]]:
    """``(out_width, [(dest, [(source, complex gain), ...]), ...])``."""
    channels = _mapping(mixer.get("channels"), f"mixer {name!r} channels")
    counts: dict[str, int] = {}
    for what in ("in", "out"):
        value = channels.get(what)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise BranchPeakError(f"mixer {name!r} has channels.{what} {value!r}")
        counts[what] = int(value)
    channels_in, channels_out = counts["in"], counts["out"]
    if channels_in != width:
        raise BranchPeakError(
            f"mixer {name!r} takes {channels_in} channels where the pipeline "
            f"carries {width}"
        )
    mapping_raw = mixer.get("mapping")
    if not isinstance(mapping_raw, list):
        raise BranchPeakError(f"mixer {name!r} carries no mapping list")
    mapping: list[tuple[int, list[tuple[int, complex]]]] = []
    for entry in mapping_raw:
        entry = _mapping(entry, f"mixer {name!r} mapping entry")
        if entry.get("mute") is True:
            continue
        dest = entry.get("dest")
        if isinstance(dest, bool) or not isinstance(dest, int):
            raise BranchPeakError(f"mixer {name!r} has dest {dest!r}")
        if not 0 <= int(dest) < channels_out:
            raise BranchPeakError(f"mixer {name!r} maps to dest {dest} out of range")
        sources_raw = entry.get("sources")
        if not isinstance(sources_raw, list):
            raise BranchPeakError(f"mixer {name!r} dest {dest} carries no sources")
        sources: list[tuple[int, complex]] = []
        for source in sources_raw:
            source = _mapping(source, f"mixer {name!r} source")
            if source.get("mute") is True:
                continue
            channel = source.get("channel")
            if isinstance(channel, bool) or not isinstance(channel, int):
                raise BranchPeakError(f"mixer {name!r} has source channel {channel!r}")
            if not 0 <= int(channel) < width:
                raise BranchPeakError(
                    f"mixer {name!r} reads channel {channel} of {width}"
                )
            gain_scale = str(source.get("scale") or "dB")
            gain = _finite(source.get("gain"), f"mixer {name!r} source gain")
            if gain_scale in ("dB", "decibel"):
                linear = 10.0 ** (gain / 20.0)
            elif gain_scale == "linear":
                linear = gain
            else:
                raise BranchPeakError(f"mixer {name!r} uses gain scale {gain_scale!r}")
            if source.get("inverted") is True:
                linear = -linear
            sources.append((int(channel), complex(linear)))
        mapping.append((int(dest), sources))
    return channels_out, mapping


def _capture_channels(config: Mapping[str, Any], wav_channels: int) -> int:
    devices = _mapping(config.get("devices"), "devices")
    rate = devices.get("samplerate")
    if isinstance(rate, bool) or not isinstance(rate, int):
        raise BranchPeakError(f"devices.samplerate is {rate!r}")
    if int(rate) != RESPONSE_SAMPLE_RATE_HZ:
        raise BranchPeakError(
            f"the graph runs at {rate} Hz; the shared filter evaluator models "
            f"{RESPONSE_SAMPLE_RATE_HZ} Hz"
        )
    capture = _mapping(devices.get("capture"), "devices.capture")
    channels = capture.get("channels")
    if isinstance(channels, bool) or not isinstance(channels, int) or channels < 1:
        raise BranchPeakError(f"devices.capture.channels is {channels!r}")
    if int(channels) != wav_channels:
        raise BranchPeakError(
            f"the stimulus carries {wav_channels} channels; the graph captures "
            f"{channels}"
        )
    return int(channels)


def stimulus_branch_peaks_dbfs(
    config: Mapping[str, Any],
    wav_path: str | Path,
    *,
    output_channels: Mapping[str, int],
) -> dict[str, float]:
    """Each named output channel's true peak, dBFS, for this WAV at fader 0.

    ``config`` is the applied CamillaDSP graph as a parsed mapping (either the
    JTS-emitted dialect or CamillaDSP's own read-back). ``output_channels`` maps
    the caller's own key — ``jasper-seat-level`` uses a driver target
    fingerprint — onto that driver's playback channel index. The returned
    mapping carries one entry per requested key.

    The render is exact linear filtering: every stage's complex response comes
    from the shared RBJ evaluator, the stimulus is pushed through the pipeline
    in the frequency domain block by block (overlap-save, so the cost and the
    memory are bounded by the block rather than by the stimulus length), and the
    peak is the largest absolute output sample. The main volume fader is
    deliberately absent — the ceiling formula this feeds adds it back.

    Raises :class:`BranchPeakError` for anything not modelled exactly.
    """
    import numpy as np

    if not output_channels:
        raise BranchPeakError("no output channels were requested")
    samples, rate = read_stimulus_samples(wav_path)
    if rate != RESPONSE_SAMPLE_RATE_HZ:
        raise BranchPeakError(
            f"the stimulus is {rate} Hz; the shared filter evaluator models "
            f"{RESPONSE_SAMPLE_RATE_HZ} Hz"
        )
    capture_channels = _capture_channels(config, int(samples.shape[1]))
    freqs = np.fft.rfftfreq(_BLOCK_SAMPLES, d=1.0 / RESPONSE_SAMPLE_RATE_HZ)
    operations, playback_channels = _pipeline_operations(
        config, freqs, capture_channels
    )
    for key, channel in output_channels.items():
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise BranchPeakError(f"{key} names output channel {channel!r}")
        if not 0 <= int(channel) < playback_channels:
            raise BranchPeakError(
                f"{key} names output channel {channel} of {playback_channels}"
            )

    # Front-padded by the overlap so the first block's discarded head is the
    # pad, and tail-padded by the same so a branch's delay and ringing land
    # inside the render rather than being truncated away.
    padded = np.zeros(
        (samples.shape[0] + 2 * _OVERLAP_SAMPLES, capture_channels), dtype=np.float64
    )
    padded[_OVERLAP_SAMPLES : _OVERLAP_SAMPLES + samples.shape[0], :] = samples
    hop = _BLOCK_SAMPLES - _OVERLAP_SAMPLES
    peaks = {key: 0.0 for key in output_channels}
    for start in range(0, padded.shape[0], hop):
        block = np.zeros((_BLOCK_SAMPLES, capture_channels), dtype=np.float64)
        chunk = padded[start : start + _BLOCK_SAMPLES, :]
        block[: chunk.shape[0], :] = chunk
        spectra = [
            np.fft.rfft(block[:, channel], n=_BLOCK_SAMPLES)
            for channel in range(capture_channels)
        ]
        for kind, payload in operations:
            if kind == "filter":
                channels, response = payload
                for channel in channels:
                    spectra[channel] = spectra[channel] * response
                continue
            width, mapping = payload
            mixed = [np.zeros(freqs.shape, dtype=np.complex128) for _ in range(width)]
            for dest, sources in mapping:
                for source, gain in sources:
                    mixed[dest] = mixed[dest] + spectra[source] * gain
            spectra = mixed
        for key, channel in output_channels.items():
            rendered = np.fft.irfft(spectra[int(channel)], n=_BLOCK_SAMPLES)
            tail = rendered[_OVERLAP_SAMPLES:]
            if tail.size:
                peaks[key] = max(peaks[key], float(np.max(np.abs(tail))))
    return {
        key: 20.0 * math.log10(max(peak, 1e-12)) for key, peak in peaks.items()
    }


def branch_peaks_for_targets(
    config: Mapping[str, Any],
    wav_path: str | Path,
    targets: Iterable[Mapping[str, Any]],
) -> dict[str, float]:
    """Branch peaks keyed by ``target_fingerprint`` for measurement targets.

    The adapter between :func:`jasper.active_speaker.measurement.active_driver_targets`
    — which already carries both a fingerprint and the driver's
    ``output_index`` — and :func:`stimulus_branch_peaks_dbfs`. Keeping it here
    rather than in the CLI means the fingerprint-to-channel mapping is stated
    once, beside the render that consumes it.
    """
    output_channels: dict[str, int] = {}
    for target in targets:
        fingerprint = str(target.get("target_fingerprint") or "")
        index = target.get("output_index")
        if not fingerprint:
            raise BranchPeakError("a driver target carries no target_fingerprint")
        if isinstance(index, bool) or not isinstance(index, int):
            raise BranchPeakError(
                f"driver target {fingerprint} carries output_index {index!r}"
            )
        output_channels[fingerprint] = int(index)
    return stimulus_branch_peaks_dbfs(
        config, wav_path, output_channels=output_channels
    )
