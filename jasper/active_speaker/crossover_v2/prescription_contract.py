# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure assembly of authoring contracts and bounds on a round's evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import MISSING, fields
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.branch_chain import beaming_onset_hz, boost_headroom_by_role
from .conductor_context import _resolve_radiating_diameter_by_role
from jasper.active_speaker.excitation_safety_plan import (
    ExcitationSafetyPlanError,
    resolve_driver_measurement_band_hz,
    resolve_driver_protection_slope_db_per_octave,
)
from jasper.active_speaker.camilla_yaml import MAX_PROGRAM_HEADROOM_DB, _branch_context
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from jasper.active_speaker.measured_crossover_candidate import room_peqs_from_correction
from jasper.active_speaker.measurement_programs import PROGRAM_DOCUMENT_ORDER
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, SIDES_BY_LAYOUT, SPL_RAISE_MARGIN_DB
from jasper.active_speaker import rear_calibration
from jasper.audio_measurement import room_limits as rl
from jasper.bass_extension import dynamic as bass
from jasper.json_fields import finite_float

from . import alignment_prescription as alignment
from . import bass_prescription
from . import blend_prescription as blend
from . import driver_prescription as driver
from . import room_prescription as room
from . import topology_prescription as topology
from .feature_classification import UNCERTAINTY_RANDOM
from .fc_sweep import fc_rejection_scenarios

CONTRACT_COMMAND = "jasper-crossover-prescriber contract"
SECTIONS = tuple(row.purpose for row in PROGRAM_DOCUMENT_ORDER)


