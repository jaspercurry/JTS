# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What peak ONE driver's branch actually receives for ONE stimulus.

Without a render, the un-segmented ceiling must assume every branch sees the whole
stimulus peak; on a crossed-over graph that is wrong in both directions (measured 10.7
dB under the full-band peak on JTS3's basin-2 graph, 2026-08-19; a boosting chain
instead binds tighter). This module closes the gap by RENDERING the actual stimulus
through the actual applied CamillaDSP graph and reading each output channel's true peak
at fader 0 dB.

Refuses rather than approximates: any filter type not modelled exactly is a refusal,
never a silent no-op, since under-reporting a peak raises the ceiling unsafely. One
evaluator supplies every filter response
(:func:`jasper.sound.profile._filter_response_complex`); this module owns only the
pipeline walk and overlap-save render around it, reading the config mapping directly
rather than through :class:`~jasper.active_speaker.graph_safety.GraphView` (which drops
``Mixer`` steps by construction and would omit the input split mixer's real -6.02 dB
per-leg gain). Agrees to 0.000171 dB worst case
against an independent time-domain renderer
(``tests/test_active_speaker_branch_peak.py`` cross-check block). Inherits the shared
evaluator's :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ` constraint. "Peak" is
the SAMPLE peak (matches ``program_admission.effective_true_peak_dbfs``); inter-sample
overshoot is not modelled, as elsewhere in this codebase.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jasper.bass_extension.bench.derivation import ALLOWED_FILTER_TYPES
from jasper.json_fields import finite_float
from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

# Overlap-save geometry: block is what the shared evaluator samples across; overlap is
# the discarded tail and so the longest branch delay + ringing this render can model
# without wraparound. 683 ms / 171 ms at 48 kHz, generous by orders of magnitude --
# measured against an independent time-domain renderer, worst case 0.000171 dB
# (``tests/test_active_speaker_branch_peak.py`` cross-check block). A branch delay
# exceeding the overlap is refused below.
_BLOCK_SAMPLES = 32768
_OVERLAP_SAMPLES = 8192

# Longest renderable stimulus, in FRAMES; bounds peak memory (~55 MiB at this bound, 2
# channels, on a 1 GB box). Checked against the decoded shape BEFORE the float64
# conversion. PUBLIC: ``jasper.cli.seat_level.default_stimulus_wav`` generates exactly
# this many frames.
MAX_STIMULUS_SAMPLES = 48_000 * 60

# CamillaDSP filter types this module models EXACTLY; anything else refuses. DERIVED
# from the offline-render allowlist. ``Conv`` is the one subtraction: the bench path can
# convolve a shipped FIR; this module models from config text alone, with no
# coefficients to read.
_MODELLED_FILTER_TYPES = ALLOWED_FILTER_TYPES - {"Conv"}

# Biquad shapes the shared RBJ evaluator implements; anything outside falls through to
# that evaluator's `Peaking` default, so it refuses instead.
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
    """This graph or stimulus cannot be rendered exactly. Fail-conservative at the call site:
    drops back to the full-stimulus-peak bound.
    """


def _finite(value: Any, what: str) -> float:
    number = finite_float(value)
    if number is None:
        raise BranchPeakError(f"{what} must be a finite number, got {value!r}")
    return number


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BranchPeakError(f"{what} is not a mapping")
    return value


def read_stimulus_samples(wav_path: str | Path) -> tuple[Any, int]:
    """``(float64 samples shaped (frames, channels), sample_rate_hz)``. Integer PCM is
    normalised by its dtype's own maximum, the SAME convention
    :func:`jasper.cli.seat_level.stimulus_provenance` uses. Length bound is enforced
    against the DECODED SHAPE, before the float64 conversion.
    """
    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(str(wav_path))
    array = np.asarray(data)
    if array.ndim == 1:
        array = array[:, None]
    if array.ndim != 2:
        raise BranchPeakError(f"{wav_path} is not a mono or multichannel WAV")
    if array.shape[0] > MAX_STIMULUS_SAMPLES:
        raise BranchPeakError(
            f"{wav_path} carries {array.shape[0]} frames, past the "
            f"{MAX_STIMULUS_SAMPLES}-frame render bound"
        )
    samples = array.astype(np.float64)
    if np.issubdtype(array.dtype, np.integer):
        samples = samples / float(np.iinfo(array.dtype).max)
    if not samples.size:
        raise BranchPeakError(f"{wav_path} carries no frames")
    return samples, int(rate)


