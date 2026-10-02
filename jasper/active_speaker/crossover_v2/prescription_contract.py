# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure assembly of authoring contracts and bounds on a round's evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import fields
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.design_inputs import declared_by_target
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.evidence_reasons import SET_REQUIRED, EvidenceUnavailable, unavailable
from jasper.audio_measurement.piston import beaming_onset_hz
from jasper.audio_measurement.trusted_band import within_trusted
from jasper.active_speaker.excitation_safety_plan import (
    ExcitationSafetyPlanError,
    resolve_driver_measurement_band_hz,
    resolve_driver_protection_slope_db_per_octave,
)
from jasper.active_speaker.branch_chain import branch_chain_peak_db
from jasper.active_speaker.camilla_yaml import MAX_PROGRAM_HEADROOM_DB, PROGRAM_HEADROOM_BINDING, PROGRAM_HEADROOM_EXHAUSTED
from jasper.active_speaker.candidate_parts import COMPOSITION_INVALID, program_charge_db
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError
from jasper.active_speaker.measurement_programs import PROGRAM_DOCUMENT_ORDER, programs_for_topology
from jasper.active_speaker.profile import (
    ActiveSpeakerConfigError, ActiveSpeakerPreset, SIDES_BY_LAYOUT, SPL_RAISE_MARGIN_DB, required_driver_roles,
)
from jasper.active_speaker import rear_calibration
from jasper.audio_measurement import room_limits as rl
from jasper.bass_extension import dynamic as bass
from jasper.dsp_control.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.platform.driver_gain import DRIVER_TRIM_MIN_DB
from jasper.platform.biquad import HEADROOM_MARGIN_DB, PEAK_EPS_DB
from jasper.platform.json_fields import as_mapping, finite_float
from jasper.audio_routes.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup, unknown_output_hardware
from jasper.platform.speaker_layout import WAY_COUNT_BY_MAIN_MODE

from . import alignment_prescription as alignment
from . import bass_prescription
from . import blend_prescription as blend
from . import driver_prescription as driver
from . import room_prescription as room
from . import topology_prescription as topology
from .feature_classification import UNCERTAINTY_RANDOM
from .corner_admissibility import fc_rejection_scenarios
from .journey import PHASE_MEASURE
from .pose_curve import WINDOW_GATED
from .position_cycle import curve_band, take_curves

CONTRACT_COMMAND = "jasper-crossover-prescriber contract"
#: A round that banked no candidate has no base its packet may charge (ADR-0371).
BASE_NOT_BANKED = "base_not_banked"
SECTIONS = tuple(row.purpose for row in PROGRAM_DOCUMENT_ORDER)
#: The rear section's name and candidate field; unlike the other layers, no module owns a constant for it.
_REAR_SECTION = "rear_calibration"


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
        "prescriber": _object({name: {"type": "string"}
                               for name in format_["optional_top_level"]["prescriber"]}, []),
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


def _preset(candidate: Mapping[str, Any], applied_profile: Mapping[str, Any]) -> ActiveSpeakerPreset | None:
    for raw in (candidate.get("source_preset"), as_mapping(applied_profile.get("recomposition_snapshot")).get("preset")):
        try:
            return ActiveSpeakerPreset.from_mapping(raw)
        except (TypeError, ValueError, ActiveSpeakerConfigError):
            continue
    return None


def contract_programs(sources: Mapping[str, Any]) -> tuple[str, ...]:
    """Resolve banked outputs without consulting the machine reading the round."""
    try:
        return programs_for_topology(OutputTopology.from_mapping(as_mapping(sources.get("draft")).get("topology")))
    except ValueError:
        pass
    preset = _preset(as_mapping(sources.get("candidate")), as_mapping(sources.get("applied_profile")))
    groups: tuple[SpeakerGroup, ...] = ()
    if preset is not None:
        mode = next(mode for mode, count in WAY_COUNT_BY_MAIN_MODE.items() if count == preset.way_count)
        groups = tuple(SpeakerGroup(side, side, side, mode, channels=tuple(
            SpeakerChannel(output.driver_role, output.driver_role == "tweeter",
                           physical_output_index=output.index, output_variant=output.output_variant)
            for output in preset.channel_map.outputs if output.side == side
        )) for side in SIDES_BY_LAYOUT[preset.channel_map.layout])
        if preset.local_subwoofer is not None:
            groups += (SpeakerGroup("subwoofer", "subwoofer", "subwoofer", "subwoofer"),)
    return programs_for_topology(OutputTopology("", "", unknown_output_hardware(), groups))