def contract_json(value: Mapping[str, Any]) -> str:
    """The exact UTF-8 serialization hashed by packet.contracts (no newline)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def contract_digests(contracts: Mapping[str, Any]) -> dict[str, str]:
    return {name: hashlib.sha256(contract_json(value).encode("utf-8")).hexdigest()
            for name, value in contracts.items()}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def _number(lo: float | None = None, hi: float | None = None, *, exclusive_lo: bool = False) -> dict[str, Any]:
    lo_key = "exclusiveMinimum" if exclusive_lo else "minimum"
    return {"type": "number", **({lo_key: lo} if lo is not None else {}),
            **({"maximum": hi} if hi is not None else {})}


def _document(format_: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    required = format_["required_top_level"]
    return _object({
        "kind": {"const": required["kind"]},
        "artifact_schema_version": {"const": required["artifact_schema_version"]},
        "prescriber": _object({name: {"type": "string", "minLength": 1}
                               for name in required["prescriber"]}, list(required["prescriber"])),
        "rationale": {"type": "string"},
        **properties,
    }, list(required))


def _filter(*, driver_role: bool = False, room_filter: bool = False) -> dict[str, Any]:
    props: dict[str, Any] = {"freq": _number(), "q": _number(), "gain": _number()}
    required = list(props)
    if driver_role:
        props.update(role={"type": "string"}, biquad_type={"enum": sorted(driver.LINEARIZATION_BIQUAD_TYPES)})
        required = ["role", "biquad_type", "freq", "gain"]
    else:
        props["biquad_type"] = {"const": "Peaking"}
        if not room_filter:
            required.append("biquad_type")
    schema = _object(props, required)
    if driver_role:
        schema["allOf"] = [{"if": {"properties": {"biquad_type": {"const": "Peaking"}}},
                            "then": {"required": ["q"]}}]
    return schema


def _request_schema(format_: dict[str, Any], kind: str, version: int,
                    required: dict[str, Any], optional: dict[str, Any]) -> dict[str, Any]:
    props = {"kind": {"const": kind}, "artifact_schema_version": {"const": version}, **required}
    schema = _object({**props, **optional}, list(props))
    for name, description in format_["fields"].items():
        schema["properties"][name]["description"] = description
    return schema


def _mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _preset(candidate: Mapping[str, Any]) -> ActiveSpeakerPreset | None:
    try:
        return ActiveSpeakerPreset.from_mapping(candidate["source_preset"])
    except (KeyError, TypeError, ValueError, ActiveSpeakerConfigError):
        return None


def _speaker(draft: Mapping[str, Any], receipt: Mapping[str, Any],
             preset: ActiveSpeakerPreset | None, candidate: Mapping[str, Any],
             manifest: Mapping[str, Any]) -> dict[str, Any]:
    blend_format = blend.prescription_response_format()
    driver_format = driver.driver_prescription_response_format()
    alignment_format = alignment.alignment_prescription_response_format()
    topology_format = topology.topology_prescription_response_format()
    safety = _mapping(design_draft_view(draft).get("driver_safety_profile"))
    passbands = driver.driver_passbands_from_safety_profile(safety)
    groups = manifest.get("sets")
    takes = [take for group in (groups if isinstance(groups, list) else [])
             if isinstance(group, Mapping) and isinstance(group.get("takes"), list)
             for take in group["takes"] if isinstance(take, Mapping) and take.get("selected")]
    levels = [value for take in takes if (value := finite_float(_mapping(take.get("level")).get("level_db"))) is not None]
    spl_margins = []
    if preset is not None:
        for take in takes:
            level = _mapping(take.get("level"))
            spl = finite_float(level.get("loudest_half_second_db_spl"))
            if spl is not None:
                spl_margins.append(max(0.0, preset.safety.max_commissioning_level_db_spl - spl - SPL_RAISE_MARGIN_DB))
    context = _branch_context(preset, {
        role: {"gain_db": trim} for role, trim in _mapping(candidate.get("role_attenuations_db")).items()
    }) if preset is not None else {role: ((), 0.0) for role in passbands}
    headroom = boost_headroom_by_role(
        branch_context=context,
        linearization=linearization_filters_by_role(_mapping(candidate.get("linearization"))),
        room_peqs=room_peqs_from_correction(_mapping(candidate.get("room_correction")), preset) if preset else (),
        session_volume_db=max(levels) if levels and len(levels) == len(takes) else None,
        spl_headroom_db=min(spl_margins) if spl_margins else None,
    )
    band = _mapping(_mapping(receipt.get("round_measurements")).get("blend")).get("band_hz")
    fc = topology.candidate_topology(SimpleNamespace(source_preset=preset))
    corner = fc["fc_hz"] if fc else None
    delay = None
    if preset is not None:
        delay = alignment.alignment_delay_search_bounds_us(preset)
    diameter = _resolve_radiating_diameter_by_role(draft).get("woofer")
    topology_bounds: dict[str, Any] = {
        "supported_orders": sorted(topology.SUPPORTED_LR_ORDERS),
        "fc_hz": None, "minimum_slope_db_per_octave": None,
        "fc_rejection_rule": "fc_sweep._fc_rejection",
        "beaming_is_a_refusal": False,
        "beaming_ceiling_hz": beaming_onset_hz(diameter) if diameter is not None else None,
    }
    raw_targets = safety.get("targets")
    targets = {t.get("role"): t for t in (raw_targets if isinstance(raw_targets, list) else [])
               if isinstance(t, Mapping)}
    try:
        lower = targets["woofer"]["target_fingerprint"]
        upper = targets["tweeter"]["target_fingerprint"]
        floor = resolve_driver_measurement_band_hz(safety, upper)[0]
        ceiling = resolve_driver_measurement_band_hz(safety, lower)[1]
        topology_bounds.update(
            fc_hz=[floor, ceiling],
            minimum_slope_db_per_octave=resolve_driver_protection_slope_db_per_octave(safety, upper),
            **fc_rejection_scenarios(floor, ceiling, declared_fc_hz=corner),
            slope_db_per_octave_by_order={
                str(order): topology.TopologyPrescription(corner or floor, order, ()).slope_db_per_octave
                for order in topology.SUPPORTED_LR_ORDERS
            },
        )
    except (KeyError, TypeError, ValueError, ExcitationSafetyPlanError):
        pass
    artifacts = {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}
    shared = {"basis_artifacts": artifacts}
    return {
        "way_count": preset.way_count if preset else None,
        "evidence_declarations": {
            "capture_snr": {"fields": {}, "not_uncertainties": dict(_SNR_NOT_AN_UNCERTAINTY)},
            "harmonics": {"fields": deepcopy(_HARMONICS_UNCERTAINTY),
                          "not_uncertainties": dict(_HARMONICS_NOT_AN_UNCERTAINTY),
                          "role_fields": dict(_HARMONICS_ROLE_NOT_AN_UNCERTAINTY)},
            "reflections": {"fields": {}, "not_uncertainties": dict(_REFLECTIONS_NOT_AN_UNCERTAINTY)},
            "not_evaluated": dict(_NOT_EVALUATED),
        },
        "driver": {
            "filters_are_a_total": driver_format["filters_are_a_total"],
            "evidence_status": "evaluated" if passbands else driver.PASSBAND_UNAVAILABLE,
            "schema": _document(driver_format, {
                blend.PACKET_FINGERPRINT_FIELD: {"type": "string"},
                "filters": {"type": "array", "items": _filter(driver_role=True)},
                "pinned_trim_db": {"type": "object", "additionalProperties":
                                   _number(driver.MAX_ATTENUATION_DB, 0.0)},
                driver.EXPECTED_DELTA_FIELD: _number(-driver.EXPECTED_DELTA_BOUND_DB, driver.EXPECTED_DELTA_BOUND_DB),
                driver.DECLARED_TILT_FIELD: _number(-driver.DECLARED_TILT_BOUND_DB_PER_OCTAVE, driver.DECLARED_TILT_BOUND_DB_PER_OCTAVE),
            }),
            "bounds": {
                "passbands_hz": {role: list(band) for role, band in sorted(passbands.items())},
                "chain_scope": driver_format["filters_are_a_total"],
                "trim_pin_scope": driver_format["optional_top_level"]["pinned_trim_db"],
                "max_filters_per_role": driver.DRIVER_MAX_FILTERS_PER_ROLE,
                "q_range_cut": [driver.EVALUABLE_Q_MIN, driver.EVALUABLE_Q_MAX],
                "q_max_boost": driver.DRIVER_MAX_BOOST_Q,
                "boost_headroom": headroom,
                "boost_headroom_rule": f"Program headroom spent must not exceed {MAX_PROGRAM_HEADROOM_DB:g} dB",
                "shelf_rule": driver_format["bounds"]["where_a_shelf_may_sit"],
                "shelf_q": driver.SHELF_Q,
            },
            "refusal_codes": sorted(driver.DRIVER_PRESCRIPTION_REFUSAL_REASONS),
            "prohibited_keys": sorted(blend.PROHIBITED_PRESCRIPTION_KEYS),
            "disclosures": {"classification": "advisory", "subaudible_below_db": driver.DRIVER_MIN_CUT_DB},
        },
        "blend": {
            "filters_are_a_total": blend_format["filters_are_a_total"],
            "evidence_status": "evaluated" if band else blend.REGION_UNAVAILABLE,
            "schema": _document(blend_format, {
                blend.PACKET_FINGERPRINT_FIELD: {"type": "string"},
                "filters": {"type": "array", "items": _filter(), "maxItems": blend.BLEND_MAX_FILTERS},
            }),
            "bounds": {
                "band_hz": band, "max_filters": blend.BLEND_MAX_FILTERS,
                "q_range_cut": [blend.EVALUABLE_Q_MIN, blend.EVALUABLE_Q_MAX],
                "q_max_boost": blend.PRESCRIPTION_MAX_BOOST_Q,
                "max_filter_boost_db": blend.PRESCRIPTION_MAX_FILTER_BOOST_DB,
                "max_composed_boost_db": blend.PRESCRIPTION_MAX_TOTAL_BOOST_DB,
                "boost_route": {"available": False, "reason": blend.BOOST_ROUTE_UNAVAILABLE,
                                "detail": "The route refuses every boost today."},
            },
            "refusal_codes": sorted(blend.BLEND_PRESCRIPTION_REFUSAL_REASONS),
            "prohibited_keys": sorted(blend.PROHIBITED_PRESCRIPTION_KEYS),
        },
        "alignment": {
            "evidence_status": (alignment.ALIGNMENT_NO_CROSSOVER_REGION if preset and preset.way_count == 1
                                else "evaluated" if corner else alignment.PRESCRIPTION_FC_UNKNOWN),
            "entry": alignment_format["entry"], "request_key": alignment_format["key"],
            "schema": _request_schema(alignment_format, alignment.ALIGNMENT_PRESCRIPTION_KIND,
                                      alignment.ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
                                      {**shared, "delay_us": _number(), "basis_delay_us": _number()},
                                      {"basis_note": {"type": "string"},
                                       "polarity": {"enum": sorted(alignment._PINNABLE_POLARITIES)}}),
            "bounds": {"declared_delay_magnitude_us": list(delay) if delay else None,
                       "fc_hz": corner, "lobe_us": alignment.half_period_us(corner) if corner else None,
                       "lobe_applies_to": "abs(delay_us - basis_delay_us)"},
            "refusal_codes": sorted(alignment.ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
        },
        "topology": {
            "evidence_status": (topology.TOPOLOGY_NO_CROSSOVER_REGION if preset and preset.way_count == 1
                                else "evaluated" if topology_bounds["fc_hz"] else topology.TOPOLOGY_MALFORMED),
            "entry": topology_format["entry"], "request_key": topology_format["key"],
            "schema": _request_schema(topology_format, topology.TOPOLOGY_PRESCRIPTION_KIND,
                                      topology.TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
                                      {**shared, "fc_hz": _number(),
                                       "order": {"type": "integer", "enum": sorted(topology.SUPPORTED_LR_ORDERS)}},
                                      {"basis_note": {"type": "string"}}),
            "bounds": topology_bounds,
            "refusal_codes": sorted(topology.TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS),
        },
    }


def room_analysis_bounds(median: room.RoomMedian, persistence: Mapping[str, Any]) -> dict[str, Any]:
    features = persistence.get("features")
    return {
        "cut_floor_db": rl.cut_floor_db(median.spread_db, median.freqs_hz, median.ceiling_hz).tolist(),
        "boost_cap_db": rl.boost_cap_db(median.freqs_hz, median.ceiling_hz).tolist(),
        "admit_boost": ([{
            "feature": dict(feature),
            **rl.admit_boost(freq, freqs_hz=median.freqs_hz, median_db=median.median_db,
                             deviations_db=median.deviations_db, n_positions=median.n_positions).to_dict(),
        } for feature in features
            if isinstance(feature, Mapping) and (freq := finite_float(feature.get("centre_hz"))) is not None]
            if isinstance(features, list) else None),
    }


def _room(raw: Mapping[str, Any], persistence: Mapping[str, Any],
          ceiling: Mapping[str, Any], preset: ActiveSpeakerPreset | None) -> dict[str, Any]:
    format_ = room.room_prescription_response_format()
    sides = list(SIDES_BY_LAYOUT[preset.channel_map.layout]) if preset is not None else None
    filters = {"type": "array", "items": _filter(room_filter=True), "maxItems": rl.ROOM_MAX_FILTERS_PER_SIDE}
    result: dict[str, Any] = {
        "filters_are_a_total": format_["filters_are_a_total"],
        "schema": _document(format_, {
            room.ROOM_MEDIAN_FIELD: {"type": "string"},
            "sides": (_object({side: filters for side in sides}, sides) if sides else
                      {"type": "object", "additionalProperties": filters}),
        }),
        "refusal_codes": sorted(room.ROOM_PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(blend.PROHIBITED_PRESCRIPTION_KEYS),
        "bounds": {key: value for key, value in format_["bounds"].items()
                   if not isinstance(value, str)},
        "evidence_status": room.ROOM_MEDIAN_UNAVAILABLE,
    }
    result["bounds"].update(band_hz=None, ceiling_hz=None, ceiling_source=None,
                            freqs_hz=None, cut_floor_db=None, boost_cap_db=None,
                            taper_knee_hz=None, spatial_support=None, sides=sides,
                            admit_boost=None)
    try:
        median = room.read_room_median(raw)
    except room.RoomPrescriptionRefused as exc:
        result["evidence_status"] = exc.reason
        return result
    result["evidence_status"] = "evaluated"
    features = persistence.get("features")
    result["persistence_status"] = "evaluated" if isinstance(features, list) else "not_evaluated"
    result["bounds"].update(
        band_hz=list(median.band_hz), ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source, ceiling_provenance=dict(ceiling),
        freqs_hz=median.freqs_hz.tolist(),
        taper_knee_hz=rl.taper_knee_hz(median.ceiling_hz),
        spatial_support=rl.spatial_support(median.n_positions),
        level_reference_db=median.level_reference_db,
        **room_analysis_bounds(median, persistence),
    )
    return result


def _bass(evidence: Mapping[str, Any]) -> dict[str, Any]:
    format_ = bass_prescription.bass_prescription_response_format()
    properties = {
        "low_boost_db": {"type": "number", "exclusiveMinimum": bass.LOW_BOOST_DB_MIN,
                         "maximum": bass.NATIVE_LOUDNESS_BOOST_MAX_DB},
        "reference_level_db": _number(bass.REFERENCE_LEVEL_DB_MIN, bass.REFERENCE_LEVEL_DB_MAX),
        "detector_lowpass_hz": _number(bass.DETECTOR_CORNER_HZ_MIN, bass.DETECTOR_CORNER_HZ_MAX),
        "compressor_threshold_dbfs": _number(bass.COMPRESSOR_THRESHOLD_DBFS_MIN, bass.COMPRESSOR_THRESHOLD_DBFS_MAX),
        "compressor_factor": {"type": "number", "exclusiveMinimum": bass.COMPRESSOR_FACTOR_MIN,
                              "maximum": bass.COMPRESSOR_FACTOR_MAX},
        "compressor_attack_s": _number(bass.COMPRESSOR_ATTACK_S_MIN, bass.COMPRESSOR_ATTACK_S_MAX),
        "compressor_release_s": _number(bass.COMPRESSOR_RELEASE_S_MIN, bass.COMPRESSOR_RELEASE_S_MAX),
        "delta_highpass_hz": {"type": ["number", "null"], "minimum": bass.DELTA_HIGHPASS_HZ_MIN},
    }
    for field in fields(bass.DynamicBassDescriptor):
        if field.default is not MISSING:
            properties[field.name]["default"] = field.default
    properties.update({name: {"type": "string", "minLength": 1, "description": description}
                       for name, description in format_["required_top_level"].items()})
    return {
        "schema": _object(properties, sorted(bass._REQUIRED_FIELDS | format_["required_top_level"].keys())),
        "bounds": {"delta_highpass_hz_exclusive_upper_field": "detector_lowpass_hz"},
        "refusal_codes": format_["refusal_reasons"],
        **bass_prescription.bass_evidence_status(evidence),
        "shared_headroom": {
            "adr": "ADR-0257",
            "layers": ["driver_linearization", "room", "bass_extension"],
            "cost": "maximum_output_level_db",
            "detail": "Room, driver and bass boosts share one headroom budget; their cost is lost maximum level.",
            "bass_reserve_function": "jasper.bass_extension.dynamic.dynamic_bass_gain_reserve_db",
        },
    }


def _rear_biquad_shape(kinds: set[str], q: dict[str, Any], *, with_gain: bool = False) -> dict[str, Any]:
    """Match the validator's exact keys and per-kind caps; SHELVING requires gain."""
    props = {"type": {"enum": sorted(kinds)}, "freq": _number(0, exclusive_lo=True), "q": q}
    required = ["type", "freq", "q"]
    if with_gain:
        props["gain"] = _number(hi=rear_calibration.MAX_CHAIN_BOOST_DB)
        required.append("gain")
    return _object(props, required)


