# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persisted active-speaker design draft.

The design draft is the durable bridge between the user-facing output map and
future crossover compilation. It records what the operator is trying to build
and any externally researched driver facts, but it does not compile filters,
load CamillaDSP, or authorize playback.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.output_topology import OutputTopology
from ._common import (
    ACTIVE_CROSSOVER_ROLE_PAIRS,
    DRIVER_CLASSES,
    LEGACY_DROPPED_DRIVER_FIELDS,
    issue as _issue,
)
from .driver_pad import DriverPadError, effective_sensitivity_db, normalise_pad
from .driver_safety import (
    DRIVER_RESEARCH_RESULT_SCHEMA_VERSION,
    DriverSafetyProfileError,
    build_driver_safety_profile,
    driver_protection_policy_view,
    driver_research_targets,
    evaluate_driver_safety_profile,
    finalise_research_result,
    normalise_driver_safety_fields,
    validate_manual_target_bindings,
    validate_driver_research_request,
    validate_driver_research_result_shape,
)
from .profile import SUPPORTED_POLARITY

SCHEMA_VERSION = 1
DESIGN_DRAFT_KIND = "jts_active_speaker_design_draft"
DRIVER_RESEARCH_KIND = "jts_active_crossover_driver_research"
DEFAULT_DESIGN_DRAFT_PATH = Path("/var/lib/jasper/active_speaker_design_draft.json")
DESIGN_DRAFT_PATH_ENV = "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE"
_DESIGN_DRAFT_WRITE_LOCK = threading.RLock()
_REVISION_UNSET = object()

_SUPPORTED_RESEARCH_ROLES = {"full_range", "woofer", "mid", "tweeter", "subwoofer"}
_SUPPORTED_CONFIDENCE = {"low", "medium", "high", "unknown"}
_SUPPORTED_GAIN_OFFSET_PROVENANCE = {
    "research_estimate",
    "sensitivity_estimate",
    "operator_pinned",
}
_MAX_DRIVERS = 16
_MAX_CANDIDATES = 16
_MAX_SOURCES = 8
MAX_DRIVER_NOTE_CHARS = 2048

_MANUAL_SETTINGS_FIELDS = {"drivers", "crossover_candidates"}
_MANUAL_DRIVER_FIELDS = {
    "target_id",
    "role",
    "model",
    "manufacturer",
    "nominal_impedance_ohm",
    "sensitivity_db_2v83_1m",
    "usable_frequency_range_hz",
    "recommended_highpass_hz",
    "recommended_highpass_slope_db_per_octave",
    "recommended_lowpass_hz",
    "do_not_test_below_hz",
    "gain_offset_db",
    "gain_offset_db_provenance",
    "notes",
    "sources",
    "hard_excitation_band_hz",
    "required_protection_filters",
    "measurement_band_hz",
    "level_duration_limits",
    "cabinet",
    "source",
    # #1665 component entry. driver_class/radiating_diameter_mm are
    # AI-researchable (see driver_safety's research prompt); pad is
    # deliberately NOT researched -- the operator is the only one who knows
    # what resistors they actually wired in.
    "driver_class",
    "radiating_diameter_mm",
    "pad",
}
_CANDIDATE_FIELDS = {
    "between_roles",
    "frequency_hz",
    "filter_type",
    "slope_db_per_octave",
    "confidence",
    "rationale",
    "warnings",
    "lower_polarity",
    "upper_polarity",
    "delay_ms",
    "delay_target_role",
    "source",
}
_OPERATOR_INPUT_FIELDS = {
    "full_range",
    "woofer",
    "mid",
    "tweeter",
    "subwoofer",
    "notes",
    "target_models",
}


class ActiveSpeakerDesignDraftError(ValueError):
    """Raised when a design draft or research packet has an unsupported shape."""


class ActiveSpeakerDesignDraftRevisionConflict(ActiveSpeakerDesignDraftError):
    """Raised when an optimistic design-draft revision is stale."""

    def __init__(self, message: str, current_draft: Mapping[str, Any]):
        super().__init__(message)
        self.current_draft = dict(current_draft)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _design_draft_path(path: str | Path | None = None) -> Path:
    return Path(
        path or os.environ.get(DESIGN_DRAFT_PATH_ENV) or DEFAULT_DESIGN_DRAFT_PATH
    )


