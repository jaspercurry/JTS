# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program peak of an emitted active-speaker graph (#5909).

One number for the headroom charge and its proof: the steady-state peak of the
graph's own series cascade, mixer sums included, on the grid
:func:`~.branch_chain.camilla_evaluation_grid` builds from every evaluated
filter. Each output's program is the worst case in phase, the sum over capture
channels of ``|H(f)|``. Limiters pass through, dynamic bass sits at rest (its
reserve is charged at admission, ADR-0359), and the preference steps between
the headroom gain and the next mixer are left out, because a preference boost
rides at unity (ADR-0121). Overshoot between grid points and transients stay
with the per-output limiters (ADR-0324).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, NamedTuple

from jasper.bass_extension.dynamic_graph import PREFIX as DYNAMIC_BASS_PREFIX
from jasper.biquad import (
    EVALUABLE_HZ_MAX, EVALUABLE_HZ_MIN, EVALUABLE_Q_MAX, EVALUABLE_Q_MIN,
    SHELF_BIQUAD_TYPES, SHELF_Q, SHELF_Q_EMIT_DECIMALS,
)
from jasper.json_fields import finite_float

from .graph_transfer import GraphTransferError, complex_channel_transfer, mixer_mapping

PROGRAM_HEADROOM_FILTER = "active_baseline_headroom"

# The Butterworth q as the emitter spells it; a shelf, low- or high-pass at or
# under it overshoots unity by less than 1e-8 dB.
_MONOTONIC_Q_MAX = round(SHELF_Q, SHELF_Q_EMIT_DECIMALS)
_UNITY_COMBOS = frozenset({
    "LinkwitzRileyHighpass", "LinkwitzRileyLowpass", "ButterworthHighpass", "ButterworthLowpass",
})


class ProgramPeak(NamedTuple):
    db: float
    output: int | None
    hz: float


def program_peak(graph: Mapping[str, Any], *, charged: bool = False) -> ProgramPeak:
    """The loudest worst-case program any output of ``graph`` plays, dB re unity.

    ``charged`` keeps the graph's own headroom gain; without it that gain is held
    at 0 dB. When no step can lift any frequency above unity nothing is evaluated
    and numpy is not imported (ADR-0226): the peak is then at most 0 dB, returned
    as ``(0.0, None, nan)``. A graph that plays nothing peaks at ``-inf``.

    Raises :class:`~.graph_transfer.GraphTransferError` when the graph has no
    exactly modelled transfer.
    """
    pipeline = _evaluated_pipeline(graph.get("pipeline"), charged=charged)
    filters = graph.get("filters")
    filters = filters if isinstance(filters, Mapping) else {}
    mixers = graph.get("mixers")
    if _never_above_unity(pipeline, filters, mixers if isinstance(mixers, Mapping) else {}):
        return ProgramPeak(0.0, None, math.nan)
    import numpy as np  # lazy: import cost, see the docstring

    from .branch_chain import camilla_evaluation_grid  # lazy: imports numpy

    specs = [
        spec for step in (pipeline if isinstance(pipeline, list) else [])
        if isinstance(step, Mapping) and step.get("type") == "Filter"
        for name in _names(step)
        if not name.startswith(DYNAMIC_BASS_PREFIX)
        and isinstance(spec := filters.get(name), Mapping)
        and spec.get("type") in ("Biquad", "BiquadCombo")
    ]
    try:
        grid = camilla_evaluation_grid(specs)
    except (KeyError, TypeError, ValueError) as exc:
        raise GraphTransferError(f"a filter's type or frequency is unreadable: {exc!r}") from exc
    outputs = range(_channel_count(graph, "playback"))
    program = np.zeros((len(outputs), grid.size))
    for channel in range(_channel_count(graph, "capture")):
        transfer = complex_channel_transfer(
            {**graph, "pipeline": pipeline}, grid,
            input_weights={channel: 1.0}, output_channels={output: output for output in outputs},
            allow_limiter_passthrough=True, dynamic_bass_at_rest=True,
        )
        program += np.abs([transfer[output] for output in outputs])
    if not np.any(program > 0.0):
        return ProgramPeak(-math.inf, None, math.nan)
    output, index = np.unravel_index(int(np.argmax(program)), program.shape)
    return ProgramPeak(
        20.0 * math.log10(float(program[output, index])), int(output), float(grid[index]),
    )