def _rear_combo_shape(kinds: set[str], *, even: bool) -> dict[str, Any]:
    """One BiquadCombo kind-group's `parameters`; LinkwitzRiley order is even."""
    order: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": rear_calibration.MAX_COMBO_ORDER}
    if even:
        order["multipleOf"] = 2
    return _object({"type": {"enum": sorted(kinds)}, "freq": _number(0, exclusive_lo=True), "order": order},
                   ["type", "freq", "order"])


def _rear_filter() -> dict[str, Any]:
    """One filter entry, ``{type, parameters}``, split into exact per-kind shapes.

    Every kind's own key set and numeric caps match ``rear_calibration``'s
    validator exactly (see ``MAX_RESONANT_Q``/``MAX_ALLPASS_Q`` for Biquad,
    ``MAX_COMBO_ORDER`` and LinkwitzRiley's even-order rule for BiquadCombo).
    ``freq``'s Nyquist ceiling depends on the document's own declared
    ``sample_rate_hz`` and is not a schema constant; see
    ``bounds.freq_hz_upper_bound_rule``.
    """
    resonant_capped = rear_calibration.BIQUADS - rear_calibration.SHELVING - {"Allpass"}
    shelf_capped = rear_calibration.SHELVING - {"Peaking"}
    linkwitz_riley = {kind for kind in rear_calibration.COMBOS if kind.startswith("LinkwitzRiley")}
    biquads = [
        _rear_biquad_shape(resonant_capped, _number(0, rear_calibration.MAX_RESONANT_Q, exclusive_lo=True)),
        _rear_biquad_shape({"Allpass"}, _number(0, rear_calibration.MAX_ALLPASS_Q, exclusive_lo=True)),
        _rear_biquad_shape(shelf_capped, _number(0, rear_calibration.MAX_RESONANT_Q, exclusive_lo=True), with_gain=True),
        _rear_biquad_shape({"Peaking"}, _number(0, exclusive_lo=True), with_gain=True),
    ]
    combos = [
        _rear_combo_shape(rear_calibration.COMBOS - linkwitz_riley, even=False),
        _rear_combo_shape(linkwitz_riley, even=True),
    ]
    return {"type": "object", "oneOf": [
        _object({"type": {"const": "Biquad"}, "parameters": shape}, ["type", "parameters"]) for shape in biquads
    ] + [
        _object({"type": {"const": "BiquadCombo"}, "parameters": shape}, ["type", "parameters"]) for shape in combos
    ]}