def _filter_records(
    names: Sequence[str], filters: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[Any], complex, float]:
    """One pipeline step's names reduced to ``(biquads, sections, scale, delay_s)``. A transfer
    function is a product, so the four accumulators may be collected in any order and
    multiplied once.
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
            # Pass-through: a limiter can only ever REDUCE a peak, so ignoring
            # it over-reports the branch, binding the ceiling tighter (safe).
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
            if isinstance(order, bool) or not isinstance(order, int) or order < 2:
                raise BranchPeakError(f"filter {name!r} has order {order!r}")
            if order % 2:
                # LR order N is two cascaded Butterworths of N/2; odd N has
                # no such pair.
                raise BranchPeakError(
                    f"filter {name!r} has odd Linkwitz-Riley order {order}"
                )
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
        # A shelf/bell may spell its width as ``bandwidth``/``slope`` instead
        # of ``q``; reading the absent ``q`` as the evaluator's 1.0 default
        # models a different filter, measured up to 2.4 dB shallower (UNDER-
        # reports the peak, RAISES the ceiling) -- refuse instead.
        if not isinstance(params.get("q"), (int, float)) or isinstance(
            params.get("q"), bool
        ):
            raise BranchPeakError(
                f"filter {name!r} is a {shape} biquad with no numeric q "
                f"(got {params.get('q')!r}); a bandwidth/slope width is not "
                "modelled"
            )
        biquads.append(
            {
                "biquad_type": shape,
                "freq": _finite(params.get("freq"), f"filter {name!r} freq"),
                "q": _finite(params.get("q"), f"filter {name!r} q"),
                "gain": float(params.get("gain") or 0.0),
            }
        )
    return biquads, sections, scale, delay_s


def _step_transfer(
    names: Sequence[str], filters: Mapping[str, Any], freqs: Any
) -> tuple[Any, float]:
    """``(complex response across freqs, seconds of delay it adds)``. Delay is returned, not
    checked here: the overlap bounds a whole BRANCH, accumulated per channel in
    :func:`_pipeline_operations`.
    """
    import numpy as np

    from jasper.active_speaker.branch_chain import (
        chain_response,
        crossover_response_complex,
    )

    biquads, sections, scale, delay_s = _filter_records(names, filters)
    response = np.full(freqs.shape, scale, dtype=np.complex128)
    if biquads:
        response = response * chain_response(biquads, freqs)
    if sections:
        response = response * crossover_response_complex(freqs, sections)
    if delay_s:
        response = response * np.exp(-2j * np.pi * freqs * delay_s)
    return response, delay_s


def _guard_branch_delay(delays: Sequence[float]) -> None:
    """Refuse once ANY branch's ACCUMULATED delay passes the render overlap. Cumulative, not
    per step: three 80 ms steps are a 240 ms branch.
    """
    worst = max(delays, default=0.0)
    if worst * RESPONSE_SAMPLE_RATE_HZ > _OVERLAP_SAMPLES:
        raise BranchPeakError(
            f"a branch delays {worst * 1e3:.1f} ms in total, past the "
            f"{_OVERLAP_SAMPLES / RESPONSE_SAMPLE_RATE_HZ * 1e3:.1f} ms render overlap"
        )


def _pipeline_operations(
    config: Mapping[str, Any], freqs: Any, capture_channels: int
) -> tuple[list[tuple[str, Any]], int]:
    """The applied pipeline reduced to ordered spectrum operations, plus the ending channel
    count (playback width requested output indexes are validated against).
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
    # Delay accumulated per CURRENT channel, so the overlap guard bounds a
    # whole branch, not one step.
    delays = [0.0] * width
    for index, step in enumerate(pipeline):
        if not isinstance(step, Mapping):
            raise BranchPeakError(f"pipeline step {index} is not a mapping")
        if step.get("bypassed") is True:
            # Refuse rather than trust a second bypass semantics to stay true.
            raise BranchPeakError(f"pipeline step {index} is bypassed")
        kind = str(step.get("type") or "")
        if kind == "Filter":
            channels = _step_channels(step, width, index)
            names = [str(name) for name in step.get("names") or [] if name is not None]
            if not names:
                continue
            response, delay_s = _step_transfer(names, filters, freqs)
            for channel in channels:
                delays[channel] += delay_s
            _guard_branch_delay(delays)
            operations.append(("filter", (channels, response)))
            continue
        if kind == "Mixer":
            name = str(step.get("name") or "")
            mixer = mixers.get(name)
            if not isinstance(mixer, Mapping):
                raise BranchPeakError(f"pipeline step {index} names mixer {name!r}")
            width, mapping = _mixer_mapping(mixer, width, name)
            # A dest inherits the WORST delay among the sources it sums.
            carried = [0.0] * width
            for dest, sources in mapping:
                for source, _gain in sources:
                    carried[dest] = max(carried[dest], delays[source])
            delays = carried
            _guard_branch_delay(delays)
            operations.append(("mixer", (width, mapping)))
            continue
        raise BranchPeakError(f"pipeline step {index} is a {kind or 'typeless'} step")
    return operations, width


