# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact complex transfer of an applied CamillaDSP graph per output channel.

Filters use their emitted parameters. The walker refuses what it cannot model
exactly because its consumers make level and timing claims from the transfer.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from jasper.bass_extension.dynamic_graph import PREFIX as DYNAMIC_BASS_PREFIX
from jasper.json_fields import finite_float
from jasper.biquad import RESPONSE_SAMPLE_RATE_HZ

# Types modelled from configuration alone; FIR convolution needs external data.
_MODELLED_FILTER_TYPES = frozenset({"Biquad", "BiquadCombo", "Delay", "Gain", "Limiter"})

_MODELLED_BIQUAD_TYPES = frozenset(
    {"Lowpass", "Highpass", "Notch", "Lowshelf", "Highshelf", "Peaking", "Allpass"}
)

_MODELLED_COMBO_TYPES = frozenset({
    "LinkwitzRileyHighpass", "LinkwitzRileyLowpass", "ButterworthHighpass", "ButterworthLowpass",
})

class GraphTransferError(RuntimeError):
    """The graph has no exactly modelled transfer."""


def _finite(value: Any, what: str) -> float:
    number = finite_float(value)
    if number is None:
        raise GraphTransferError(f"{what} must be a finite number, got {value!r}")
    return number


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GraphTransferError(f"{what} is not a mapping")
    return value


def _delay_seconds(params: Mapping[str, Any], name: str) -> float:
    """The whole-sample delay CamillaDSP applies when ``subsample`` is false."""
    subsample = params.get("subsample")
    if subsample not in (None, False):
        if subsample is True:
            raise GraphTransferError(
                f"filter {name!r} uses a subsample Delay, whose allpass is not modelled"
            )
        raise GraphTransferError(f"filter {name!r} has invalid subsample {subsample!r}")
    delay = _finite(params.get("delay"), f"filter {name!r} delay")
    if delay < 0.0:
        raise GraphTransferError(f"filter {name!r} has a negative delay")
    unit = str(params.get("unit") or "ms")
    if unit == "samples":
        requested_samples = delay
    elif unit == "ms":
        requested_samples = delay * RESPONSE_SAMPLE_RATE_HZ / 1000.0
    elif unit == "us":
        requested_samples = delay * RESPONSE_SAMPLE_RATE_HZ / 1_000_000.0
    elif unit == "mm":
        requested_samples = delay * RESPONSE_SAMPLE_RATE_HZ / 343_000.0
    else:
        raise GraphTransferError(f"filter {name!r} delays in {unit!r}")
    # CamillaDSP's default Delay is a delay line rounded to the nearest full
    # sample. The request is non-negative, so floor(x + 0.5) matches Rust's
    # f64::round rule at half-sample ties.
    samples = math.floor(requested_samples + 0.5)
    return samples / float(RESPONSE_SAMPLE_RATE_HZ)