def _rear_filters_array() -> dict[str, Any]:
    return {"type": "array", "maxItems": rear_calibration.MAX_FILTERS_PER_CHAIN, "items": _rear_filter()}


def _rear_chain(filters: dict[str, Any]) -> dict[str, Any]:
    return _object({
        "gain_db": _number(rear_calibration.MIN_CHAIN_GAIN_DB, 0.0),
        "inverted": {"type": "boolean"}, "delay_ms": _number(), "muted": {"type": "boolean"},
        "filters": filters,
    }, ["gain_db", "inverted", "delay_ms", "muted", "filters"])


def _rear_calibration_schema() -> dict[str, Any]:
    filters = _rear_filters_array()
    chain = _rear_chain(filters)
    stages = {"type": "array", "items": {"enum": sorted(rear_calibration.STAGES)}}
    properties = {
        "kind": {"const": rear_calibration.KIND},
        "schema": {"const": 1},
        "case": {"const": "electrical_dsp"},
        "sample_rate_hz": {"type": "integer", "minimum": 1, "description": "must equal the installed DSP rate"},
        "phase_convention": {"const": rear_calibration.PHASE_CONVENTION},
        "geometry": _object({
            "cabinet_back_wall_m": {"type": ["number", "null"], "minimum": 0},
            "sources": _object({"front": {}, "rear": {}}, ["front", "rear"]),
            "details": {},
        }, ["cabinet_back_wall_m", "sources", "details"]),
        "reference": _object({
            "quantity": {"const": "electrical_filter_transfer"},
            "units": {"type": "string", "minLength": 1},
            "level": {},
        }, ["quantity", "units", "level"]),
        "conditions": {"type": "object"},
        "valid_band_hz": {"type": ["array", "null"], "items": _number(0, exclusive_lo=True), "minItems": 2, "maxItems": 2},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "included_stages": _object({"front": stages, "rear": stages}, ["front", "rear"]),
        "front": chain,
        "boundary": _object({"front": filters, "rear": filters}, ["front", "rear"]),
        "common_delay_ms": _number(0.0),
        "rear_muted": {"type": "boolean"},
        "rear": _object({"mode": {"const": "branches"}, "bass": chain, "cancellation": chain},
                        ["mode", "bass", "cancellation"]),
    }
    return _object(properties, list(properties))