def _names(step: Mapping[str, Any]) -> list[str]:
    names = step.get("names")
    return [str(name) for name in names if name is not None] if isinstance(names, list) else []


def _evaluated_pipeline(pipeline: Any, *, charged: bool) -> Any:
    """``pipeline`` less its preference steps, and less the headroom gain unless
    ``charged``; both only ahead of the first mixer."""
    if not isinstance(pipeline, list):
        return pipeline
    steps: list[Any] = []
    preference = mixed = False
    for step in pipeline:
        if isinstance(step, Mapping) and step.get("type") == "Mixer":
            preference, mixed = False, True
        elif preference:
            continue
        elif not mixed and isinstance(step, Mapping) and PROGRAM_HEADROOM_FILTER in _names(step):
            preference = True
            if not charged:
                step = {**step, "names": [
                    name for name in _names(step) if name != PROGRAM_HEADROOM_FILTER
                ]}
        steps.append(step)
    return steps


def _never_above_unity(pipeline: Any, filters: Mapping[str, Any], mixers: Mapping[str, Any]) -> bool:
    """Every filter is unity-bounded and no mixer output sums its sources above unity."""
    if not isinstance(pipeline, list):
        return False
    for step in pipeline:
        if not isinstance(step, Mapping) or step.get("bypassed") is True:
            return False
        name = str(step.get("name") or "")
        if name.startswith(DYNAMIC_BASS_PREFIX):
            continue
        if step.get("type") == "Filter":
            if not all(
                _unity_bounded(filters.get(filter_name)) for filter_name in _names(step)
                if not filter_name.startswith(DYNAMIC_BASS_PREFIX)
            ):
                return False
        elif step.get("type") != "Mixer" or not _sums_within_unity(mixers.get(name), name):
            return False
    return True


def _sums_within_unity(mixer: Any, name: str) -> bool:
    channels = mixer.get("channels") if isinstance(mixer, Mapping) else None
    width = channels.get("in") if isinstance(channels, Mapping) else None
    if isinstance(width, bool) or not isinstance(width, int):
        return False
    try:
        _, mapping = mixer_mapping(mixer, width, name)
    except GraphTransferError:
        return False
    return all(sum(abs(gain) for _, gain in sources) <= 1.0 for _, sources in mapping)


def _unity_bounded(spec: Any) -> bool:
    """No frequency leaves this filter above unity.

    A q or frequency outside the evaluator's domain is left to the evaluator:
    f64 round-off can lift a cut there (jasper/biquad.py).
    """
    params = spec.get("parameters") if isinstance(spec, Mapping) else None
    if not isinstance(params, Mapping):
        return False
    kind, shape = spec.get("type"), params.get("type")
    gain = finite_float(params.get("gain"))
    cut = gain is not None and gain <= 0.0
    if kind in ("Delay", "Limiter"):
        return True
    if kind == "Gain":
        return params.get("mute") is True or (cut and params.get("scale") in (None, "dB", "decibel"))
    freq, q = finite_float(params.get("freq")), finite_float(params.get("q"))
    if freq is None or not EVALUABLE_HZ_MIN <= freq <= EVALUABLE_HZ_MAX:
        return False
    if kind == "BiquadCombo":
        return shape in _UNITY_COMBOS
    if kind != "Biquad" or q is None or not EVALUABLE_Q_MIN <= q <= EVALUABLE_Q_MAX:
        return False
    if shape in ("Notch", "Allpass", "Peaking"):
        return shape != "Peaking" or cut
    return q <= _MONOTONIC_Q_MAX and (
        shape in ("Lowpass", "Highpass") or (shape in SHELF_BIQUAD_TYPES and cut)
    )


def _channel_count(graph: Mapping[str, Any], end: str) -> int:
    devices = graph.get("devices")
    side = devices.get(end) if isinstance(devices, Mapping) else None
    count = side.get("channels") if isinstance(side, Mapping) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise GraphTransferError(f"devices.{end}.channels is {count!r}")
    return count