def _filter_records(
    names: Sequence[str], filters: Mapping[str, Any],
    *,
    allow_limiter_passthrough: bool = True,
) -> tuple[list[Mapping[str, Any]], complex, float]:
    """Validated Camilla filters, gain and delay for one pipeline step."""
    biquads: list[Mapping[str, Any]] = []
    scale = 1.0 + 0.0j
    delay_s = 0.0
    for name in names:
        spec = filters.get(name)
        if not isinstance(spec, Mapping):
            raise GraphTransferError(f"pipeline names filter {name!r}, which is undefined")
        kind = str(spec.get("type") or "")
        if kind not in _MODELLED_FILTER_TYPES:
            raise GraphTransferError(f"filter {name!r} is a {kind or 'typeless'} filter")
        params = _mapping(spec.get("parameters"), f"filter {name!r} parameters")
        if kind == "Limiter":
            if not allow_limiter_passthrough:
                raise GraphTransferError(
                    f"filter {name!r} is nonlinear and has no complex transfer"
                )
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
                raise GraphTransferError(
                    f"filter {name!r} uses gain scale {gain_scale!r}"
                )
            if params.get("inverted") is True:
                linear = -linear
            scale = scale * linear
            continue
        if kind == "Delay":
            delay_s += _delay_seconds(params, name)
            continue
        if kind == "BiquadCombo":
            combo = str(params.get("type") or "")
            if combo not in _MODELLED_COMBO_TYPES:
                raise GraphTransferError(f"filter {name!r} is a {combo!r} combo")
            order = params.get("order")
            if isinstance(order, bool) or not isinstance(order, int) or order < 1:
                raise GraphTransferError(f"filter {name!r} has order {order!r}")
            if combo.startswith("LinkwitzRiley") and order % 2:
                # LR order N is two cascaded Butterworths of N/2; odd N has
                # no such pair.
                raise GraphTransferError(
                    f"filter {name!r} has odd Linkwitz-Riley order {order}"
                )
            _finite(params.get("freq"), f"filter {name!r} freq")
            biquads.append(spec)
            continue
        shape = str(params.get("type") or "")
        if shape not in _MODELLED_BIQUAD_TYPES:
            raise GraphTransferError(f"filter {name!r} is a {shape!r} biquad")
        # A bandwidth/slope width cannot use the evaluator's default q.
        if not isinstance(params.get("q"), (int, float)) or isinstance(
            params.get("q"), bool
        ):
            raise GraphTransferError(
                f"filter {name!r} is a {shape} biquad with no numeric q "
                f"(got {params.get('q')!r}); a bandwidth/slope width is not "
                "modelled"
            )
        _finite(params.get("freq"), f"filter {name!r} freq")
        _finite(params.get("q"), f"filter {name!r} q")
        biquads.append(spec)
    return biquads, scale, delay_s


def filter_transfer(
    names: Sequence[str], filters: Mapping[str, Any], freqs: Any,
    *,
    allow_limiter_passthrough: bool = True,
) -> Any:
    """Complex transfer of a named filter chain at the supplied frequencies."""
    import numpy as np  # lazy: NumPy cost belongs to numerical analysis

    from jasper.active_speaker.branch_chain import camilla_filter_response  # lazy: numerical analysis

    biquads, scale, delay_s = _filter_records(
        names, filters, allow_limiter_passthrough=allow_limiter_passthrough,
    )
    response = scale * camilla_filter_response(biquads, freqs)
    if delay_s:
        response = response * np.exp(-2j * np.pi * freqs * delay_s)
    return response