def _rear() -> dict[str, Any]:
    """Electrical branches only; see ADR-0318, ADR-0322 and ADR-0324."""
    return {
        "document_section": "rear_calibration",
        "case": "electrical_dsp",
        "mode": "branches",
        "schema": _rear_calibration_schema(),
        "bounds": {
            "freq_hz_upper_bound_rule": (
                "every filter's freq must stay strictly below the document's own "
                "sample_rate_hz / 2 (Nyquist); freq is otherwise required to be > 0"
            ),
            "max_filters_per_chain": rear_calibration.MAX_FILTERS_PER_CHAIN,
            "chain_gain_db": [rear_calibration.MIN_CHAIN_GAIN_DB, 0.0],
            "chain_gain_rule": (
                "front, rear.bass and rear.cancellation gain_db is an attenuation between "
                f"{rear_calibration.MIN_CHAIN_GAIN_DB:g} and 0 dB: a rear weight above 1 is the same "
                "filter boost on both rear branches (ADR-0327), never front attenuation"
            ),
            "resonant_q_max": rear_calibration.MAX_RESONANT_Q,
            "allpass_q_max": rear_calibration.MAX_ALLPASS_Q,
            "combo_order_max": rear_calibration.MAX_COMBO_ORDER,
            "biquad_kinds": sorted(rear_calibration.BIQUADS),
            "combo_kinds": sorted(rear_calibration.COMBOS),
            "gain_kinds": sorted(rear_calibration.SHELVING),
            "stage_kinds": sorted(rear_calibration.STAGES),
            "gain_rule": (
                "Peaking, Lowshelf and Highshelf gain must not exceed "
                f"+{rear_calibration.MAX_CHAIN_BOOST_DB:g} dB; "
                "a boost is charged to program headroom (ADR-0326)"
            ),
            "emitted_delay_rule": (
                "common_delay_ms + front.delay_ms + a rear branch's own delay_ms must sum to >= 0; "
                "add common delay to realize a negative relative rear delay"
            ),
            "branch_delay_is_not_acoustic_delay": (
                "a branch's raw delay_ms is not its acoustic delay: the branch's own filters add delay"
            ),
            "boundary_correction_rule": (
                "included_stages.<side> must not list boundary_correction while boundary.<side> carries filters"
            ),
            "comparison_scope": (
                "a variant changes ONE control family -- rear gain, rear relative delay, or one band "
                "edge -- and carries every other field of the incumbent's section verbatim, including "
                "the front chain and the filter structure"
            ),
            "rear_muted_reference": "the same section with rear_muted: true is the rear-muted reference",
            "inheritance_rule": (
                "an absent rear_calibration key inherits the base's section; null clears the stage and "
                "the rear output is then muted"
            ),
        },
    }