def _mapping(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be an object")
    return raw


def _reject_unknown_keys(
    raw: Mapping[str, Any],
    field_name: str,
    allowed: set[str],
) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ActiveSpeakerDesignDraftError(
            f"{field_name} has unknown fields: {', '.join(unknown)}"
        )


def _sequence(raw: Any, field_name: str, *, limit: int | None = None) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be a list")
    if limit is not None and len(raw) > limit:
        raise ActiveSpeakerDesignDraftError(
            f"{field_name} must contain <= {limit} items"
        )
    return raw


def _text(
    raw: Any, field_name: str, *, required: bool = False, max_chars: int = 240
) -> str | None:
    if raw is None or raw == "":
        if required:
            raise ActiveSpeakerDesignDraftError(f"{field_name} is required")
        return None
    if not isinstance(raw, str):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be a string")
    out = " ".join(raw.split())
    if not out:
        if required:
            raise ActiveSpeakerDesignDraftError(f"{field_name} is required")
        return None
    if len(out) > max_chars:
        raise ActiveSpeakerDesignDraftError(
            f"{field_name} must be <= {max_chars} chars"
        )
    return out


def _finite_float(raw: Any, field_name: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be numeric")
    try:
        out = float(raw)
    except (TypeError, ValueError) as exc:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be numeric") from exc
    if not math.isfinite(out):
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be finite")
    return out


def _gain_offset_provenance(
    raw: Any,
    field_name: str,
    *,
    default: str,
) -> str | None:
    if raw is None or raw == "":
        return default
    value = _text(raw, field_name, max_chars=40)
    if value not in _SUPPORTED_GAIN_OFFSET_PROVENANCE:
        supported = ", ".join(sorted(_SUPPORTED_GAIN_OFFSET_PROVENANCE))
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be one of: {supported}")
    return value


def _positive_float(raw: Any, field_name: str) -> float | None:
    out = _finite_float(raw, field_name)
    if out is not None and out <= 0:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be > 0")
    return out


def _polarity(raw: Any, field_name: str) -> str | None:
    if raw is None or raw == "":
        return None
    value = _text(raw, field_name, max_chars=20)
    if value not in SUPPORTED_POLARITY:
        supported = ", ".join(sorted(SUPPORTED_POLARITY))
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be one of: {supported}")
    return value


def _crossover_filter_type(raw: Any, field_name: str) -> str | None:
    """A declared ``filter_type`` the compiler can build, or a refusal.

    Entry-time half of the crossover vocabulary. Anything the compiler cannot
    build is refused HERE, where the operator (or the research packet) named
    it, rather than reaching ``staging``'s
    ``crossover_preview_filter_unsupported`` blocker several screens later with
    nothing left pointing at the field that caused it.

    ``staging`` is asked rather than answered for: it owns the declared
    spellings in both directions, so accepted-here and compilable-there are one
    answer by construction. Imported inside the call because it is the compiler
    and this module is a persistence layer -- the same reason
    ``crossover_declaration`` reaches for it this way.
    """

    from .declaration_vocabulary import (
        declared_filter_type_compiles,
        supported_declaration_filter_types,
    )

    if raw is None or raw == "":
        return None
    value = _text(raw, field_name, max_chars=80)
    if value is None:
        return None
    if not declared_filter_type_compiles(value):
        supported = ", ".join(supported_declaration_filter_types())
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be one of: {supported}")
    return value


def _crossover_slope_db_per_octave(raw: Any, field_name: str) -> float | None:
    """A declared slope the compiler can build, or a refusal.

    The slope half of :func:`_crossover_filter_type`'s refusal, and the reason
    the pair exists: 18 dB/octave is a perfectly ordinary number that no
    supported filter order compiles to.
    """

    from .declaration_vocabulary import (
        declared_slope_db_per_octave_compiles,
        supported_declaration_slopes_db_per_octave,
    )

    value = _positive_float(raw, field_name)
    if value is None:
        return None
    if not declared_slope_db_per_octave_compiles(value):
        supported = ", ".join(
            f"{slope:g}" for slope in supported_declaration_slopes_db_per_octave()
        )
        raise ActiveSpeakerDesignDraftError(
            f"{field_name} must be one of: {supported} dB/octave"
        )
    return value


def _delay_ms(raw: Any, field_name: str) -> float | None:
    out = _finite_float(raw, field_name)
    if out is not None and not 0.0 <= out <= 20.0:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be between 0 and 20 ms")
    return out


def _role(raw: Any, field_name: str) -> str:
    role = _text(raw, field_name, required=True, max_chars=40)
    if role not in _SUPPORTED_RESEARCH_ROLES:
        raise ActiveSpeakerDesignDraftError(f"{field_name} is unsupported: {role}")
    return role


def _string_list(raw: Any, field_name: str, *, limit: int = _MAX_SOURCES) -> list[str]:
    return [
        value
        for value in (
            _text(item, f"{field_name}[]", max_chars=320)
            for item in _sequence(raw, field_name, limit=limit)
        )
        if value
    ]


def _frequency_range(raw: Any, field_name: str) -> list[float] | None:
    if raw is None:
        return None
    values = _sequence(raw, field_name, limit=2)
    if len(values) != 2:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must contain two values")
    low = _positive_float(values[0], f"{field_name}[0]")
    high = _positive_float(values[1], f"{field_name}[1]")
    if low is None or high is None or low >= high:
        raise ActiveSpeakerDesignDraftError(f"{field_name} must be an increasing range")
    return [low, high]


def _driver_class(raw: Any, field_name: str) -> str | None:
    value = _text(raw, field_name, max_chars=40)
    if value is None:
        return None
    if value not in DRIVER_CLASSES:
        raise ActiveSpeakerDesignDraftError(
            f"{field_name} must be one of: {', '.join(DRIVER_CLASSES)}"
        )
    return value


def _normalise_driver_common(
    raw: Any,
    prefix: str,
    *,
    require_model: bool,
    include_sources: bool,
    include_research_safety_evidence: bool,
    gain_provenance_default: str,
) -> dict[str, Any]:
    raw = _mapping(raw, prefix)
    gain_offset_db = _finite_float(
        raw.get("gain_offset_db"),
        f"{prefix}.gain_offset_db",
    )
    nominal_impedance_ohm = _positive_float(
        raw.get("nominal_impedance_ohm"),
        f"{prefix}.nominal_impedance_ohm",
    )
    driver: dict[str, Any] = {
        "role": _role(raw.get("role"), f"{prefix}.role"),
        "model": _text(
            raw.get("model"),
            f"{prefix}.model",
            required=require_model,
            max_chars=120,
        ),
        "manufacturer": _text(
            raw.get("manufacturer"),
            f"{prefix}.manufacturer",
            max_chars=120,
        ),
        "nominal_impedance_ohm": nominal_impedance_ohm,
        "sensitivity_db_2v83_1m": _finite_float(
            raw.get("sensitivity_db_2v83_1m"),
            f"{prefix}.sensitivity_db_2v83_1m",
        ),
        "usable_frequency_range_hz": _frequency_range(
            raw.get("usable_frequency_range_hz"),
            f"{prefix}.usable_frequency_range_hz",
        ),
        # recommended_highpass_hz -- the OWNER of this driver's low limit
        # (#2603) -- is deliberately NOT parsed here. It is a safety field, and
        # ``normalise_driver_safety_fields`` below owns it along with its slope
        # condition, so the declaration has exactly one parse point.
        "recommended_lowpass_hz": _positive_float(
            raw.get("recommended_lowpass_hz"),
            f"{prefix}.recommended_lowpass_hz",
        ),
        "do_not_test_below_hz": _positive_float(
            raw.get("do_not_test_below_hz"),
            f"{prefix}.do_not_test_below_hz",
        ),
        "gain_offset_db": gain_offset_db,
        "gain_offset_db_provenance": (
            _gain_offset_provenance(
                raw.get("gain_offset_db_provenance"),
                f"{prefix}.gain_offset_db_provenance",
                default=gain_provenance_default,
            )
            if gain_offset_db is not None
            else None
        ),
        "notes": _text(
            raw.get("notes"),
            f"{prefix}.notes",
            max_chars=MAX_DRIVER_NOTE_CHARS,
        ),
        # #1665 component entry: physical facts about the driver, not safety
        # limits. driver_class feeds compose_envelope's class_prior_limit;
        # radiating_diameter_mm is the ka-beaming input (#1675). pad is the
        # third such field and is parsed below, outside the safety normaliser.
        "driver_class": _driver_class(raw.get("driver_class"), f"{prefix}.driver_class"),
        "radiating_diameter_mm": _positive_float(
            raw.get("radiating_diameter_mm"),
            f"{prefix}.radiating_diameter_mm",
        ),
    }
    if include_sources:
        driver["sources"] = _string_list(raw.get("sources"), f"{prefix}.sources")
    try:
        driver.update(
            normalise_driver_safety_fields(
                raw,
                prefix,
                include_research_evidence=include_research_safety_evidence,
            )
        )
        # Pad is deliberately outside normalise_driver_safety_fields (a
        # research/safety-profile surface pad is never part of): it is an
        # operator-only, never-researched fact, folded into effective
        # sensitivity by declared_effective_driver_sensitivities() below and
        # by baseline_profile._derive_corrections, not a safety limit.
        driver["pad"] = normalise_pad(
            raw.get("pad"),
            nominal_impedance_ohm=nominal_impedance_ohm,
            field_name=f"{prefix}.pad",
        )
    except (DriverSafetyProfileError, DriverPadError) as exc:
        raise ActiveSpeakerDesignDraftError(str(exc)) from exc
    return {key: value for key, value in driver.items() if value not in (None, [])}


def _normalise_driver(
    raw: Any,
    *,
    include_research_safety_evidence: bool = False,
) -> dict[str, Any]:
    return _normalise_driver_common(
        raw,
        "driver",
        require_model=True,
        include_sources=True,
        include_research_safety_evidence=include_research_safety_evidence,
        gain_provenance_default="research_estimate",
    )


def _normalise_manual_driver(raw: Any) -> dict[str, Any]:
    # Legacy manual values had no provenance. Preserve them as pinned: an
    # upgrade must never silently replace an attenuation the operator may have
    # chosen for driver safety. New UI-generated sensitivity proposals send
    # ``sensitivity_estimate`` and remain supersedable by acoustic measurement.
    raw = _mapping(raw, "manual_settings.driver")
    # Tolerated, never stored: a retired key still on disk from an older build
    # passes the gate and is dropped by the explicit output dict below, so a
    # draft saved before the deletion stays saveable. See
    # LEGACY_DROPPED_DRIVER_FIELDS.
    _reject_unknown_keys(
        raw,
        "manual_settings.driver",
        _MANUAL_DRIVER_FIELDS | LEGACY_DROPPED_DRIVER_FIELDS,
    )
    driver = _normalise_driver_common(
        raw,
        "manual_settings.driver",
        require_model=False,
        include_sources=False,
        include_research_safety_evidence=False,
        gain_provenance_default="operator_pinned",
    )
    target_id = _text(
        raw.get("target_id") if isinstance(raw, Mapping) else None,
        "manual_settings.driver.target_id",
        max_chars=160,
    )
    if target_id:
        driver["target_id"] = target_id
    return driver


def _normalise_candidate(raw: Any) -> dict[str, Any]:
    raw = _mapping(raw, "crossover_candidate")
    _reject_unknown_keys(raw, "crossover_candidate", _CANDIDATE_FIELDS)
    roles = [
        _role(item, "crossover_candidate.between_roles[]")
        for item in _sequence(
            raw.get("between_roles"),
            "crossover_candidate.between_roles",
            limit=2,
        )
    ]
    if len(roles) != 2:
        raise ActiveSpeakerDesignDraftError(
            "crossover_candidate.between_roles must contain two roles"
        )
    confidence = (
        _text(
            raw.get("confidence", "unknown"),
            "crossover_candidate.confidence",
            max_chars=20,
        )
        or "unknown"
    )
    if confidence not in _SUPPORTED_CONFIDENCE:
        raise ActiveSpeakerDesignDraftError(
            "crossover_candidate.confidence must be low, medium, high, or unknown"
        )
    delay_ms = _delay_ms(raw.get("delay_ms"), "crossover_candidate.delay_ms")
    delay_target_role = None
    if raw.get("delay_target_role") not in (None, ""):
        delay_target_role = _role(
            raw.get("delay_target_role"), "crossover_candidate.delay_target_role"
        )
        if delay_target_role not in roles:
            raise ActiveSpeakerDesignDraftError(
                "crossover_candidate.delay_target_role must be one of between_roles"
            )
    if delay_ms is not None and delay_target_role is None:
        raise ActiveSpeakerDesignDraftError(
            "crossover_candidate.delay_target_role is required when delay_ms is set"
        )
    candidate: dict[str, Any] = {
        "between_roles": roles,
        "frequency_hz": _positive_float(
            raw.get("frequency_hz"),
            "crossover_candidate.frequency_hz",
        ),
        "filter_type": _crossover_filter_type(
            raw.get("filter_type"),
            "crossover_candidate.filter_type",
        ),
        "slope_db_per_octave": _crossover_slope_db_per_octave(
            raw.get("slope_db_per_octave"),
            "crossover_candidate.slope_db_per_octave",
        ),
        "confidence": confidence,
        "rationale": _text(
            raw.get("rationale"),
            "crossover_candidate.rationale",
            max_chars=1000,
        ),
        "warnings": _string_list(
            raw.get("warnings"),
            "crossover_candidate.warnings",
            limit=8,
        ),
        # Persisted working-crossover values (Slice 0): the operator's/preview's
        # own polarity and relative-delay intent for this driver pair. Distinct
        # from any future MEASURED delay-walk verdict — see
        # docs/active-crossover-information-design.md "Slice 0".
        "lower_polarity": _polarity(
            raw.get("lower_polarity"), "crossover_candidate.lower_polarity"
        ),
        "upper_polarity": _polarity(
            raw.get("upper_polarity"), "crossover_candidate.upper_polarity"
        ),
        "delay_ms": delay_ms,
        "delay_target_role": delay_target_role,
    }
    return {key: value for key, value in candidate.items() if value not in (None, [])}


def normalise_driver_research(
    raw: Any,
    *,
    expected_request: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a bounded driver-research packet, or ``None`` when absent."""

    if raw is None or raw == "":
        return None
    raw = _mapping(raw, "driver_research")
    research_schema_version = raw.get("artifact_schema_version")
    if type(research_schema_version) is not int:  # noqa: E721
        raise ActiveSpeakerDesignDraftError(
            "driver_research.artifact_schema_version must be integer 1 or 2"
        )
    if research_schema_version not in {
        SCHEMA_VERSION,
        DRIVER_RESEARCH_RESULT_SCHEMA_VERSION,
    }:
        raise ActiveSpeakerDesignDraftError(
            "driver_research.artifact_schema_version must be 1 or 2"
        )
    if raw.get("kind") != DRIVER_RESEARCH_KIND:
        raise ActiveSpeakerDesignDraftError(
            f"driver_research.kind must be {DRIVER_RESEARCH_KIND}"
        )
    if research_schema_version == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION:
        try:
            validate_driver_research_result_shape(raw)
        except DriverSafetyProfileError as exc:
            raise ActiveSpeakerDesignDraftError(str(exc)) from exc
    drivers = [
        _normalise_driver(
            item,
            include_research_safety_evidence=(
                research_schema_version == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
            ),
        )
        for item in _sequence(
            raw.get("drivers"), "driver_research.drivers", limit=_MAX_DRIVERS
        )
    ]
    target_ids = [
        str(driver["target_id"]) for driver in drivers if driver.get("target_id")
    ]
    if len(target_ids) != len(set(target_ids)):
        raise ActiveSpeakerDesignDraftError(
            "driver_research.drivers contains duplicate target_id"
        )
    if not drivers:
        raise ActiveSpeakerDesignDraftError("driver_research.drivers is required")
    candidates = [
        _normalise_candidate(item)
        for item in _sequence(
            raw.get("crossover_candidates"),
            "driver_research.crossover_candidates",
            limit=_MAX_CANDIDATES,
        )
    ]
    result: dict[str, Any] = {
        "artifact_schema_version": research_schema_version,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": candidates,
        "human_review": {
            "must_verify_wiring": True,
            "must_start_quiet": True,
            "needs_measurement_before_final": True,
        },
    }
    if research_schema_version == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION:
        request_fingerprint = _text(
            raw.get("request_fingerprint"),
            "driver_research.request_fingerprint",
            required=True,
            max_chars=64,
        )
        result["request_fingerprint"] = request_fingerprint
        if expected_request is None:
            raise ActiveSpeakerDesignDraftError(
                "driver_research version 2 requires the current target-bound request"
            )
        try:
            result = finalise_research_result(result, expected_request)
        except DriverSafetyProfileError as exc:
            raise ActiveSpeakerDesignDraftError(str(exc)) from exc
    return result


def normalise_manual_settings(raw: Any) -> dict[str, Any] | None:
    """Return bounded operator-entered crossover settings, or ``None`` when absent."""

    if raw is None or raw == "":
        return None
    raw = _mapping(raw, "manual_settings")
    _reject_unknown_keys(raw, "manual_settings", _MANUAL_SETTINGS_FIELDS)
    drivers = [
        _normalise_manual_driver(item)
        for item in _sequence(
            raw.get("drivers"), "manual_settings.drivers", limit=_MAX_DRIVERS
        )
    ]
    target_ids = [
        str(driver["target_id"]) for driver in drivers if driver.get("target_id")
    ]
    if len(target_ids) != len(set(target_ids)):
        raise ActiveSpeakerDesignDraftError(
            "manual_settings.drivers contains duplicate target_id"
        )
    candidates = [
        _normalise_candidate(item)
        for item in _sequence(
            raw.get("crossover_candidates"),
            "manual_settings.crossover_candidates",
            limit=_MAX_CANDIDATES,
        )
    ]
    drivers = [
        {**driver, "source": "manual_settings"} for driver in drivers if len(driver) > 1
    ]
    candidates = [
        {
            **candidate,
            "source": "manual_settings",
            "confidence": candidate.get("confidence") or "medium",
        }
        for candidate in candidates
        if candidate.get("frequency_hz") is not None
    ]
    if not drivers and not candidates:
        return None
    return {
        "drivers": drivers,
        "crossover_candidates": candidates,
    }


def declared_driver_sensitivities(draft: Mapping[str, Any] | None) -> dict[str, float]:
    """Per-role declared datasheet sensitivities (dB @ 2.83 V/1 m) from the draft.

    The declaration (``manual_settings.drivers``) is the ONE owner of driver
    sensitivity — a declared physical property, not a safety limit — so it is
    never duplicated onto the confirmed safety profile. Consumers wanting the
    ceiling read the pad-folded
    :func:`declared_effective_driver_sensitivities` rather than this naked one.

    A role declared more than once with disagreeing values derives nothing for
    that role (ambiguity fails toward the conservative class-default ceiling).
    Returns ``{}`` when the draft carries no declaration.
    """

    if not isinstance(draft, Mapping):
        return {}
    manual = draft.get("manual_settings")
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, float] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("sensitivity_db_2v83_1m")
        if (
            not role
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            continue
        number = float(value)
        if role in out and out[role] != number:
            conflicted.add(role)
            continue
        out[role] = number
    for role in conflicted:
        out.pop(role, None)
    return out


def declared_effective_driver_sensitivities(
    draft: Mapping[str, Any] | None,
) -> dict[str, float]:
    """Per-role declared sensitivities with any in-line pad folded in.

    Sibling of :func:`declared_driver_sensitivities` with the same shape, except
    each row's naked ``sensitivity_db_2v83_1m`` is folded through
    :func:`jasper.active_speaker.driver_pad.effective_sensitivity_db` using that
    row's own ``pad``. Excitation-ceiling derivation, session-volume planning and
    playback admission read THIS one (#1665): they need the number a microphone
    would measure at the driver terminals, not the naked rating.

    A role is dropped on ANY disagreement between its rows — naked sensitivity,
    pad, or both — since either makes the effective figure ambiguous. Returns
    ``{}`` when the draft carries no declaration.
    """

    if not isinstance(draft, Mapping):
        return {}
    manual = draft.get("manual_settings")
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, float] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("sensitivity_db_2v83_1m")
        if (
            not role
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            continue
        effective = effective_sensitivity_db(float(value), driver.get("pad"))
        if effective is None:
            continue
        if role in out and out[role] != effective:
            conflicted.add(role)
            continue
        out[role] = effective
    for role in conflicted:
        out.pop(role, None)
    return out


def normalise_operator_inputs(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        raw = {}
    else:
        raw = _mapping(raw, "operator_inputs")
    _reject_unknown_keys(raw, "operator_inputs", _OPERATOR_INPUT_FIELDS)
    out: dict[str, Any] = {}
    for key in ("full_range", "woofer", "mid", "tweeter", "subwoofer", "notes"):
        value = _text(
            raw.get(key),
            f"operator_inputs.{key}",
            max_chars=1000 if key == "notes" else 160,
        )
        if value:
            out[key] = value
    target_models = raw.get("target_models")
    if target_models is not None:
        if not isinstance(target_models, Mapping):
            raise ActiveSpeakerDesignDraftError(
                "operator_inputs.target_models must be an object"
            )
        if len(target_models) > _MAX_DRIVERS:
            raise ActiveSpeakerDesignDraftError(
                f"operator_inputs.target_models must contain <= {_MAX_DRIVERS} items"
            )
        normalised_targets: dict[str, str] = {}
        for raw_target_id, raw_model in target_models.items():
            target_id = _text(
                raw_target_id,
                "operator_inputs.target_models key",
                required=True,
                max_chars=160,
            )
            model = _text(
                raw_model,
                f"operator_inputs.target_models.{target_id}",
                required=True,
                max_chars=160,
            )
            if str(target_id) in normalised_targets:
                raise ActiveSpeakerDesignDraftError(
                    f"operator_inputs.target_models contains duplicate target {target_id}"
                )
            normalised_targets[str(target_id)] = str(model)
        if normalised_targets:
            out["target_models"] = normalised_targets
    return out


# The research staleness-comparison field set — one of the four allowlist
# gates for per-driver fields (see tests/test_active_speaker_driver_safety.py
# ::test_component_entry_fields_present_in_all_four_allowlist_gates).
_V2_RESEARCH_COMPARABLE_FIELDS = frozenset({
        "role",
        "model",
        "nominal_impedance_ohm",
        "sensitivity_db_2v83_1m",
        "recommended_highpass_hz",
        "recommended_highpass_slope_db_per_octave",
        "recommended_lowpass_hz",
        "do_not_test_below_hz",
        "gain_offset_db",
        "gain_offset_db_provenance",
        "hard_excitation_band_hz",
        "required_protection_filters",
        "measurement_band_hz",
        "level_duration_limits",
        "cabinet",
        # #1665 component entry: driver_class/radiating_diameter_mm are
        # researchable, so a stale bound packet must be caught the same way as
        # any other editable field. pad is never researched (never present on
        # a research_driver), so this entry is inert for it -- included anyway
        # for consistency with the other allowlists, which carry the same keys.
        "driver_class",
        "radiating_diameter_mm",
        "pad",
    })


def _validate_v2_research_prefill(
    research: Mapping[str, Any],
    manual: Mapping[str, Any] | None,
) -> None:
    """Prove persisted v2 advice still matches the visible imported values.

    A visible edit intentionally invalidates the bound packet in the browser;
    callers then save the manual authority without v2 research.  While the
    packet remains attached, every research-provided editable field must still
    equal its target-specific visible value.
    """

    manual_by_target = {
        str(driver.get("target_id")): driver
        for driver in (manual or {}).get("drivers", [])
        if isinstance(driver, Mapping) and driver.get("target_id")
    }
    comparable = _V2_RESEARCH_COMPARABLE_FIELDS
    for research_driver in research.get("drivers", []):
        target_id = str(research_driver.get("target_id") or "")
        visible = manual_by_target.get(target_id)
        if visible is None:
            raise ActiveSpeakerDesignDraftError(
                f"driver_research target {target_id} has no visible target-specific values"
            )
        for key in comparable:
            if key not in research_driver:
                continue
            if json.dumps(visible.get(key), sort_keys=True) != json.dumps(
                research_driver.get(key),
                sort_keys=True,
            ):
                raise ActiveSpeakerDesignDraftError(
                    f"driver_research visible context is stale for {target_id}.{key}"
                )


def _topology_roles(topology: OutputTopology) -> list[str]:
    roles: list[str] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.role not in roles:
                roles.append(channel.role)
    order = {"full_range": 0, "woofer": 1, "mid": 2, "tweeter": 3, "subwoofer": 4}
    return sorted(roles, key=lambda role: order.get(role, 99))


def _candidate_roles(candidates: list[dict[str, Any]]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for candidate in candidates:
        roles = candidate.get("between_roles")
        if isinstance(roles, list) and len(roles) == 2:
            out.add(frozenset(str(role) for role in roles))
    return out


def _active_crossover_pairs(topology: OutputTopology) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for group in topology.speaker_groups:
        for pair in ACTIVE_CROSSOVER_ROLE_PAIRS.get(group.mode, ()):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _required_driver_info_roles(topology: OutputTopology) -> list[str]:
    pairs = _active_crossover_pairs(topology)
    if not pairs:
        return _topology_roles(topology)
    roles: list[str] = []
    for pair in pairs:
        for role in pair:
            if role not in roles:
                roles.append(role)
    order = {"full_range": 0, "woofer": 1, "mid": 2, "tweeter": 3}
    return sorted(roles, key=lambda role: order.get(role, 99))


def _summary(
    topology: OutputTopology,
    driver_research: dict[str, Any] | None,
    manual_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    topology_roles = _topology_roles(topology)
    required_roles = _required_driver_info_roles(topology)
    required_targets = driver_research_targets(topology)
    required_target_ids = [str(target["target_id"]) for target in required_targets]
    target_role = {
        str(target["target_id"]): str(target["role"]) for target in required_targets
    }
    role_target_ids: dict[str, list[str]] = {}
    for target_id, role in target_role.items():
        role_target_ids.setdefault(role, []).append(target_id)

    def resolved_target_ids(
        drivers: list[dict[str, Any]],
        *,
        allow_legacy_role_fanout: bool = False,
    ) -> set[str]:
        resolved: set[str] = set()
        for driver in drivers:
            explicit = driver.get("target_id")
            if explicit in target_role:
                resolved.add(str(explicit))
                continue
            role = str(driver.get("role") or "")
            matches = role_target_ids.get(role, [])
            if allow_legacy_role_fanout:
                resolved.update(matches)
            elif len(matches) == 1:
                resolved.add(matches[0])
        return resolved

    research_roles = []
    if driver_research:
        for driver in driver_research.get("drivers", []):
            role = driver.get("role")
            if role and role not in research_roles:
                research_roles.append(role)
    candidates = (
        driver_research.get("crossover_candidates", []) if driver_research else []
    )
    manual_drivers = manual_settings.get("drivers", []) if manual_settings else []
    manual_candidates = (
        manual_settings.get("crossover_candidates", []) if manual_settings else []
    )
    research_drivers = driver_research.get("drivers", []) if driver_research else []
    research_target_ids = resolved_target_ids(
        research_drivers,
        allow_legacy_role_fanout=bool(
            driver_research
            and driver_research.get("artifact_schema_version") == SCHEMA_VERSION
        ),
    )
    manual_target_ids = resolved_target_ids(
        manual_drivers,
        allow_legacy_role_fanout=True,
    )
    combined_target_ids = research_target_ids | manual_target_ids
    manual_roles = []
    for driver in manual_drivers:
        role = driver.get("role")
        if role and role not in manual_roles:
            manual_roles.append(role)
    combined_candidate_roles = _candidate_roles(candidates) | _candidate_roles(
        manual_candidates
    )
    missing_candidate_pairs = [
        list(pair)
        for pair in _active_crossover_pairs(topology)
        if frozenset(pair) not in combined_candidate_roles
    ]
    return {
        "speaker_group_count": len(topology.speaker_groups),
        "topology_roles": topology_roles,
        "required_driver_info_roles": required_roles,
        "required_driver_target_ids": required_target_ids,
        "driver_count": len(driver_research.get("drivers", []))
        if driver_research
        else 0,
        "research_roles": research_roles,
        "missing_research_roles": [
            role for role in required_roles if role not in research_roles
        ],
        "missing_research_target_ids": [
            target_id
            for target_id in required_target_ids
            if target_id not in research_target_ids
        ],
        "extra_research_roles": [
            role for role in research_roles if role not in topology_roles
        ],
        "crossover_candidate_count": len(candidates),
        "manual_driver_count": len(manual_drivers),
        "manual_crossover_candidate_count": len(manual_candidates),
        "manual_roles": manual_roles,
        "manual_target_ids": sorted(manual_target_ids),
        "missing_driver_info_target_ids": [
            target_id
            for target_id in required_target_ids
            if target_id not in combined_target_ids
        ],
        "missing_driver_info_roles": [
            role
            for role in required_roles
            if any(
                target_id not in combined_target_ids
                for target_id in role_target_ids.get(role, [])
            )
        ],
        "missing_crossover_candidate_pairs": missing_candidate_pairs,
        "candidate_frequencies_hz": [
            candidate.get("frequency_hz")
            for candidate in [*candidates, *manual_candidates]
            if candidate.get("frequency_hz") is not None
        ],
        "warning_count": sum(
            len(candidate.get("warnings", []))
            for candidate in [*candidates, *manual_candidates]
            if isinstance(candidate, Mapping)
        ),
    }


def _rebound_to_restamped_request(
    driver_research: Any,
    *,
    stored: Any,
    canonical: Mapping[str, Any] | None,
) -> Any:
    """Carry a v2 result across a request RE-STAMP, or leave it exactly alone.

    ``validate_driver_research_request`` re-stamps a request fingerprinted by a
    build that has since had a per-driver field retired
    (``_common.LEGACY_DROPPED_DRIVER_FIELDS``). A v2 result ECHOES that
    fingerprint, so re-stamping the request alone orphans the result stored
    beside it: migrating one of a matched pair is not a migration.

    This module owns the pair — the only place both artifacts are in hand — so
    the re-binding lives here rather than in either validator.

    Deliberately narrow: it moves a result ONLY when the request's digest
    actually changed AND the result echoed the exact pre-stamp digest, so a
    genuinely mismatched result is still refused by the binding.
    """

    if not isinstance(canonical, Mapping) or not isinstance(driver_research, Mapping):
        return driver_research
    if not isinstance(stored, Mapping):
        return driver_research
    was = stored.get("request_fingerprint")
    now = canonical.get("request_fingerprint")
    if not was or not now or was == now:
        return driver_research
    if driver_research.get("request_fingerprint") != was:
        return driver_research
    return {**driver_research, "request_fingerprint": now}


def build_design_draft(
    topology: OutputTopology,
    *,
    driver_research_request: Any = None,
    driver_research: Any = None,
    manual_settings: Any = None,
    operator_inputs: Any = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build a versioned, non-authoritative speaker design draft."""

    inputs = normalise_operator_inputs(operator_inputs)
    target_models = inputs.get("target_models")
    if isinstance(target_models, Mapping):
        current_target_ids = {
            str(target["target_id"]) for target in driver_research_targets(topology)
        }
        unknown_target_ids = sorted(set(target_models) - current_target_ids)
        if unknown_target_ids:
            raise ActiveSpeakerDesignDraftError(
                "operator_inputs.target_models has unknown physical targets: "
                + ", ".join(unknown_target_ids)
            )
    manual = normalise_manual_settings(manual_settings)
    try:
        validate_manual_target_bindings(topology, manual)
    except DriverSafetyProfileError as exc:
        raise ActiveSpeakerDesignDraftError(str(exc)) from exc
    request = None
    if driver_research_request is not None:
        try:
            request = validate_driver_research_request(
                driver_research_request,
                topology,
                inputs,
                manual,
            )
        except DriverSafetyProfileError as exc:
            raise ActiveSpeakerDesignDraftError(str(exc)) from exc
        driver_research = _rebound_to_restamped_request(
            driver_research,
            stored=driver_research_request,
            canonical=request,
        )
    if (
        isinstance(driver_research, Mapping)
        and driver_research.get("artifact_schema_version")
        == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
    ):
        if request is None:
            raise ActiveSpeakerDesignDraftError(
                "driver_research version 2 requires its target-bound request"
            )
    research = normalise_driver_research(
        driver_research,
        expected_request=request,
    )
    if (
        research is not None
        and research.get("artifact_schema_version")
        == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
    ):
        _validate_v2_research_prefill(research, manual)
    evaluation = topology.evaluation()
    summary = _summary(topology, research, manual)
    issues: list[dict[str, str]] = []
    if not topology.speaker_groups:
        issues.append(
            _issue(
                "blocker", "output_topology_empty", "choose and save a speaker layout"
            )
        )
    for blocker in evaluation.get("blockers", []):
        if isinstance(blocker, Mapping):
            issues.append(
                _issue(
                    "blocker",
                    str(blocker.get("code") or "output_topology_blocker"),
                    str(blocker.get("message") or "output topology is blocked"),
                )
            )
    if research is None:
        issues.append(
            _issue(
                "warning",
                "driver_research_missing",
                "AI driver research is not saved; manual crossover settings may still be used",
            )
        )
    for target_id in summary["missing_driver_info_target_ids"]:
        issues.append(
            _issue(
                "warning",
                "driver_target_info_missing",
                f"no target-specific driver info saved for {target_id}",
            )
        )
    for pair in summary["missing_crossover_candidate_pairs"]:
        issues.append(
            _issue(
                "warning",
                "crossover_setting_missing",
                f"no crossover point saved for {'/'.join(pair)}",
            )
        )
    if any(issue["severity"] == "blocker" for issue in issues):
        status = "blocked"
    elif (
        summary["missing_driver_info_target_ids"]
        or summary["missing_crossover_candidate_pairs"]
    ):
        status = "needs_research"
    else:
        status = "ready_for_review"
    now = updated_at or created_at or _utc_now()
    created = created_at or now
    safety_profile = None
    safety_evaluation = evaluate_driver_safety_profile(None, topology).to_dict()
    if _active_crossover_pairs(topology):
        try:
            safety_profile = build_driver_safety_profile(
                topology,
                manual_settings=manual,
                driver_research=research,
                saved_at=now,
            )
        except DriverSafetyProfileError as exc:
            raise ActiveSpeakerDesignDraftError(str(exc)) from exc
        safety_evaluation = evaluate_driver_safety_profile(
            safety_profile,
            topology,
        ).to_dict()
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DESIGN_DRAFT_KIND,
        "status": status,
        "created_at": created,
        "updated_at": now,
        "topology": topology.to_dict(include_evaluation=True),
        "operator_inputs": inputs,
        "driver_research_request": request,
        "driver_research": research,
        "driver_safety_profile": safety_profile,
        "driver_safety_profile_evaluation": safety_evaluation,
        "driver_protection_policy_view": driver_protection_policy_view(
            topology, manual
        ),
        "manual_settings": manual,
        "summary": summary,
        "permissions": {
            "may_explain": True,
            "may_recommend_research_or_measurement": True,
            "may_suggest_bounded_crossover_starting_points": True,
            "may_not_apply_filters": True,
            "may_not_load_camilla": True,
            "may_not_emit_audio": True,
        },
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "authorizes_playback": False,
            "requires_human_review": True,
            "research_is_advisory": True,
            "driver_safety_profile_authorizes_playback": False,
        },
        "issues": issues,
        "next_step": (
            "Resolve output-map blockers before using this draft."
            if status == "blocked"
            else "Add or review crossover settings before compiling a speaker preset."
            if status == "needs_research"
            else "Review the crossover settings before preparing a no-audio preview."
        ),
    }


def _demote_legacy_driver_research_binding(
    draft: dict[str, Any],
) -> dict[str, Any]:
    """Drop obsolete v2 prompt bindings while preserving visible manual values.

    Older builds included the now-hidden per-driver ``operator_notes`` field in
    a request fingerprint. Revalidating that request against the current,
    visible-only prompt contract makes the next save fail as stale. The
    research result is inseparable from its v2 request fingerprint, so demote
    both on load; the normalized manual settings and safety profile remain
    available, and the operator can copy a fresh prompt if more research is
    wanted.
    """

    request = draft.get("driver_research_request")
    targets = request.get("targets") if isinstance(request, Mapping) else None
    has_legacy_notes = isinstance(targets, list) and any(
        isinstance(target, Mapping)
        and isinstance(target.get("operator_declared_context"), Mapping)
        and "operator_notes" in target["operator_declared_context"]
        for target in targets
    )
    if not has_legacy_notes:
        return draft
    out = dict(draft)
    out["driver_research_request"] = None
    research = out.get("driver_research")
    if (
        isinstance(research, Mapping)
        and research.get("artifact_schema_version")
        == DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
    ):
        out["driver_research"] = None
    return out


def load_design_draft(
    path: str | Path | None = None,
    *,
    topology: OutputTopology | None = None,
) -> dict[str, Any]:
    """Return the saved design draft, failing soft when it has not been saved.

    The derived, code-owned fields (``driver_safety_profile_evaluation`` and
    ``driver_protection_policy_view``) are re-stamped here only when a
    ``topology`` is supplied — without one there is nothing to re-derive them
    from, and the disk copy is returned as it was written.  Callers that show
    either field to an operator must pass a topology; the /sound/ design-draft
    endpoint always does.
    """

    target = _design_draft_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "not_saved",
            "revision": 0,
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [],
            "next_step": "Save a speaker design draft from /sound/.",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "revision": 0,
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_unreadable",
                    f"could not read active-speaker design draft: {type(exc).__name__}",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    if not isinstance(raw, dict):
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "revision": 0,
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_not_object",
                    "active-speaker design draft is not a JSON object",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    if (
        type(raw.get("artifact_schema_version")) is not int  # noqa: E721
        or raw.get("artifact_schema_version") != SCHEMA_VERSION
        or raw.get("kind") != DESIGN_DRAFT_KIND
    ):
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": DESIGN_DRAFT_KIND,
            "status": "unreadable",
            "revision": 0,
            "path": str(target),
            "driver_research": None,
            "manual_settings": None,
            "operator_inputs": {},
            "summary": {},
            "issues": [
                _issue(
                    "blocker",
                    "design_draft_unsupported_schema",
                    "active-speaker design draft has an unsupported schema",
                )
            ],
            "next_step": "Save a fresh speaker design draft.",
        }
    revision = raw.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        out = dict(raw)
        out.update(
            {
                "status": "unreadable",
                "revision": 0,
                "issues": [
                    _issue(
                        "blocker",
                        "design_draft_revision_invalid",
                        "active-speaker design draft revision is invalid",
                    )
                ],
                "next_step": "Save a fresh speaker design draft.",
            }
        )
        return out
    raw = _demote_legacy_driver_research_binding({**raw, "revision": revision})
    if topology is None:
        return raw
    out = dict(raw)
    out["driver_safety_profile_evaluation"] = evaluate_driver_safety_profile(
        raw.get("driver_safety_profile"),
        topology,
    ).to_dict()
    # Re-stamped, never trusted from disk: the saved copy was correct for the
    # policy and topology in force when it was written, and both can move
    # underneath it. Same reason the evaluation above is recomputed here.
    # `_view` in the name because `driver_protection_policy` is already taken,
    # by a different shape: excitation_safety_plan hashes one under that key
    # inside the protection-requirement fingerprint.
    # The saved manual settings travel in, because the resolved low limit the
    # view now publishes is a property of the DECLARATION, not of the topology
    # (#2874). Without them every target would report the class fallback on a
    # box that declared its drivers.
    out["driver_protection_policy_view"] = driver_protection_policy_view(
        topology,
        raw.get("manual_settings"),
    )
    return out


def save_design_draft(
    topology: OutputTopology,
    *,
    driver_research_request: Any = None,
    driver_research: Any = None,
    manual_settings: Any = None,
    operator_inputs: Any = None,
    expected_revision: Any = _REVISION_UNSET,
    path: str | Path | None = None,
    created_at: str | None = None,
    durable: bool = False,
) -> dict[str, Any]:
    """Persist a design draft atomically. This does not authorize playback.

    ``durable=True`` fsyncs the write before it is visible (see
    :func:`jasper.atomic_io.atomic_write_text`). The default stays ``False``
    for ordinary editing saves; callers that are accepting a value onto the
    Sound declaration (the crossover-accept seam) opt in explicitly so that
    acceptance survives a power loss, not just a torn write.
    """

    target = _design_draft_path(path)
    with _DESIGN_DRAFT_WRITE_LOCK:
        prior = load_design_draft(target)
        event_at = created_at or _utc_now()
        current_revision = prior.get("revision", 0)
        if expected_revision is not _REVISION_UNSET:
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise ActiveSpeakerDesignDraftError(
                    "expected_revision must be a non-negative integer"
                )
            if expected_revision != current_revision:
                raise ActiveSpeakerDesignDraftRevisionConflict(
                    "speaker design changed in another session; review the fresh values",
                    prior,
                )
        draft = build_design_draft(
            topology,
            driver_research_request=driver_research_request,
            driver_research=driver_research,
            manual_settings=manual_settings,
            operator_inputs=operator_inputs,
            created_at=(
                prior.get("created_at")
                if prior.get("status") != "not_saved"
                else event_at
            ),
            updated_at=event_at,
        )
        draft["path"] = str(target)
        draft["revision"] = current_revision + 1
        # group_from_parent: the crossover-accept seam writes this store from
        # the ROOT jasper-correction-web process, while /sound/ reads it as
        # jasper-web (group jasper). /var/lib/jasper is group jasper but not
        # setgid, so a root write without this publishes root:root 0640 and the
        # design page renders empty against a store it cannot open. Same
        # contract as the sibling Layer-A stores.
        atomic_write_text(
            target,
            # allow_nan=False: fail at the writer that produced the non-finite
            # value, not at the evidence packet hours later (#2839).
            json.dumps(draft, allow_nan=False, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
            durable=durable,
        )
    return draft