def _pipeline_operations(
    config: Mapping[str, Any], freqs: Any, capture_channels: int,
    *,
    allow_limiter_passthrough: bool = True,
    dynamic_bass_at_rest: bool = False,
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
        raise GraphTransferError("the applied config carries no pipeline list")

    operations: list[tuple[str, Any]] = []
    width = int(capture_channels)
    for index, step in enumerate(pipeline):
        if not isinstance(step, Mapping):
            raise GraphTransferError(f"pipeline step {index} is not a mapping")
        if step.get("bypassed") is True:
            # Refuse rather than trust a second bypass semantics to stay true.
            raise GraphTransferError(f"pipeline step {index} is bypassed")
        if dynamic_bass_at_rest and str(step.get("name", "")).startswith(DYNAMIC_BASS_PREFIX):
            continue
        kind = str(step.get("type") or "")
        if kind == "Filter":
            names = [str(name) for name in step.get("names") or [] if name is not None]
            if dynamic_bass_at_rest:
                names = [name for name in names if not name.startswith(DYNAMIC_BASS_PREFIX)]
            if dynamic_bass_at_rest and not names:
                continue
            channels = _step_channels(step, width, index)
            if not names:
                continue
            response = filter_transfer(
                names, filters, freqs,
                allow_limiter_passthrough=allow_limiter_passthrough,
            )
            operations.append(("filter", (channels, response)))
            continue
        if kind == "Mixer":
            name = str(step.get("name") or "")
            mixer = mixers.get(name)
            if not isinstance(mixer, Mapping):
                raise GraphTransferError(f"pipeline step {index} names mixer {name!r}")
            width, mapping = mixer_mapping(mixer, width, name)
            operations.append(("mixer", (width, mapping)))
            continue
        raise GraphTransferError(f"pipeline step {index} is a {kind or 'typeless'} step")
    return operations, width


def complex_channel_transfer(
    config: Mapping[str, Any],
    freqs_hz: Any,
    *,
    input_weights: Mapping[int, complex],
    output_channels: Mapping[Any, int],
    allow_limiter_passthrough: bool = False,
    dynamic_bass_at_rest: bool = False,
) -> dict[Any, Any]:
    """Complex transfer from one declared input mixture to named outputs.

    ``input_weights`` states the signal actually driven on each capture
    channel. A role-routed diagnostic uses one weight of ``1``; a coherent
    stereo summed sweep uses ``{0: 1, 1: 1}``, so the active split mixer's two
    -6.02 dB legs are both included.

    Limiters have no linear transfer. They refuse by default. A caller may
    treat them as pass-through only after proving the compared graphs carry the
    same limiter semantics and placement. ``dynamic_bass_at_rest`` excludes the
    dynamic-bass block from a static comparison; admission charges its reserve.
    """
    import numpy as np  # lazy: keep NumPy off admission/status imports until analysis is needed

    if not isinstance(config, Mapping):
        raise GraphTransferError("the applied config is not a mapping")
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if freqs.ndim != 1 or not freqs.size or not np.all(np.isfinite(freqs)):
        raise GraphTransferError("frequencies must be a non-empty finite vector")
    if np.any(freqs < 0.0):
        raise GraphTransferError("frequencies must be non-negative")
    devices = _mapping(config.get("devices"), "devices")
    rate = devices.get("samplerate")
    if isinstance(rate, bool) or not isinstance(rate, int):
        raise GraphTransferError(f"devices.samplerate is {rate!r}")
    if int(rate) != RESPONSE_SAMPLE_RATE_HZ:
        raise GraphTransferError(
            f"the graph runs at {rate} Hz; the shared filter evaluator models "
            f"{RESPONSE_SAMPLE_RATE_HZ} Hz"
        )
    capture = _mapping(devices.get("capture"), "devices.capture")
    capture_channels = capture.get("channels")
    if (
        isinstance(capture_channels, bool)
        or not isinstance(capture_channels, int)
        or capture_channels < 1
    ):
        raise GraphTransferError(
            f"devices.capture.channels is {capture_channels!r}"
        )
    if not input_weights:
        raise GraphTransferError("no input channel weights were declared")
    spectra = [np.zeros(freqs.shape, dtype=np.complex128) for _ in range(capture_channels)]
    for channel, weight in input_weights.items():
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise GraphTransferError(f"input channel is {channel!r}")
        if not 0 <= channel < capture_channels:
            raise GraphTransferError(
                f"input channel {channel} is outside capture width {capture_channels}"
            )
        value = complex(weight)
        if not (math.isfinite(value.real) and math.isfinite(value.imag)):
            raise GraphTransferError(f"input channel {channel} has a non-finite weight")
        spectra[channel] = np.full(freqs.shape, value, dtype=np.complex128)

    operations, playback_channels = _pipeline_operations(
        config, freqs, capture_channels,
        allow_limiter_passthrough=allow_limiter_passthrough,
        dynamic_bass_at_rest=dynamic_bass_at_rest,
    )
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

    result: dict[Any, Any] = {}
    for key, channel in output_channels.items():
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise GraphTransferError(f"{key} names output channel {channel!r}")
        if not 0 <= channel < playback_channels:
            raise GraphTransferError(
                f"{key} names output channel {channel} of {playback_channels}"
            )
        result[key] = spectra[channel].copy()
    return result


def _step_channels(step: Mapping[str, Any], width: int, index: int) -> tuple[int, ...]:
    """A Filter step's channel list, in either CamillaDSP spelling: the ``channels: [..]`` list
    JTS emits, or the scalar ``channel: N`` sugar CamillaDSP's readback adds. Absent
    means "every channel" (CamillaDSP's own default).
    """
    raw = step.get("channels")
    if isinstance(raw, list):
        found = [c for c in raw if isinstance(c, int) and not isinstance(c, bool)]
        if len(found) != len(raw):
            raise GraphTransferError(f"pipeline step {index} has a non-integer channel")
    elif isinstance(raw, int) and not isinstance(raw, bool):
        found = [int(raw)]
    else:
        channel = step.get("channel")
        if isinstance(channel, int) and not isinstance(channel, bool):
            found = [int(channel)]
        elif raw is None and channel is None:
            found = list(range(width))
        else:
            raise GraphTransferError(f"pipeline step {index} has no readable channels")
    for channel in found:
        if not 0 <= channel < width:
            raise GraphTransferError(
                f"pipeline step {index} touches channel {channel} of {width}"
            )
    return tuple(found)


def mixer_mapping(
    mixer: Mapping[str, Any], width: int, name: str
) -> tuple[int, list[tuple[int, list[tuple[int, complex]]]]]:
    """``(out_width, [(dest, [(source, complex gain), ...]), ...])``."""
    channels = _mapping(mixer.get("channels"), f"mixer {name!r} channels")
    counts: dict[str, int] = {}
    for what in ("in", "out"):
        value = channels.get(what)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GraphTransferError(f"mixer {name!r} has channels.{what} {value!r}")
        counts[what] = int(value)
    channels_in, channels_out = counts["in"], counts["out"]
    if channels_in != width:
        raise GraphTransferError(
            f"mixer {name!r} takes {channels_in} channels where the pipeline "
            f"carries {width}"
        )
    mapping_raw = mixer.get("mapping")
    if not isinstance(mapping_raw, list):
        raise GraphTransferError(f"mixer {name!r} carries no mapping list")
    mapping: list[tuple[int, list[tuple[int, complex]]]] = []
    for entry in mapping_raw:
        entry = _mapping(entry, f"mixer {name!r} mapping entry")
        if entry.get("mute") is True:
            continue
        dest = entry.get("dest")
        if isinstance(dest, bool) or not isinstance(dest, int):
            raise GraphTransferError(f"mixer {name!r} has dest {dest!r}")
        if not 0 <= int(dest) < channels_out:
            raise GraphTransferError(f"mixer {name!r} maps to dest {dest} out of range")
        sources_raw = entry.get("sources")
        if not isinstance(sources_raw, list):
            raise GraphTransferError(f"mixer {name!r} dest {dest} carries no sources")
        sources: list[tuple[int, complex]] = []
        for source in sources_raw:
            source = _mapping(source, f"mixer {name!r} source")
            if source.get("mute") is True:
                continue
            channel = source.get("channel")
            if isinstance(channel, bool) or not isinstance(channel, int):
                raise GraphTransferError(f"mixer {name!r} has source channel {channel!r}")
            if not 0 <= int(channel) < width:
                raise GraphTransferError(
                    f"mixer {name!r} reads channel {channel} of {width}"
                )
            gain_scale = str(source.get("scale") or "dB")
            gain = _finite(source.get("gain"), f"mixer {name!r} source gain")
            if gain_scale in ("dB", "decibel"):
                linear = 10.0 ** (gain / 20.0)
            elif gain_scale == "linear":
                linear = gain
            else:
                raise GraphTransferError(f"mixer {name!r} uses gain scale {gain_scale!r}")
            if source.get("inverted") is True:
                linear = -linear
            sources.append((int(channel), complex(linear)))
        mapping.append((int(dest), sources))
    return channels_out, mapping