def prescription_contracts(*, draft: Mapping[str, Any] | None = None,
                           receipt: Mapping[str, Any] | None = None,
                           candidate: Mapping[str, Any] | None = None,
                           room_median: Mapping[str, Any] | None = None,
                           room_persistence: Mapping[str, Any] | None = None,
                           room_ceiling: Mapping[str, Any] | None = None,
                           bass_evidence: Mapping[str, Any] | None = None,
                           applied_profile: Mapping[str, Any] | None = None,
                           manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidate = candidate or {}
    preset = _preset(candidate) or _preset({"source_preset":
        _mapping((applied_profile or {}).get("recomposition_snapshot")).get("preset")})
    return {name: (_speaker(draft or {}, receipt or {}, preset, candidate, manifest or {}) if name == "speaker" else
                   _room(room_median or {}, room_persistence or {}, room_ceiling or {}, preset) if name == "room" else
                   _bass(bass_evidence or {}) if name == "bass" else _rear()) for name in SECTIONS}


_SNR_NOT_AN_UNCERTAINTY: dict[str, str] = {'<role>_snr_db': "the worst per-band signal-to-noise ratio over the bands that decide this DRIVER role's MAGNITUDE claims — its level and its overlap-band trim. A ratio is not a spread about a reading: it BOUNDS the random error a level measured in that band can carry, and it does not shrink as captures are added, because it is a property of the capture conditions rather than of how many times they were repeated", '<role>_snr_verdict': "the policy's own answer about the figure above, in jasper.audio_measurement.snr_policy's per-band rank — a REFUSAL vocabulary that ships a shortfall in dB, deliberately not the quality_model trust labels it resembles. The words are not spelled here: they have an owner, and a copy that agrees today is still a copy. A verdict, not a quantity: there is nothing here to be uncertain by", '<role>_snr_band': 'which band produced the worst reading above. A label, not a quantity', '<role>_alignment_snr_db': "the same worst-band ratio over the bands that decide this DRIVER role's ALIGNMENT claims — polarity and delay — which need far more SNR because a null of depth D cannot be measured with less than roughly D + 10 dB. Published apart from the magnitude figure rather than pooled with it: the two answer different questions under different floors, and one number would let a capture that is fine for a trim read as fine for a null depth", '<role>_alignment_snr_verdict': "the same policy's answer about the alignment figure, under the alignment floor rather than the magnitude one — which is why one capture can legitimately carry a passing magnitude verdict and a refusing alignment one at the same time. A verdict, not a quantity", '<role>_alignment_snr_band': 'which band produced the worst alignment reading. A label, not a quantity', '<role>_pilot_snr_db': "the quiet-pilot in-band SNR. Null when no usable ambient window was captured. Pilot roles include 'summed'. A ratio, not a spread", 'pilot_ambient': 'whether usable ambient evidence is present or unavailable; unavailable is not low SNR. A label', 'pilot_snr_ok': "whether every pilot cleared its SNR floor; null means no pilots or no usable ambient, never a pass. A verdict, not a spread", 'gain_plan_snr_floor_ok': 'the room-quality gate: whether the ambient report cleared the floor the target capture level needs. False also when that report was missing or unreadable, so it is a gate outcome rather than a measurement, and never a spread'}


_HARMONICS_UNCERTAINTY: dict[str, dict[str, str]] = {'h{order}_repeat_spread_db': {'kind': UNCERTAINTY_RANDOM, 'of': "how far this order's reading moved across the sweep repeats of one role INSIDE ONE CAPTURE — the sample standard deviation over those repeats. It is random, and unusually cleanly so: a MEASURE capture is one pose, so its repeats share the microphone position, the session volume, the graph and the drive, and what is left to differ is capture noise. It is the same statistic linearization_envelope.compute_sigma_curve owns and the runbook's sigma table calls repeatability sigma(f), read here at a distortion ratio instead of a magnitude. Like every sample spread it CONVERGES as repeats are added rather than shrinking; what falls with more repeats is the standard error of the pooled median beneath it. Absent (null) below two real repeats, where it is undefined — never 0.0, which would say the repeats agreed. It is NOT a cross-pose spread: pooling two captures would mix this with whatever differs between takes, which is why a round with two MEASURE captures publishes two role blocks rather than one merged one"}}


_HARMONICS_NOT_AN_UNCERTAINTY: dict[str, str] = {'h{order}_below_fundamental_db': "this order's level MINUS the fundamental's at the same EXCITATION frequency — the conventional 'HD2 sits 46 dB down' reading, negative for a well-behaved driver, pooled as the median over the capture's repeats. A reading, not a spread about one. It carries a SYSTEMATIC error that this block does not publish as a field and will not pretend it has bounded: the microphone calibration enters each curve at its own acoustic frequency, so the ratio inherits C(N*f) - C(f), the calibration curve's own slope across an octave. That error is zero only where the calibration is flat across an octave, it does not shrink with repeats, and quantifying it needs the calibration file's slope at each published frequency — which is the field this block would add if the figure were ever read to a tighter tolerance than the roughly 1 dB the rows are rounded to", 'h{order}_floor_below_fundamental_db': "the measured noise floor in the SAME units as the reading above — what a phantom window between the harmonic images reads, so a reading that approaches it is describing the instrument rather than the driver. It BOUNDS an error without being one, exactly as the capture_snr block's figures do, and reading it as a spread that more captures would shrink is the mistake the two lists exist to prevent. It is an ESTIMATE and not a bound in one further respect the reader is owed: the phantom window is narrower than the image window it describes, so the level is scaled by the window-length ratio, and that scaling assumes the floor is spectrally flat across the window — true for capture noise, approximate for the deconvolution's regularization residue", 'h{order}_floor_limited': "true where the reading sits within 6 dB of the floor above, by majority vote across the capture's repeats. A VERDICT about whether a point describes the driver at all, not a quantity: where it is true the reading is real only as an upper bound. Points past the order's own band edge are null here rather than false, because a comparison against a null reading is not a clean point", 'hz': 'the excitation frequency the row was sampled at — one of a fixed ladder, not a per-round choice. A coordinate, not a measurement', 'fundamental_re_band_median_db': 'the pooled fundamental minus its own band median. Published because every ratio in the row divides by the fundamental: a notch at this excitation frequency inflates the ratios on this row with no change in harmonic energy at all, and a reader without this column would read that as distortion. A reading about the response, not a spread', 'thd_percent': "the root-sum-square of the published orders over the fundamental, in percent, computed on the band where EVERY order is real so a total cannot quietly lose a term above one order's edge. A reading. Null where the all-orders band does not reach"}


_HARMONICS_ROLE_NOT_AN_UNCERTAINTY: dict[str, str] = {'role': "which driver's own sweep this block reads. A label", 'wav_sha256_12': "the first 12 hex of the capture's digest — the same identity the positions rows and the capture_snr block name a capture by, so a reader can join them. An identity, not a measurement", 'n_sweeps': "how many of this role's sweep repeats INSIDE this capture the rows below were pooled over. A count, and the n that h{order}_repeat_spread_db must be judged against — a spread over two repeats is a very different statement from one over six", 'sweep': "the excitation this reading was taken from, and the provenance that says what the numbers could and could not cover. f1_hz/f2_hz are the sweep's own bounds and L_s its Novak time constant — the ONE parameter every harmonic offset derives from, since order N's image sits exactly L*ln(N) ahead of the linear impulse response and is windowed to a fraction of the distance to order N+1's centre. read_band_hz is where the rows are reported: its bottom is f1 plus a 0.25-octave trim for the sweep's fade-in, which is an artefact and not distortion, and its top is f2 divided by the LOWEST published order. Each higher order stops earlier still, at f2/order, because an order survives the deconvolution only while N*f stays inside the sweep's own passband — that bound is the passband, NOT Nyquist, and past it the columns are null. Provenance, not a measurement", 'drive': 'the level this reading was taken at, in every reference it has: stimulus and effective peak dBFS describe what was PLAYED, capture peak and RMS dBFS what was RECORDED, each re its own full scale. NOT SPL — no acoustic reference exists anywhere in this corpus. Load-bearing rather than housekeeping: distortion is a function of drive, so a ratio quoted without this names nothing and two blocks at different drives are not comparable. Readings, not spreads', 'images_clean': "true when every harmonic window for this role sat in program silence rather than reaching back into the previous segment's audio. A verdict about the reading's conditions", 'worst_clearance_s': "the smallest margin, over this capture's sweeps, between the program silence in front of a sweep and what its harmonic windows need. NEGATIVE means a window reached into prior audio: the read is still returned, because the window's taper is near zero at that edge, but it is no longer clean and the reader is told rather than left to assume. A duration, not a spread", 'worst': "the highest (dirtiest) point of each order that CLEARS the floor, with the frequency it sits at — pooled exactly as the rows are, so the headline cannot contradict its own table. Null for an order where nothing clears its floor, which is the honest reading of 'nothing measurable here' and the ordinary answer for a tweeter at a low drive. A reduction of the readings, not an uncertainty", 'floor_limited_fraction': "the share of the reported grid where this order is floor-limited. Near 1.0 the order was buried and the block is describing the instrument; near 0.0 the reading is the driver's. A coverage fraction — it says how much of the curve is trustworthy, never how uncertain a value is", 'rows': 'the per-frequency readings themselves; their columns are declared above'}


_REFLECTIONS_NOT_AN_UNCERTAINTY: dict[str, str] = {'reflector_path_distance_m': "how much FURTHER the delayed copy travelled than the direct sound — tau times the speed of sound, in metres. An excess path length, not a distance to the reflector: a mirror-image bounce off a surface d away from a coincident source and microphone travels 2d further, and this corpus banks no geometry that would let the packet halve it for one case and not another. Published to the millimetre, which is already finer than anything it supports. A READING, derived from one. The SYSTEMATIC it carries and this block does not publish as a field: the speed of sound is assumed, not measured, and moves 0.606 m/s per Kelvin — 0.18 % of 343 — so a room 10 K from the assumption below shifts every distance here by 1.8 %. That is small next to the error already banked beside it: the comment on interference_nulls.LADDER_ARRIVAL_TOLERANCE records the fitted ladder tau sitting 6.671 % to 7.540 % BELOW the directly measured arrival tau across the four S0 groupings, and null_registry.ladder_arrival_gap carries this round's own figure. There is no uncertainty ON the distance to publish: nothing banks a sigma for tau, and the two things that bound it — that gap, and each rung's own rung_error_spacings — are already on the registry, so this block points at them rather than reducing them to a second number", 'tau_ladder_us': 'the fitted ladder delay this distance was converted from, in microseconds — echoed from honesty_mask.null_registry.tau_ladder_us, which stays its one authority. It is here so the multiply is auditable in place and so the distance cannot be paired with a tau it was not taken from, the same reason cross_seat_sigma sits inside the positions block rather than beside it. A READING: the frequency-domain least-squares fit over at least MIN_LADDER_RUNGS consecutive rungs, corroborated against an independent time-domain arrival before any of it is published', 'speed_of_sound_m_s': "the constant the conversion used, in m/s — jasper.audio_measurement.null_walk.DEFAULT_SOUND_SPEED_M_S, the repo's one definition, consumed here rather than restated. An ASSUMPTION, not a measurement and not a spread: it is published so the arithmetic is reproducible and so a reader who knows the room's real temperature can redo it", 'speed_of_sound_air_temperature_c': "the air temperature the constant above is the conventional figure for. An assumption's own assumption, published for the same reason: nothing in this corpus measures room temperature, so a reader is owed the number that was assumed instead of one that was read", 'positions[].gate_moved_rms_db': "how far THAT capture's reflection gate moved the response's shape, in dB RMS over the band the gate can be priced on. A READING, and one that is uninterpretable alone: the same small number means 'genuinely clean' beside gate_floor_source=measured_reflection and 'nothing was proven about reflections' beside search_span_bound. Its band is the capture's own trusted floor intersected with what the stimulus radiated, and where that intersection is empty the field is null rather than a figure taken over noise", 'positions[].gate_reflection_delay_ms': "when the first reflection arrived AFTER the direct sound, at that capture, in milliseconds. A READING. It is a DELAY, deliberately not the gating block's absolute first_reflection_ms, whose origin is the deconvolution window's and means nothing to a reader. Null — never 0.0 — on a capture whose window was capped at the search ceiling: no reflection was found, so there is none to time. Its own reflector path is NOT converted here: it is a different tau, from a different instrument, at one pose rather than fitted across the cloud", 'positions[].gate_entanglement_floor_hz': "the ROOM's floor at that seat, in Hz — below it no gate window, however long, separates the speaker from the room, so nothing there is a speaker measurement. A DERIVED reading, and one that must be read beside positions[].gate_entanglement_floor_source, which names which of three things produced it: a measured reflection timed it, the operator's declared rig geometry gives it, or it is unknown. Declared is not measured and never prints as if it were. Null with an unknown source is the ordinary state on a rig whose first bounce arrives while the direct sound is still decaying — the reflection finder structurally never fires there — and is resolved by declaring the geometry, not by measuring harder. Banked per SEAT because it is derived at that seat's own mark distance, though every pose a round walks declares the same distance today, so these rows currently carry one number (DeclaredGeometry.first_bounce_s)", 'verify.gate.entanglement_floor_hz': "the same derivation as positions[].gate_entanglement_floor_hz, for the VERIFY capture rather than a cloud seat, and read beside its own verify.gate.entanglement_floor_source. It survives an ungateable capture, unlike every other number in this block: the geometry that sets it is the rig's, not the window's", 'verify.gate.moved_rms_db': "the same reading as positions[].gate_moved_rms_db, for the VERIFY capture rather than a cloud seat. Read it beside verify.gate.reflection_measured, which is that capture's gate_floor_source in the one bit a reader needs", 'verify.gate.reflection_delay_ms': 'the same reading as positions[].gate_reflection_delay_ms, for the VERIFY capture. Null when that capture found no reflection, which is what the whole 2026-07-30 corpus was'}


_NOT_EVALUATED = {'vertical_plane_response': 'no claim in this packet reads an elevation — every aggregate pools seats without regard to height, and nothing analyses a raised pose on its own — so no banked verdict sees a floor or ceiling bounce, and what a filter of either sign does off the horizontal plane is unmeasured rather than shown to be safe. positions[].vertical_deg says which seats, if any, were raised; a round whose seats are all 0 sampled the horizontal plane alone', 'lateral_poses[].position_deg': 'this round banked no lateral walk poses, so no pose in it carries a numeric bearing. Whether its CLOUD seats do is a separate question with its own answer — see positions.angle_deg', 'candidates': 'no take this round banked names a candidate, so nothing here says which configurations were played against each other; a round that cycled no candidates measured one graph', 'per_bin_minimum_phase_class': 'No feature classification is banked. Driver filters remain admissible within physical and numerical limits and are reported as unvouched. Run jasper-round-views classify-features if that evidence would help choose an experiment.', 'drivers.passbands_hz': "no driver limits were supplied, so this packet cannot say where each driver's own band starts and ends; a per-driver prescription has no bound to be checked against and is refused", 'findings': 'no attributed finding reaches this packet under any phase; findings.phases says per phase whether a set was banked, empty or unreadable, and findings.summary.echo_band_hz the band the two scanned sets could have found anything in'}


_SNR_ROLE_SUFFIXES: tuple[tuple[str, str], ...] = tuple(sorted(((shape.removeprefix('<role>'), shape) for shape in _SNR_NOT_AN_UNCERTAINTY if shape.startswith('<role>')), key=lambda pair: len(pair[0]), reverse=True))

def snr_shape(column: str) -> str | None:
    """Which declared shape ``column`` is an instance of, or ``None``.

    ``None`` names the field in the block's ``undeclared_fields`` rather than
    letting it travel as a figure no reader was told the kind of.
    """
    if column in _SNR_NOT_AN_UNCERTAINTY:
        return column
    for suffix, shape in _SNR_ROLE_SUFFIXES:
        if column.endswith(suffix) and len(column) > len(suffix):
            return shape
    return None
