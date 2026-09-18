# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Acoustic-task handoff and a CamillaDSP stage; no devices, storage or apply."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import math
import struct
from typing import Any, Callable

import yaml

from jasper.camilla_emit import emit_delay_filter, emit_gain_filter, emit_mixer
from jasper.json_fields import JsonFields, finite_float

KIND = "jts_rear_calibration"
PHASE_CONVENTION = "positive_delay_has_negative_phase"
STAGES = {"crossover", "driver_correction", "boundary_correction", "protection"}
BIQUADS = {"Highpass", "Lowpass", "Peaking", "Lowshelf", "Highshelf", "Allpass"}
COMBOS = {"ButterworthHighpass", "ButterworthLowpass", "LinkwitzRileyHighpass", "LinkwitzRileyLowpass"}
SHELVING = {"Peaking", "Lowshelf", "Highshelf"}

# The vocabulary bounds that keep every chain filter at |H| <= 1. The one
# exception is a resonant high/low-pass, which peaks by at most 1.25 dB at
# Q = 1.0. Allpass is unity magnitude at every Q; a Peaking filter that cannot
# boost is a cut at every Q.
MAX_FILTERS_PER_CHAIN = 16
# CamillaDSP's own Gain floor; a chain at it is silent.
MIN_CHAIN_GAIN_DB = -150.0
MAX_RESONANT_Q = 1.0
# An all-pass passes every magnitude but steers the branch SUM through its phase
# rotation, and the headroom charge reads that sum on a finite grid
# (``branch_chain.camilla_evaluation_grid``). Past this Q the rotation is
# narrower than the grid resolves, so a peak could hide between two samples.
# Cabinet-scale boundary correction needs nothing near it.
MAX_ALLPASS_Q = 10.0
MAX_COMBO_ORDER = 8


class RearCalibrationError(ValueError):
    pass


_FIELDS = JsonFields(RearCalibrationError)


def _object(raw: Any, keys: set[str], field: str) -> Mapping[str, Any]:
    value = _FIELDS.mapping(raw, field)
    if set(value) != keys:
        raise RearCalibrationError(f"{field} requires exactly {sorted(keys)}")
    return value