def _step_channels(step: Mapping[str, Any], width: int, index: int) -> tuple[int, ...]:
    """A Filter step's channel list, in either CamillaDSP spelling: the ``channels: [..]`` list
    JTS emits, or the scalar ``channel: N`` sugar CamillaDSP's readback adds. Absent
    means "every channel" (CamillaDSP's own default).
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
    """Each named output channel's true peak, dBFS, for this WAV at fader 0. ``config`` is the
    applied CamillaDSP graph, parsed; ``output_channels`` maps the caller's own key onto
    a playback channel index. The main volume fader is deliberately absent -- the
    ceiling formula this feeds adds it back. Memory: everything after decode is
    per-BLOCK, but decode itself holds the whole float64 stimulus
    (:data:`MAX_STIMULUS_SAMPLES` bounds it, ~46 MB at 48 kHz stereo, 60 s). Raises
    :class:`BranchPeakError` for anything not modelled exactly.
    """
    import numpy as np

    if not output_channels:
        raise BranchPeakError("no output channels were requested")
    # yaml.safe_load can return None/list/str/int, none with ``.get``; typed
    # here so only BranchPeakError reaches the caller's fallback, not a crash.
    if not isinstance(config, Mapping):
        raise BranchPeakError(
            "the applied config is not a mapping "
            f"(read a {type(config).__name__}); an empty, list, or scalar "
            "YAML document cannot be a CamillaDSP graph"
        )
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

    # Walks a VIRTUAL padded signal (silence before and after the stimulus,
    # so branch delay/ringing land inside the render); sliced directly from
    # the stimulus rather than materialised, to avoid doubling peak memory.
    frames = samples.shape[0]
    hop = _BLOCK_SAMPLES - _OVERLAP_SAMPLES
    peaks = {key: 0.0 for key in output_channels}
    for start in range(0, frames + 2 * _OVERLAP_SAMPLES, hop):
        block = np.zeros((_BLOCK_SAMPLES, capture_channels), dtype=np.float64)
        # Padded index i is stimulus index i - _OVERLAP_SAMPLES.
        low = start - _OVERLAP_SAMPLES
        source_low = max(low, 0)
        source_high = min(low + _BLOCK_SAMPLES, frames)
        if source_high > source_low:
            block[source_low - low : source_high - low, :] = (
                samples[source_low:source_high, :]
            )
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
    """Branch peaks keyed by ``target_fingerprint`` for measurement targets. Adapter between
    :func:`jasper.active_speaker.measurement.active_driver_targets` and
    :func:`stimulus_branch_peaks_dbfs`.
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