def _base_charge(candidate: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """The program charge of the candidate the round banked, or the code that says why there
    is none; a banked candidate that does not reopen refuses the contract by its own code.
    Read from the round alone, so a packet is built from banked inputs (ADR-0371).
    """
    if not candidate:
        return None, BASE_NOT_BANKED
    base = MeasuredCrossoverCandidate.from_mapping(candidate)
    try:
        return program_charge_db(base), None
    except (MeasuredCrossoverCandidateError, ActiveSpeakerConfigError) as exc:
        return None, getattr(exc, "code", COMPOSITION_INVALID)


def _blend_band(corner: float | None, takes: list[Mapping[str, Any]]) -> tuple[list[float] | None, dict[str, Any]]:
    """The blend region, Fc ÷ 2 to Fc × 2 clipped to the gated trusted band of every kept MEASURE take
    whose pose names no driver, and its status (ADR-0402)."""
    if corner is None:
        return None, unavailable(blend.REGION_UNAVAILABLE)
    overlap = overlap_band_hz(corner)
    try:
        trusted = [curve_band(take, curve) for take in takes
                   if take.get("phase") == PHASE_MEASURE and not as_mapping(take.get("pose")).get("driver")
                   for curve in take_curves(take, WINDOW_GATED) or ()]
    except EvidenceUnavailable as exc:
        return None, unavailable(exc.reason, exc.detail)
    band = within_trusted(overlap, *trusted)
    if band is None:
        return None, unavailable(blend.REGION_UNAVAILABLE, {"overlap_band_hz": list(overlap), "trusted_band_hz": [
            max((one.low_hz for one in trusted if one.low_hz is not None), default=None),
            min((one.high_hz for one in trusted if one.high_hz is not None), default=None)]})
    return list(band), {"status": "available"}


def _speaker(draft: Mapping[str, Any], preset: ActiveSpeakerPreset | None,
             candidate: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    blend_format = blend.prescription_response_format()
    driver_format = driver.driver_prescription_response_format()
    alignment_format = alignment.alignment_prescription_response_format()
    topology_format = topology.topology_prescription_response_format()
    safety = as_mapping(design_draft_view(draft).get("driver_safety_profile"))
    passbands = driver.driver_passbands_from_safety_profile(safety)
    groups = manifest.get("sets")
    takes = [take for group in (groups if isinstance(groups, list) else [])
             if isinstance(group, Mapping) and isinstance(group.get("takes"), list)
             for take in group["takes"] if isinstance(take, Mapping) and take.get("selected")]
    levels = [value for take in takes if (value := finite_float(as_mapping(take.get("level")).get("level_db"))) is not None]
    spl_margins = []
    if preset is not None:
        for take in takes:
            level = as_mapping(take.get("level"))
            spl = finite_float(level.get("loudest_half_second_db_spl"))
            if spl is not None:
                spl_margins.append(max(0.0, preset.safety.max_commissioning_level_db_spl - spl - SPL_RAISE_MARGIN_DB))
    spent, reason = _base_charge(candidate)
    linearization = linearization_filters_by_role(as_mapping(candidate.get("linearization")))
    headroom = {role: {
        "composed_boost_db": max(0.0, branch_chain_peak_db(linearization.get(role, ()))),
        "program_headroom_spent_db": spent,
        "program_headroom_remaining_db": None if spent is None else max(0.0, MAX_PROGRAM_HEADROOM_DB - spent),
        "max_program_headroom_db": MAX_PROGRAM_HEADROOM_DB,
        "session_volume_db": max(levels) if levels and len(levels) == len(takes) else None,
        "spl_headroom_db": min(spl_margins) if spl_margins else None,
        "binding": PROGRAM_HEADROOM_BINDING if spent is not None and spent >= MAX_PROGRAM_HEADROOM_DB else None,
        "reason": reason,
    } for role in (required_driver_roles(preset.way_count) if preset else sorted(passbands))}
    fc = topology.candidate_topology(SimpleNamespace(source_preset=preset))
    corner = fc["fc_hz"] if fc else None
    band, blend_status = _blend_band(corner, takes)
    delay = None
    if preset is not None:
        delay = alignment.alignment_delay_search_bounds_us(preset)
    diameter = declared_by_target(draft, "radiating_diameter_mm").get("woofer")
    topology_bounds: dict[str, Any] = {
        "supported_orders": sorted(topology.SUPPORTED_LR_ORDERS),
        "fc_hz": None, "minimum_slope_db_per_octave": None,
        # A stable rule id, not a module path: it is hashed into the contract
        # digests every round packet carries, so it keeps its name across renames.
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
    shared = {"basis_artifacts": {"type": "array", "items": {"type": "string"}}}
    one_way = preset is not None and preset.way_count == 1
    return {
        "way_count": preset.way_count if preset else None,
        "evidence_declarations": {
            "capture_snr": {"fields": {}, "not_uncertainties": dict(_SNR_NOT_AN_UNCERTAINTY)},
            "harmonics": {"fields": deepcopy(_HARMONICS_UNCERTAINTY),
                          "not_uncertainties": dict(_HARMONICS_NOT_AN_UNCERTAINTY),
                          "role_fields": dict(_HARMONICS_ROLE_NOT_AN_UNCERTAINTY)},
            "not_evaluated": dict(_NOT_EVALUATED),
        },
        "driver": {
            "filters_are_a_total": driver_format["filters_are_a_total"],
            **({"status": "available"} if passbands else unavailable(driver.PASSBAND_UNAVAILABLE)),
            "schema": _document(driver_format, {
                blend.PACKET_FINGERPRINT_FIELD: {"type": "string"},
                "filters": {"type": "array", "items": _filter(driver_role=True)},
                "pinned_trim_db": {"type": "object", "additionalProperties":
                                   _number(DRIVER_TRIM_MIN_DB, 0.0)},
                driver.EXPECTED_DELTA_FIELD: _number(-driver.EXPECTED_DELTA_BOUND_DB, driver.EXPECTED_DELTA_BOUND_DB),
                driver.DECLARED_TILT_FIELD: _number(-driver.DECLARED_TILT_BOUND_DB_PER_OCTAVE, driver.DECLARED_TILT_BOUND_DB_PER_OCTAVE),
            }),
            "bounds": {
                "passbands_hz": {role: list(band) for role, band in sorted(passbands.items())},
                "passband_scope": driver_format["bounds"]["freq_must_be_inside"],
                "chain_scope": driver_format["filters_are_a_total"],
                "trim_pin_scope": driver_format["optional_top_level"]["pinned_trim_db"],
                "max_filters_per_role": driver.DRIVER_MAX_FILTERS_PER_ROLE,
                "q_range_cut": [driver.EVALUABLE_Q_MIN, driver.EVALUABLE_Q_MAX],
                "q_max_boost": driver.DRIVER_MAX_BOOST_Q,
                "boost_headroom": headroom,
                "boost_headroom_rule": (f"Program headroom spent must not exceed {MAX_PROGRAM_HEADROOM_DB:g} dB; "
                                        f"composition refuses {PROGRAM_HEADROOM_EXHAUSTED} past it"),
                "shelf_rule": driver_format["bounds"]["where_a_shelf_may_sit"],
                "shelf_q": driver.SHELF_Q,
            },
            "refusal_codes": sorted(driver.DRIVER_PRESCRIPTION_REFUSAL_REASONS),
            "prohibited_keys": sorted(blend.PROHIBITED_PRESCRIPTION_KEYS),
            "disclosures": {"classification": "advisory", "subaudible_below_db": driver.DRIVER_MIN_CUT_DB},
        },
        "blend": {
            "filters_are_a_total": blend_format["filters_are_a_total"],
            **blend_status,
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
                "boost_route": unavailable(blend.BOOST_ROUTE_UNAVAILABLE, "The route refuses every boost today."),
            },
            "refusal_codes": sorted(blend.BLEND_PRESCRIPTION_REFUSAL_REASONS),
            "prohibited_keys": sorted(blend.PROHIBITED_PRESCRIPTION_KEYS),
        },
        "alignment": {
            **(unavailable(alignment.ALIGNMENT_NO_CROSSOVER_REGION) if one_way else {"status": "available"}
               if corner else unavailable(alignment.PRESCRIPTION_FC_UNKNOWN)),
            "schema": _request_schema(alignment_format, alignment.ALIGNMENT_PRESCRIPTION_KIND,
                                      alignment.ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
                                      {"delay_us": _number()},
                                      {**shared, "basis_delay_us": _number(), "basis_note": {"type": "string"},
                                       "polarity": {"enum": sorted(alignment._PINNABLE_POLARITIES)}}),
            "bounds": {"declared_delay_magnitude_us": list(delay) if delay else None,
                       "fc_hz": corner, "lobe_us": alignment.half_period_us(corner) if corner else None,
                       "lobe_applies_to": "abs(delay_us - basis_delay_us), disclosure only"},
            "refusal_codes": sorted(alignment.ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
        },
        "topology": {
            **(unavailable(topology.TOPOLOGY_NO_CROSSOVER_REGION) if one_way else {"status": "available"}
               if topology_bounds["fc_hz"] else unavailable(topology.TOPOLOGY_MALFORMED)),
            "schema": _request_schema(topology_format, topology.TOPOLOGY_PRESCRIPTION_KIND,
                                      topology.TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
                                      {"fc_hz": _number(),
                                       "order": {"type": "integer", "enum": sorted(topology.SUPPORTED_LR_ORDERS)}},
                                      {**shared, "basis_note": {"type": "string"}}),
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
    }
    result["bounds"].update(band_hz=None, ceiling_hz=None, ceiling_source=None,
                            freqs_hz=None, cut_floor_db=None, boost_cap_db=None,
                            taper_knee_hz=None, spatial_support=None, sides=sides,
                            admit_boost=None)
    if raw.get("code") == SET_REQUIRED:
        return {**result, **unavailable(SET_REQUIRED, {"sets": raw["sets"]})}
    try:
        median = room.read_room_median(raw)
    except room.RoomPrescriptionRefused as exc:
        return {**result, **unavailable(exc.reason)}
    result["status"] = "available"
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
        "detector_lowpass_hz": _number(bass.DETECTOR_CORNER_HZ_MIN, bass.DETECTOR_CORNER_HZ_MAX),
        "compressor_threshold_dbfs": _number(bass.COMPRESSOR_THRESHOLD_DBFS_MIN, bass.COMPRESSOR_THRESHOLD_DBFS_MAX),
        "compressor_factor": {"type": "number", "exclusiveMinimum": bass.COMPRESSOR_FACTOR_MIN,
                              "maximum": bass.COMPRESSOR_FACTOR_MAX},
        "compressor_attack_s": _number(bass.COMPRESSOR_ATTACK_S_MIN, bass.COMPRESSOR_ATTACK_S_MAX),
        "compressor_release_s": _number(bass.COMPRESSOR_RELEASE_S_MIN, bass.COMPRESSOR_RELEASE_S_MAX),
        "delta_highpass_hz": _number(bass.DELTA_HIGHPASS_HZ_MIN),
        "linkwitz_transform": _object({
            "source_hz": _number(bass.LINKWITZ_SOURCE_HZ_MIN, bass.LINKWITZ_SOURCE_HZ_MAX),
            "source_q": _number(bass.LINKWITZ_Q_MIN, bass.LINKWITZ_Q_MAX),
            "target_hz": _number(bass.LINKWITZ_TARGET_HZ_MIN),
            "target_q": _number(bass.LINKWITZ_Q_MIN, bass.LINKWITZ_Q_MAX),
        }, sorted(field.name for field in fields(bass.LinkwitzTransform))),
    }
    for field in fields(bass.DynamicBassDescriptor):
        if field.name in bass.OPTIONAL_FIELDS:
            properties[field.name]["default"] = field.default
    properties.update({name: {"type": "string", "minLength": 1, "description": description}
                       for name, description in format_["optional_top_level"].items()})
    return {
        "schema": _object(properties, sorted(bass.REQUIRED_FIELDS)),
        "bounds": {"delta_highpass_hz_exclusive_upper_field": "detector_lowpass_hz",
                   "linkwitz_transform": {"adr": "ADR-0359", "target_hz_exclusive_upper_field": "source_hz"}},
        "refusal_codes": format_["refusal_reasons"],
        **bass_prescription.bass_evidence_summary(evidence),
        "shared_headroom": {
            "adrs": ["ADR-0385", "ADR-0359", "ADR-0121"],
            "charge_function": "jasper.active_speaker.program_headroom.charge_db",
            "charged_layers": [driver.LINEARIZATION_CANDIDATE_FIELD, blend.BLEND_CANDIDATE_FIELD,
                               room.ROOM_CANDIDATE_FIELD, _REAR_SECTION],
            "uncharged_layers": ["bass_extension", "preference_filters"],
            "margin_db": HEADROOM_MARGIN_DB,
            "max_charge_db": MAX_PROGRAM_HEADROOM_DB,
            "cost": "maximum_output_level_db",
            "bass_reserve_function": "jasper.bass_extension.dynamic.dynamic_bass_gain_reserve_db",
            "detail": ("One program charge covers the charged layers: the emitted graph's program peak, where every "
                       "series stage and mixer sum ahead of an output nets (crossovers, high-passes and trims too), "
                       f"plus one {HEADROOM_MARGIN_DB:g} dB margin when that peak is over {PEAK_EPS_DB:g} dB, plus "
                       f"the output trim. Composition refuses {PROGRAM_HEADROOM_EXHAUSTED} past "
                       f"{MAX_PROGRAM_HEADROOM_DB:g} dB. The charge costs maximum output level. The bass boost and "
                       "the preference filters are not charged: the bass boost plays at every volume, reserves its "
                       "own lift (bass_reserve_function) and costs maximum bass level near the clip point, where "
                       "its compressor gives way."),
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
    """Electrical branches only; see ADR-0318, ADR-0322, ADR-0324 and ADR-0327."""
    return {
        "document_section": _REAR_SECTION,
        "case": "electrical_dsp",
        "mode": "branches",
        "schema": _rear_calibration_schema(),
        # Untuned and muted, at the rate the door binds a rear section to, so it is admitted as written.
        "seed": rear_calibration.diagnostic_seed(DEFAULT_SAMPLE_RATE),
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
                f"Peaking, Lowshelf and Highshelf gain must not exceed +{rear_calibration.MAX_CHAIN_BOOST_DB:g} dB. "
                f"A boost uses shared program headroom (ceiling {MAX_PROGRAM_HEADROOM_DB:g} dB), applied "
                "as broadband attenuation pre-split to every driver, including the tweeter (ADR-0327)."
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
                "change ONE family: rear gain, rear relative delay, or one band edge; "
                "copy all other incumbent fields verbatim, including the front chain and filter structure"
            ),
            "rear_muted_reference": "the same section with rear_muted: true is the rear-muted reference",
            "inheritance_rule": (
                "an absent rear_calibration key inherits the base's section; null clears the stage and "
                "the rear output is then muted"
            ),
        },
    }


def prescription_contracts(*, programs: Collection[str] = SECTIONS, draft: Mapping[str, Any] | None = None,
                           candidate: Mapping[str, Any] | None = None,
                           room_median: Mapping[str, Any] | None = None,
                           room_persistence: Mapping[str, Any] | None = None,
                           room_ceiling: Mapping[str, Any] | None = None,
                           bass_evidence: Mapping[str, Any] | None = None,
                           applied_profile: Mapping[str, Any] | None = None,
                           manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidate = candidate or {}
    preset = _preset(candidate, applied_profile or {})
    return {name: (_speaker(draft or {}, preset, candidate, manifest or {}) if name == "speaker" else
                   _room(room_median or {}, room_persistence or {}, room_ceiling or {}, preset) if name == "room" else
                   _bass(bass_evidence or {}) if name == "bass" else _rear()) for name in SECTIONS if name in programs}


_SNR_NOT_AN_UNCERTAINTY: dict[str, str] = {'<role>_snr_db': "the worst per-band signal-to-noise ratio over the bands that decide this DRIVER role's MAGNITUDE claims — its level and its overlap-band trim. A ratio is not a spread about a reading: it BOUNDS the random error a level measured in that band can carry, and it does not shrink as captures are added, because it is a property of the capture conditions rather than of how many times they were repeated", '<role>_snr_verdict': "the policy's own answer about the figure above, in jasper.audio_measurement.snr_policy's per-band rank — a REFUSAL vocabulary that ships a shortfall in dB, deliberately not the quality_model trust labels it resembles. The words are not spelled here: they have an owner, and a copy that agrees today is still a copy. A verdict, not a quantity: there is nothing here to be uncertain by", '<role>_snr_band': 'which band produced the worst reading above. A label, not a quantity', '<role>_alignment_snr_db': "the same worst-band ratio over the bands that decide this DRIVER role's ALIGNMENT claims — polarity and delay — which need far more SNR because a null of depth D cannot be measured with less than roughly D + 10 dB. Published apart from the magnitude figure rather than pooled with it: the two answer different questions under different floors, and one number would let a capture that is fine for a trim read as fine for a null depth", '<role>_alignment_snr_verdict': "the same policy's answer about the alignment figure, under the alignment floor rather than the magnitude one — which is why one capture can legitimately carry a passing magnitude verdict and a refusing alignment one at the same time. A verdict, not a quantity", '<role>_alignment_snr_band': 'which band produced the worst alignment reading. A label, not a quantity', '<role>_pilot_snr_db': "the quiet-pilot in-band SNR. Null when no usable ambient window was captured. Pilot roles include 'summed'. A ratio, not a spread", 'pilot_ambient': 'whether usable ambient evidence is present or unavailable; unavailable is not low SNR. A label', 'pilot_snr_ok': "whether every pilot cleared its SNR floor; null means no pilots or no usable ambient, never a pass. A verdict, not a spread", 'gain_plan_snr_floor_ok': 'the room-quality gate: whether the ambient report cleared the floor the target capture level needs. False also when that report was missing or unreadable, so it is a gate outcome rather than a measurement, and never a spread'}


_HARMONICS_UNCERTAINTY: dict[str, dict[str, str]] = {'h{order}_repeat_spread_db': {'kind': UNCERTAINTY_RANDOM, 'of': "how far this order's reading moved across the sweep repeats of one role INSIDE ONE CAPTURE — the sample standard deviation over those repeats. It is random, and unusually cleanly so: a MEASURE capture is one pose, so its repeats share the microphone position, the session volume, the graph and the drive, and what is left to differ is capture noise. It is the same statistic linearization_envelope.compute_sigma_curve owns and the runbook's sigma table calls repeatability sigma(f), read here at a distortion ratio instead of a magnitude. Like every sample spread it CONVERGES as repeats are added rather than shrinking; what falls with more repeats is the standard error of the pooled median beneath it. Absent (null) below two real repeats, where it is undefined — never 0.0, which would say the repeats agreed. It is NOT a cross-pose spread: pooling two captures would mix this with whatever differs between takes, which is why a round with two MEASURE captures publishes two role blocks rather than one merged one"}}


_HARMONICS_NOT_AN_UNCERTAINTY: dict[str, str] = {'h{order}_below_fundamental_db': "this order's level MINUS the fundamental's at the same EXCITATION frequency — the conventional 'HD2 sits 46 dB down' reading, negative for a well-behaved driver, pooled as the median over the capture's repeats. A reading, not a spread about one. It carries a SYSTEMATIC error that this block does not publish as a field and will not pretend it has bounded: the microphone calibration enters each curve at its own acoustic frequency, so the ratio inherits C(N*f) - C(f), the calibration curve's own slope across an octave. That error is zero only where the calibration is flat across an octave, it does not shrink with repeats, and quantifying it needs the calibration file's slope at each published frequency — which is the field this block would add if the figure were ever read to a tighter tolerance than the roughly 1 dB the rows are rounded to", 'h{order}_floor_below_fundamental_db': "the measured noise floor in the SAME units as the reading above — what a phantom window between the harmonic images reads, so a reading that approaches it is describing the instrument rather than the driver. It BOUNDS an error without being one, exactly as the capture_snr block's figures do, and reading it as a spread that more captures would shrink is the mistake the two lists exist to prevent. It is an ESTIMATE and not a bound in one further respect the reader is owed: the phantom window is narrower than the image window it describes, so the level is scaled by the window-length ratio, and that scaling assumes the floor is spectrally flat across the window — true for capture noise, approximate for the deconvolution's regularization residue", 'h{order}_floor_limited': "true where the reading sits within 6 dB of the floor above, by majority vote across the capture's repeats. A VERDICT about whether a point describes the driver at all, not a quantity: where it is true the reading is real only as an upper bound. Points past the order's own band edge are null here rather than false, because a comparison against a null reading is not a clean point", 'hz': 'the excitation frequency the row was sampled at — one of a fixed ladder, not a per-round choice. A coordinate, not a measurement', 'fundamental_re_band_median_db': 'the pooled fundamental minus its own band median. Published because every ratio in the row divides by the fundamental: a notch at this excitation frequency inflates the ratios on this row with no change in harmonic energy at all, and a reader without this column would read that as distortion. A reading about the response, not a spread', 'thd_percent': "the root-sum-square of the published orders over the fundamental, in percent, computed on the band where EVERY order is real so a total cannot quietly lose a term above one order's edge. A reading. Null where the all-orders band does not reach"}


_HARMONICS_ROLE_NOT_AN_UNCERTAINTY: dict[str, str] = {'role': "which driver's own sweep this block reads. A label", 'wav_sha256_12': "the first 12 hex of the capture's digest — the same identity the lateral_poses takes and the capture_snr block name a capture by, so a reader can join them. An identity, not a measurement", 'n_sweeps': "how many of this role's sweep repeats INSIDE this capture the rows below were pooled over. A count, and the n that h{order}_repeat_spread_db must be judged against — a spread over two repeats is a very different statement from one over six", 'sweep': "the excitation this reading was taken from, and the provenance that says what the numbers could and could not cover. f1_hz/f2_hz are the sweep's own bounds and L_s its Novak time constant — the ONE parameter every harmonic offset derives from, since order N's image sits exactly L*ln(N) ahead of the linear impulse response and is windowed to a fraction of the distance to order N+1's centre. read_band_hz is where the rows are reported: its bottom is f1 plus a 0.25-octave trim for the sweep's fade-in, which is an artefact and not distortion, and its top is f2 divided by the LOWEST published order. Each higher order stops earlier still, at f2/order, because an order survives the deconvolution only while N*f stays inside the sweep's own passband — that bound is the passband, NOT Nyquist, and past it the columns are null. Provenance, not a measurement", 'drive': 'the level this reading was taken at, in every reference it has: stimulus and effective peak dBFS describe what was PLAYED, capture peak and RMS dBFS what was RECORDED, each re its own full scale. NOT SPL — no acoustic reference exists anywhere in this corpus. Load-bearing rather than housekeeping: distortion is a function of drive, so a ratio quoted without this names nothing and two blocks at different drives are not comparable. Readings, not spreads', 'images_clean': "true when every harmonic window for this role sat in program silence rather than reaching back into the previous segment's audio. A verdict about the reading's conditions", 'worst_clearance_s': "the smallest margin, over this capture's sweeps, between the program silence in front of a sweep and what its harmonic windows need. NEGATIVE means a window reached into prior audio: the read is still returned, because the window's taper is near zero at that edge, but it is no longer clean and the reader is told rather than left to assume. A duration, not a spread", 'worst': "the highest (dirtiest) point of each order that CLEARS the floor, with the frequency it sits at — pooled exactly as the rows are, so the headline cannot contradict its own table. Null for an order where nothing clears its floor, which is the honest reading of 'nothing measurable here' and the ordinary answer for a tweeter at a low drive. A reduction of the readings, not an uncertainty", 'floor_limited_fraction': "the share of the reported grid where this order is floor-limited. Near 1.0 the order was buried and the block is describing the instrument; near 0.0 the reading is the driver's. A coverage fraction — it says how much of the curve is trustworthy, never how uncertain a value is", 'rows': 'the per-frequency readings themselves; their columns are declared above'}


_NOT_EVALUATED = {'vertical_plane_response': 'no claim in this packet reads an elevation — every aggregate pools seats without regard to height, and nothing analyses a raised pose on its own — so no banked verdict sees a floor or ceiling bounce, and what a filter of either sign does off the horizontal plane is unmeasured rather than shown to be safe. lateral_poses.takes[].vertical_deg says which poses, if any, were raised; a round whose poses are all 0 sampled the horizontal plane alone', 'lateral_poses[].position_deg': 'this round banked no lateral walk poses, so no pose in it carries a numeric bearing', 'candidates': 'no take this round banked names a candidate, so nothing here says which configurations were played against each other; a round that cycled no candidates measured one graph', 'drivers.passbands_hz': "no driver limits were supplied, so this packet cannot say where each driver's own band starts and ends; a per-driver boost has no bound to be checked against and is refused, while a cut is admitted and counted onto prescription.cuts_outside_passband (ADR-0367)"}


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