def _number(raw: Any, field: str, minimum: float | None = None) -> float:
    value = finite_float(raw)
    if value is None or (minimum is not None and value < minimum):
        raise RearCalibrationError(f"{field} must be a finite number" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def _filters(raw: Any, sample_rate: int, field: str) -> None:
    items = _FIELDS.sequence(raw, field)
    if len(items) > MAX_FILTERS_PER_CHAIN:
        raise RearCalibrationError(f"{field} carries more than {MAX_FILTERS_PER_CHAIN} filters")
    for i, item in enumerate(items):
        name = f"{field}[{i}]"
        entry = _object(item, {"type", "parameters"}, name)
        params = _FIELDS.mapping(entry["parameters"], name + ".parameters")
        kind = params.get("type")
        if not isinstance(kind, str):
            raise RearCalibrationError(f"{name}.type must be text")
        if entry["type"] == "Biquad" and kind in BIQUADS:
            keys = {"type", "freq", "q"}
            if kind in SHELVING:
                keys.add("gain")
                if _number(params.get("gain"), name + ".gain") > 0:
                    raise RearCalibrationError(f"{name}.gain must be a cut, not a boost")
            q = _number(params.get("q"), name + ".q", 0)
            if q == 0:
                raise RearCalibrationError(f"{name}.q must be positive")
            if kind not in {"Peaking", "Allpass"} and q > MAX_RESONANT_Q:
                raise RearCalibrationError(f"{name}.q must not exceed {MAX_RESONANT_Q} and resonate")
            if kind == "Allpass" and q > MAX_ALLPASS_Q:
                raise RearCalibrationError(f"{name}.q must not exceed {MAX_ALLPASS_Q:g} and outrun the headroom grid")
        elif entry["type"] == "BiquadCombo" and kind in COMBOS:
            keys = {"type", "freq", "order"}
            order = params.get("order")
            if type(order) is not int or not 1 <= order <= MAX_COMBO_ORDER or (
                str(kind).startswith("LinkwitzRiley") and order % 2
            ):
                raise RearCalibrationError(f"{name}.order is invalid")
        else:
            raise RearCalibrationError(f"{name} is not a supported biquad or crossover")
        _object(params, keys, name + ".parameters")
        freq = _number(params["freq"], name + ".freq", 0)
        if not 0 < freq < sample_rate / 2:
            raise RearCalibrationError(f"{name}.freq must be between DC and Nyquist")


def _chain(raw: Any, sample_rate: int, name: str) -> None:
    chain = _object(raw, {"gain_db", "inverted", "delay_ms", "muted", "filters"}, name)
    if not MIN_CHAIN_GAIN_DB <= _number(chain["gain_db"], name + ".gain_db") <= 0:
        raise RearCalibrationError(
            f"{name}.gain_db must be an attenuation between {MIN_CHAIN_GAIN_DB:g} and 0 dB")
    _number(chain["delay_ms"], name + ".delay_ms")
    for key in ("inverted", "muted"):
        if type(chain[key]) is not bool:
            raise RearCalibrationError(f"{name}.{key} must be boolean")
    _filters(chain["filters"], sample_rate, name + ".filters")


def coefficient_sha256(values: list[float]) -> str:
    """SHA-256 of coefficients as consecutive little-endian IEEE float64 values."""
    return hashlib.sha256(b"".join(struct.pack("<d", value) for value in values)).hexdigest()


def read_rear_calibration(raw: Any, *, sample_rate: int | None = None) -> dict[str, Any]:
    common = {"kind", "schema", "case", "sample_rate_hz", "phase_convention", "geometry", "reference",
              "conditions", "valid_band_hz", "assumptions", "included_stages"}
    document = _FIELDS.mapping(raw, "calibration")
    case = document.get("case")
    fields = {"targets"} if case == "acoustic_targets" else {"front", "rear", "boundary", "common_delay_ms", "rear_muted"}
    _object(document, common | fields, "calibration")
    if document["kind"] != KIND or type(document["schema"]) is not int or document["schema"] != 1:
        raise RearCalibrationError("unsupported calibration kind or schema")
    if case not in {"acoustic_targets", "electrical_dsp"} or document["phase_convention"] != PHASE_CONVENTION:
        raise RearCalibrationError("unsupported calibration case or phase convention")
    rate = document["sample_rate_hz"]
    if type(rate) is not int or rate <= 0 or (sample_rate is not None and rate != sample_rate):
        raise RearCalibrationError("calibration sample rate must match the selected DSP rate")
    geometry = _object(document["geometry"], {"cabinet_back_wall_m", "sources", "details"}, "geometry")
    if geometry["cabinet_back_wall_m"] is not None:
        _number(geometry["cabinet_back_wall_m"], "cabinet_back_wall_m", 0)
    _object(geometry["sources"], {"front", "rear"}, "geometry.sources")
    _FIELDS.mapping(document["conditions"], "conditions")
    reference = _object(document["reference"], {"quantity", "units", "level"}, "reference")
    expected = {"acoustic_motion", "pressure_per_electrical_input"} if case == "acoustic_targets" else {"electrical_filter_transfer"}
    if reference["quantity"] not in expected or not isinstance(reference["units"], str) or not reference["units"]:
        raise RearCalibrationError("reference quantity and units must identify the represented transfer")
    band = document["valid_band_hz"]
    if band is None and case == "electrical_dsp":
        pass
    elif not isinstance(band, list) or len(band) != 2 or not 0 < _number(band[0], "valid_band_hz[0]") < _number(band[1], "valid_band_hz[1]") < rate / 2:
        raise RearCalibrationError("valid_band_hz must be ordered and below Nyquist")
    if any(not isinstance(value, str) for value in _FIELDS.sequence(document["assumptions"], "assumptions")):
        raise RearCalibrationError("assumptions must be text")
    included = _object(document["included_stages"], {"front", "rear"}, "included_stages")
    for side, stages in included.items():
        if stages is not None and (not isinstance(stages, list) or any(not isinstance(stage, str) or stage not in STAGES for stage in stages)):
            raise RearCalibrationError(f"included_stages.{side} contains an unknown stage")
    if case == "acoustic_targets":
        targets = _object(document["targets"], {"frequency_hz", "front", "rear"}, "targets")
        frequencies = _FIELDS.sequence(targets["frequency_hz"], "targets.frequency_hz")
        if len(frequencies) < 2 or any(not band[0] <= _number(f, "target frequency") <= band[1] for f in frequencies):
            raise RearCalibrationError("target frequencies must cover at least two samples in the valid band")
        if any(a >= b for a, b in zip(frequencies, frequencies[1:])):
            raise RearCalibrationError("target frequencies must increase")
        for side in ("front", "rear"):
            values = _FIELDS.sequence(targets[side], f"targets.{side}")
            if len(values) != len(frequencies):
                raise RearCalibrationError(f"targets.{side} must share the frequency grid")
            for value in values:
                if not isinstance(value, list) or len(value) != 2:
                    raise RearCalibrationError("complex targets use [real, imaginary] pairs")
                for part in value:
                    _number(part, "complex target")
        return deepcopy(dict(document))
    if any(value is None for value in included.values()):
        raise RearCalibrationError("electrical settings must declare their included stages")
    _chain(document["front"], rate, "front")
    common_delay = _number(document["common_delay_ms"], "common_delay_ms", 0)
    front_delay = _number(document["front"]["delay_ms"], "front.delay_ms", 0)
    if type(document["rear_muted"]) is not bool:
        raise RearCalibrationError("rear_muted must be boolean")
    boundary = _object(document["boundary"], {"front", "rear"}, "boundary")
    for side, filters in boundary.items():
        _filters(filters, rate, f"boundary.{side}")
        if filters and "boundary_correction" in included[side]:
            raise RearCalibrationError(f"{side} boundary correction is already included")
    rear = _FIELDS.mapping(document["rear"], "rear")
    if rear.get("mode") == "branches":
        _object(rear, {"mode", "bass", "cancellation"}, "rear")
        for branch in ("bass", "cancellation"):
            _chain(rear[branch], rate, f"rear.{branch}")
            if common_delay + front_delay + rear[branch]["delay_ms"] < 0:
                raise RearCalibrationError("add common delay to realize a negative relative rear delay")
    elif rear.get("mode") == "fir":
        _object(rear, {"mode", "coefficients", "sample_rate_hz", "normalization", "added_latency_ms", "sha256"}, "rear")
        values = _FIELDS.sequence(rear["coefficients"], "rear.coefficients")
        if not values or type(rear["sample_rate_hz"]) is not int or rear["sample_rate_hz"] != rate or rear["normalization"] != "as_supplied":
            raise RearCalibrationError("FIR requires coefficients at the selected rate, with explicit as_supplied normalization")
        for value in values:
            _number(value, "FIR coefficient")
        _number(rear["added_latency_ms"], "added_latency_ms", 0)
        if coefficient_sha256(values) != rear["sha256"]:
            raise RearCalibrationError("FIR coefficient hash mismatch")
    else:
        raise RearCalibrationError("rear.mode must be branches or fir")
    return deepcopy(dict(document))


#: The coarse family a changed field path falls in, keyed on the leaf field
#: this document spells; a path on the front chain reads ``front_chain``, an
#: unmapped leaf ``other``, and several families at once ``multiple``. A
#: DISCLOSURE for a reader comparing two settings — never a refusal.
FAMILY_BY_LEAF = {
    "gain_db": "gain", "gain": "gain",
    "delay_ms": "delay", "common_delay_ms": "delay",
    "freq": "band_edge", "order": "band_edge", "q": "band_edge",
    "muted": "mute", "rear_muted": "mute",
}


def changed_section_paths(now: Any, was: Any, prefix: str = "") -> list[str]:
    """Every leaf path, dotted with list indices, at which two settings differ."""
    if isinstance(now, Mapping) and isinstance(was, Mapping):
        return [path for key in sorted(set(now) | set(was))
                for path in changed_section_paths(
                    now.get(key), was.get(key), f"{prefix}.{key}" if prefix else str(key))]
    if isinstance(now, list) and isinstance(was, list) and len(now) == len(was):
        return [path for index, (left, right) in enumerate(zip(now, was))
                for path in changed_section_paths(left, right, f"{prefix}.{index}")]
    return [] if now == was else [prefix]


def section_change_family(paths: Sequence[str]) -> str:
    """One coarse family for a set of changed paths (:data:`FAMILY_BY_LEAF`)."""
    families = {
        "front_chain" if path.startswith(("front", "boundary.front"))
        else FAMILY_BY_LEAF.get(path.rsplit(".", 1)[-1], "other")
        for path in paths
    }
    if len(families) == 1:
        return families.pop()
    return "multiple" if families else ""


def _corner_hz(filters: Sequence[Mapping[str, Any]], suffix: str,
               pick: Callable[..., float]) -> float | None:
    """The corner one chain's band-limiting filters settle on, or ``None``.

    A pass direction is read off the filter type's own name, which
    :data:`BIQUADS` and :data:`COMBOS` already fix, so there is no second
    vocabulary of high- and low-pass shapes. Several filters in one direction
    pass what the steepest of them passes: ``pick`` is ``max`` for a high-pass
    and ``min`` for a low-pass.
    """
    corners = [float(item["parameters"]["freq"]) for item in filters
               if str(item["parameters"].get("type") or "").endswith(suffix)]
    return pick(corners) if corners else None


def rear_operating_facts(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a validated branches document OPERATES at, for a measured report.

    ``band_hz`` is the cancellation branch's own pass band, ``bass_lowpass_hz``
    the bass branch's corner, and ``handover_hz`` the geometric mean of that
    corner and the cancellation high-pass — where the bass branch hands over to
    the inverted one. A filter the document does not carry yields ``None``:
    nothing here is estimated, and an acoustic-targets or ``fir`` document
    names no corners at all.
    """
    rear = (document or {}).get("rear") or {}
    if (document or {}).get("case") != "electrical_dsp" or rear.get("mode") != "branches":
        return {"band_hz": None, "bass_lowpass_hz": None, "handover_hz": None}
    highpass = _corner_hz(rear["cancellation"]["filters"], "Highpass", max)
    lowpass = _corner_hz(rear["cancellation"]["filters"], "Lowpass", min)
    bass_lowpass = _corner_hz(rear["bass"]["filters"], "Lowpass", min)
    return {
        "band_hz": None if highpass is None or lowpass is None else [highpass, lowpass],
        "bass_lowpass_hz": bass_lowpass,
        "handover_hz": None if highpass is None or bass_lowpass is None
                       else math.sqrt(highpass * bass_lowpass),
    }


def diagnostic_seed(sample_rate: int) -> dict[str, Any]:
    chain = {"gain_db": 0.0, "inverted": False, "delay_ms": 0.0, "muted": False, "filters": []}
    return {"kind": KIND, "schema": 1, "case": "electrical_dsp", "sample_rate_hz": sample_rate,
            "phase_convention": PHASE_CONVENTION,
            "geometry": {"cabinet_back_wall_m": 0.2032, "sources": {"front": None, "rear": None}, "details": None},
            "reference": {"quantity": "electrical_filter_transfer", "units": "linear output/input", "level": None},
            "conditions": {}, "valid_band_hz": None,
            "assumptions": ["Untuned diagnostic seed: the acoustic 200 Hz ratio is not an electrical calibration.",
                            "Band-limiting filters remain to be fitted from the full saved dataset, including their phase."],
            "included_stages": {"front": [], "rear": []}, "common_delay_ms": 0.0, "rear_muted": True,
            "front": deepcopy(chain), "boundary": {"front": [], "rear": []},
            "rear": {"mode": "branches", "bass": deepcopy(chain),
                     "cancellation": {**deepcopy(chain), "gain_db": -0.84, "inverted": True, "delay_ms": 1.14}}}


def rear_stage_mixer_names(rear_channel: int) -> tuple[str, str]:
    """The stage's split and sum mixer names, in the order it wires them."""
    return f"rear_out{rear_channel}_split", f"rear_out{rear_channel}_sum"


def compile_rear_stage(document: Mapping[str, Any], *, front_channel: int, rear_channel: int,
                       channel_count: int, tweeter_channel: int) -> dict[str, Any]:
    """Stage after common EQ / physical split, before per-output protection.

    ``document`` must already be through :func:`read_rear_calibration`: the
    caller's read is where the document is bound to ITS sample rate. Rear branch
    delays are relative to the front reference. FIR coefficients replace both
    branches; their declared latency is reported, never added twice. Delays are
    whole-sample, which the branch-peak render can model exactly.
    """
    data = document
    if data["case"] != "electrical_dsp":
        raise RearCalibrationError("acoustic targets still require electrical conversion and causal fitting")
    if type(channel_count) is not int or channel_count < 3 or len({front_channel, rear_channel, tweeter_channel}) != 3 or any(
        type(channel) is not int or not 0 <= channel < channel_count for channel in (front_channel, rear_channel, tweeter_channel)
    ):
        raise RearCalibrationError("declare distinct front/rear/tweeter channels within the physical output count")
    prefix = f"rear_out{rear_channel}"
    filters: dict[str, Any] = {}
    mixers: dict[str, Any] = {}
    pipeline: list[dict[str, Any]] = []

    def chain(name: str, channel: int, value: Mapping[str, Any], delay: float) -> None:
        names = []
        gain = f"{prefix}_{name}_gain"
        filters.update(yaml.safe_load("\n".join(emit_gain_filter(gain, value["gain_db"], inverted=value["inverted"], mute=value["muted"]))))
        names.append(gain)
        if delay:
            delay_name = f"{prefix}_{name}_delay"
            filters.update(yaml.safe_load("\n".join(emit_delay_filter(delay_name, delay_ms=delay))))
            names.append(delay_name)
        for i, item in enumerate(value["filters"]):
            filter_name = f"{prefix}_{name}_{i}"
            filters[filter_name] = deepcopy(item)
            names.append(filter_name)
        pipeline.append({"type": "Filter", "channels": [channel], "names": names})

    common = data["common_delay_ms"]
    front_delay = data["front"]["delay_ms"]
    front = {**data["front"], "filters": [*data["front"]["filters"], *data["boundary"]["front"]]}
    chain("front", front_channel, front, common + front_delay)
    rear = data["rear"]
    if rear["mode"] == "branches":
        split, summed = rear_stage_mixer_names(rear_channel)
        for mixer_name, width_in, width_out, mapping in (
            (split, channel_count, channel_count + 1,
             [(i, [(i, 0.0, False)]) for i in range(channel_count)] + [(channel_count, [(rear_channel, 0.0, False)])]),
            (summed, channel_count + 1, channel_count,
             [(i, [(i, 0.0, False)] + ([(channel_count, 0.0, False)] if i == rear_channel else []))
              for i in range(channel_count)]),
        ):
            mixers.update(yaml.safe_load(emit_mixer(mixer_name, channels_in=width_in,
                                                    channels_out=width_out, mapping=mapping)))
        pipeline.append({"type": "Mixer", "name": split})
        for name, channel in (("bass", rear_channel), ("cancellation", channel_count)):
            chain(name, channel, rear[name], common + front_delay + rear[name]["delay_ms"])
        pipeline.append({"type": "Mixer", "name": summed})
    else:
        name = f"{prefix}_fir"
        filters[name] = {"type": "Conv", "parameters": {"type": "Values", "values": rear["coefficients"]}}
        pipeline.append({"type": "Filter", "channels": [rear_channel], "names": [name]})
        if common:
            chain("fir_delay", rear_channel, {"gain_db": 0, "inverted": False, "muted": False, "filters": []}, common)
    chain("output", rear_channel, {"gain_db": 0, "inverted": False, "muted": data["rear_muted"], "filters": data["boundary"]["rear"]}, 0)
    if common:
        chain(f"common_{tweeter_channel}", tweeter_channel, {"gain_db": 0, "inverted": False, "muted": False, "filters": []}, common)
    return {"filters": filters, "mixers": mixers, "pipeline": pipeline}
