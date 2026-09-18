# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver research and the computed view of declared driver limits."""

from __future__ import annotations

import json
import math
from functools import partial
from typing import Any, Mapping, Sequence

from jasper.json_fields import CodedFieldError
from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

from ._common import (
    DriverFields,
    MANUAL_CANDIDATE_FIELDS,
    MANUAL_DRIVER_FIELDS,
    blocker_issue,
    issue,
)
from .driver_protection import (
    DRIVER_PROTECTION_POLICY_VERSION,
    LOW_LIMIT_DECLARED,
    LOW_LIMIT_PLAUSIBILITY_FACTOR,
    apply_driver_low_limit,
    driver_excitation_floor_hz,
    driver_low_limit_plausibility_band_hz,
    driver_low_limit_plausible,
    driver_protection_profile,
    format_low_limit,
    resolve_driver_low_limit,
)
from .linearization_budget import normalise_fit_budget
from .measurement import active_driver_targets, measured_speaker_groups, physical_driver_target

DRIVER_RESEARCH_KIND = "jts_active_crossover_driver_research"
DRIVER_RESEARCH_RESULT_SCHEMA_VERSION = 2

DRIVER_SAFETY_PROFILE_KIND = "jts_active_speaker_driver_safety_profile"
DRIVER_SAFETY_PROFILE_SCHEMA_VERSION = 1

SUPPORTED_ENCLOSURE_KINDS = {
    "sealed",
    "vented",
    "passive_radiator",
    "open_baffle",
    "transmission_line",
    "unknown",
}
SUPPORTED_PROTECTION_KINDS = {"highpass", "lowpass"}
SUPPORTED_FIELD_CONFIDENCE = {"low", "medium", "high", "unknown"}
MAX_UNKNOWNS = 32
MAX_PROVENANCE_FIELDS = 32
MAX_PROVENANCE_SOURCES = 8
#: Cap for a provenance entry's single free-text ``source`` citation. The same
#: budget as a ``sources[]`` URL, because the citation slot legitimately holds a
#: datasheet URL and any URL the list accepts must be promotable here verbatim.
MAX_PROVENANCE_SOURCE_CHARS = 320


class DriverSafetyProfileError(CodedFieldError):
    """Raised when research or safety-profile input is malformed."""


_fields = DriverFields(DriverSafetyProfileError, length_limit_separator=" ")
_text = partial(_fields._text, max_chars=320)
_finite_float = _fields._finite_float
_positive_float = _fields._positive_float
_sequence = _fields._sequence
_reject_unknown_keys = _fields._reject_unknown_keys


DRIVER_SAFETY_FIELDS = (
    "hard_excitation_band_hz", "required_protection_filters",
    "measurement_band_hz", "level_duration_limits", "cabinet",
)
assert set(DRIVER_SAFETY_FIELDS) <= set(MANUAL_DRIVER_FIELDS)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _driver_research_channels(
    topology: OutputTopology,
) -> list[tuple[SpeakerGroup, SpeakerChannel]]:
    groups = measured_speaker_groups(topology)
    if groups:
        return [(group, channel) for group in groups for channel in group.channels]
    return [
        (group, channel)
        for group in topology.speaker_groups
        if group.mode == "full_range_passive"
        for channel in group.channels
        if channel.role == "full_range"
    ]


def driver_research_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Describe researchable drivers using the measurement target contract."""

    return [
        physical_driver_target(topology, group, channel)
        for group, channel in _driver_research_channels(topology)
    ]


def driver_protection_policy_view(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the code-owned protection bounds /sound/ needs to *explain* itself.

    Display-only, derived, never persisted-authoritative: every design-draft
    load that knows the topology re-stamps it, so a saved copy can never be read
    back as current policy. It exists because the browser must answer *has this
    target delegated its level?* before anything is saved, and must not own a
    second copy of that policy: an absent peak says delegated, and a profile
    saved under the retired contract carries the class default and means the
    same, so the page needs ``max_auto_level_dbfs`` to recognise it.

    The class low limit travels only as ``low_limit_hz`` +
    ``low_limit_provenance`` + a rendered ``low_limit_summary``, never as a bare
    ``min_highpass_hz`` beside a declared figure — two unlabelled floats one key
    apart is the ambiguity this replaced. Resolving that needs the operator's
    visible values, hence ``manual_settings``; without them every target reports
    the class fallback, labelled as such.

    ``policy_version`` has no reader yet and is kept as staleness detection on a
    view whose whole contract is that it gets re-stamped.
    """

    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    targets = driver_research_targets(topology)
    role_counts: dict[str, int] = {}
    for target in targets:
        role = str(target.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    entries: list[dict[str, Any]] = []
    for target in targets:
        target_id = str(target["target_id"])
        role = str(target.get("role") or "")
        style = _topology_driver_style(topology, target_id)
        policy = driver_protection_profile(role, driver_style=style)
        visible, _ = _visible_values_for_target(
            target_id=target_id,
            role=role,
            manual_by_target=manual_by_target,
            manual_by_role=manual_by_role,
            role_counts=role_counts,
        )
        low_limit = resolve_driver_low_limit(visible, role=role, driver_style=style)
        entries.append({
            "target_id": target_id,
            "role_class": policy.role_class,
            "max_auto_level_dbfs": policy.max_auto_level_dbfs,
            "low_limit_hz": low_limit.frequency_hz if low_limit is not None else None,
            "low_limit_provenance": (
                low_limit.provenance if low_limit is not None else None
            ),
            "low_limit_summary": (
                format_low_limit(low_limit) if low_limit is not None else None
            ),
        })
    return {
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        "targets": entries,
    }


def _visible_values_for_target(
    *,
    target_id: str,
    role: str,
    manual_by_target: Mapping[str, Mapping[str, Any]],
    manual_by_role: Mapping[str, Mapping[str, Any]],
    role_counts: Mapping[str, int],
) -> tuple[Mapping[str, Any], bool]:
    """The operator-visible values bound to one physical target, and how.

    One owner for the binding rule — target-specific values first, then the
    legacy per-role entry when that role appears exactly once — so the page
    cannot explain one number while the profile stores another. The second
    element is ``True`` only for the legacy per-role read, which is what
    ``target_values_binding`` records.
    """

    explicit = manual_by_target.get(target_id)
    if explicit is not None:
        return explicit, False
    if role_counts.get(role) == 1:
        legacy = manual_by_role.get(role)
        if legacy is not None:
            return legacy, True
    return {}, False


def _topology_driver_style(topology: OutputTopology, target_id: str) -> str | None:
    """The topology-owned driver style for one physical target, or None."""

    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.target_id(group.id) == target_id:
                return channel.driver_style
    return None


def _bounded_int(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must be an integer")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise DriverSafetyProfileError(f"{field_name} must be an integer") from exc
    if isinstance(value, float) and value != out:
        raise DriverSafetyProfileError(f"{field_name} must be an integer")
    if not minimum <= out <= maximum:
        raise DriverSafetyProfileError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return out


def _reject_bool_tree(value: Any, field_name: str) -> None:
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must not be boolean", code="field_boolean_forbidden")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_bool_tree(item, f"{field_name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_bool_tree(item, f"{field_name}[{index}]")


def _frequency_band(value: Any, field_name: str) -> list[float] | None:
    if value is None:
        return None
    items = _sequence(value, field_name, limit=2)
    if len(items) != 2:
        raise DriverSafetyProfileError(f"{field_name} must contain two values")
    low = _positive_float(items[0], f"{field_name}[0]")
    high = _positive_float(items[1], f"{field_name}[1]")
    if low is None or high is None or low >= high:
        raise DriverSafetyProfileError(f"{field_name} must be an increasing range")
    return [low, high]


def _normalise_protection_filters(value: Any, field_name: str) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_sequence(value, field_name, limit=2)):
        prefix = f"{field_name}[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{prefix} must be an object")
        kind = _text(raw.get("kind"), f"{prefix}.kind", required=True, max_chars=20)
        if kind not in SUPPORTED_PROTECTION_KINDS:
            raise DriverSafetyProfileError(f"{prefix}.kind must be highpass or lowpass")
        if kind in seen:
            raise DriverSafetyProfileError(
                f"{field_name} may contain only one {kind} requirement"
            )
        seen.add(kind)
        cutoff = _positive_float(raw.get("cutoff_hz"), f"{prefix}.cutoff_hz")
        slope = _positive_float(
            raw.get("minimum_slope_db_per_octave"),
            f"{prefix}.minimum_slope_db_per_octave",
        )
        if cutoff is None or slope is None:
            # "Required but unpublished" has no encoding here and deliberately
            # gets none: under the best-estimate contract the honest answer is a
            # declared engineering estimate an operator can see and correct, not
            # a marker that leaves the driver unprotected-but-declared.
            raise DriverSafetyProfileError(
                f"{prefix} requires cutoff_hz and minimum_slope_db_per_octave; "
                "a required filter whose numbers are unpublished takes a "
                "best engineering estimate, not null",
                code="protection_filter_numbers_missing",
            )
        if slope > 96:
            raise DriverSafetyProfileError(
                f"{prefix}.minimum_slope_db_per_octave must be <= 96"
            )
        family = _text(
            raw.get("family_or_equivalent") or "equivalent_or_steeper",
            f"{prefix}.family_or_equivalent",
            max_chars=80,
        )
        if family != "equivalent_or_steeper":
            raise DriverSafetyProfileError(
                f"{prefix}.family_or_equivalent must be equivalent_or_steeper"
            )
        filters.append(
            {
                "kind": kind,
                "cutoff_hz": cutoff,
                "minimum_slope_db_per_octave": slope,
                "family_or_equivalent": family,
            }
        )
    return sorted(filters, key=lambda item: str(item["kind"]))


def _normalise_cabinet(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    enclosure = (
        _text(
            value.get("enclosure_kind") or "unknown",
            f"{field_name}.enclosure_kind",
            max_chars=40,
        )
        or "unknown"
    )
    if enclosure not in SUPPORTED_ENCLOSURE_KINDS:
        raise DriverSafetyProfileError(
            f"{field_name}.enclosure_kind is unsupported: {enclosure}"
        )
    radiator_count = _bounded_int(
        value.get("radiator_count"),
        f"{field_name}.radiator_count",
        minimum=1,
        maximum=16,
    )
    diameter = _positive_float(
        value.get("effective_radiating_diameter_mm"),
        f"{field_name}.effective_radiating_diameter_mm",
    )
    baffle_width = _positive_float(
        value.get("baffle_width_mm"),
        f"{field_name}.baffle_width_mm",
    )
    if (
        enclosure == "sealed"
        and radiator_count == 1
        and diameter is not None
        and baffle_width is not None
    ):
        reconstruction = "sealed_single_radiator_supported"
    elif enclosure == "unknown":
        reconstruction = "refused_unknown_enclosure"
    elif enclosure in {"vented", "passive_radiator"}:
        reconstruction = "refused_multi_radiator_contract_missing"
    elif radiator_count != 1:
        reconstruction = "refused_single_radiator_contract_not_proven"
    else:
        reconstruction = "refused_geometry_incomplete"
    out: dict[str, Any] = {
        "enclosure_kind": enclosure,
        "lf_reconstruction_capability": reconstruction,
    }
    if radiator_count is not None:
        out["radiator_count"] = radiator_count
    if diameter is not None:
        out["effective_radiating_diameter_mm"] = diameter
    if baffle_width is not None:
        out["baffle_width_mm"] = baffle_width
    return out


def _normalise_level_duration_limits(
    value: Any,
    field_name: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    peak = _finite_float(
        value.get("max_effective_peak_dbfs"),
        f"{field_name}.max_effective_peak_dbfs",
    )
    if peak is not None and peak > 0:
        raise DriverSafetyProfileError(
            f"{field_name}.max_effective_peak_dbfs must be <= 0"
        )
    duration = _positive_float(
        value.get("max_sweep_duration_s"),
        f"{field_name}.max_sweep_duration_s",
    )
    repeats = _bounded_int(
        value.get("max_repeat_count"),
        f"{field_name}.max_repeat_count",
        minimum=1,
        maximum=16,
    )
    cooldown = _finite_float(
        value.get("minimum_cooldown_s"),
        f"{field_name}.minimum_cooldown_s",
    )
    if cooldown is not None and cooldown < 0:
        raise DriverSafetyProfileError(f"{field_name}.minimum_cooldown_s must be >= 0")
    out = {
        "max_effective_peak_dbfs": peak,
        "max_sweep_duration_s": duration,
        "max_repeat_count": repeats,
        "minimum_cooldown_s": cooldown,
    }
    return {key: item for key, item in out.items() if item is not None} or None


def _normalise_unknowns(value: Any, field_name: str) -> list[str]:
    unknowns: list[str] = []
    for index, raw in enumerate(_sequence(value, field_name, limit=MAX_UNKNOWNS)):
        item = _text(raw, f"{field_name}[{index}]", required=True, max_chars=160)
        if item and item not in unknowns:
            unknowns.append(item)
    return unknowns


def _normalise_field_provenance(value: Any, field_name: str) -> dict[str, Any]:
    """Normalize per-field provenance assertions.

    A value carries three facts: the number, whether it is published or the
    researcher's best engineering estimate, and one citation either way.
    ``source`` is that citation as a short free string (often a name, not a
    URL), separate from the ``sources`` URL list, and OPTIONAL — an absent key
    is omitted rather than stored as ``None``, so an entry written before it
    existed normalises byte-identically and stays canonical.

    There is deliberately no ``state`` key: ``confidence`` is the single writer
    of "published or estimated" and display derives the badge from it.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    if len(value) > MAX_PROVENANCE_FIELDS:
        raise DriverSafetyProfileError(
            f"{field_name} must contain <= {MAX_PROVENANCE_FIELDS} fields"
        )
    out: dict[str, Any] = {}
    for raw_key, raw_assertion in value.items():
        key = _text(raw_key, f"{field_name} key", required=True, max_chars=80)
        if not isinstance(raw_assertion, Mapping):
            raise DriverSafetyProfileError(f"{field_name}.{key} must be an object")
        _reject_unknown_keys(
            raw_assertion, f"{field_name}.{key}", {"confidence", "basis", "source", "sources"},
        )
        confidence = (
            _text(
                raw_assertion.get("confidence") or "unknown",
                f"{field_name}.{key}.confidence",
                max_chars=20,
            )
            or "unknown"
        )
        if confidence not in SUPPORTED_FIELD_CONFIDENCE:
            raise DriverSafetyProfileError(
                f"{field_name}.{key}.confidence is unsupported"
            )
        basis = _text(
            raw_assertion.get("basis"),
            f"{field_name}.{key}.basis",
            required=True,
            max_chars=240,
        )
        # `citation`, not `source`: the loop below already binds `source` per
        # URL, and reusing the name silently overwrote this one with the last
        # URL in `sources`.
        citation = _text(
            raw_assertion.get("source"),
            f"{field_name}.{key}.source",
            max_chars=MAX_PROVENANCE_SOURCE_CHARS,
        )
        sources: list[str] = []
        for index, raw_source in enumerate(
            _sequence(
                raw_assertion.get("sources"),
                f"{field_name}.{key}.sources",
                limit=MAX_PROVENANCE_SOURCES,
            )
        ):
            source = _text(
                raw_source,
                f"{field_name}.{key}.sources[{index}]",
                required=True,
                max_chars=320,
            )
            if source and source not in sources:
                sources.append(source)
        assertion: dict[str, Any] = {
            "confidence": confidence,
            "basis": basis,
            "sources": sources,
        }
        if citation is not None:
            assertion["source"] = citation
        out[str(key)] = assertion
    return out


def normalise_driver_safety_fields(
    value: Any,
    field_name: str,
    *,
    include_research_evidence: bool,
) -> dict[str, Any]:
    """Normalize the safety fields shared by research and visible settings."""

    if not isinstance(value, Mapping):
        raise DriverSafetyProfileError(f"{field_name} must be an object")
    out: dict[str, Any] = {}
    # The OWNER of this driver's low limit is parsed here, with the safety
    # fields rather than the advisory display ones: one parse site across the
    # design draft, the research result and the profile's manual settings.
    low_limit_hz = _positive_float(
        value.get("recommended_highpass_hz"),
        f"{field_name}.recommended_highpass_hz",
    )
    low_limit_slope = _positive_float(
        value.get("recommended_highpass_slope_db_per_octave"),
        f"{field_name}.recommended_highpass_slope_db_per_octave",
    )
    if low_limit_slope is not None:
        if low_limit_hz is None:
            raise DriverSafetyProfileError(
                f"{field_name}.recommended_highpass_slope_db_per_octave has no "
                "recommended_highpass_hz to condition"
            )
        if low_limit_slope > 96:
            raise DriverSafetyProfileError(
                f"{field_name}.recommended_highpass_slope_db_per_octave must be <= 96"
            )
    if low_limit_hz is not None:
        out["recommended_highpass_hz"] = low_limit_hz
    if low_limit_slope is not None:
        out["recommended_highpass_slope_db_per_octave"] = low_limit_slope
    for key in (
        "hard_excitation_band_hz",
        "measurement_band_hz",
    ):
        band = _frequency_band(value.get(key), f"{field_name}.{key}")
        if band is not None:
            out[key] = band
    if "required_protection_filters" in value:
        out["required_protection_filters"] = _normalise_protection_filters(
            value.get("required_protection_filters"),
            f"{field_name}.required_protection_filters",
        )
    if "fit_budget" in value:
        try:
            out["fit_budget"] = normalise_fit_budget(value["fit_budget"])
        except ValueError as exc:
            raise DriverSafetyProfileError(f"{field_name}.{exc}") from exc
    cabinet = _normalise_cabinet(value.get("cabinet"), f"{field_name}.cabinet")
    if cabinet is not None:
        out["cabinet"] = cabinet
    limits = _normalise_level_duration_limits(
        value.get("level_duration_limits"),
        f"{field_name}.level_duration_limits",
    )
    if limits is not None:
        out["level_duration_limits"] = limits
    if include_research_evidence:
        out["target_id"] = _text(
            value.get("target_id"),
            f"{field_name}.target_id",
            required=True,
            max_chars=160,
        )
        out["unknowns"] = _normalise_unknowns(
            value.get("unknowns"), f"{field_name}.unknowns"
        )
        out["field_provenance"] = _normalise_field_provenance(
            value.get("field_provenance"), f"{field_name}.field_provenance"
        )
    return out


def build_driver_research_context(
    topology: OutputTopology,
    operator_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe the current drivers and build notes, without declared limits."""

    channels = _driver_research_channels(topology)
    role_counts: dict[str, int] = {}
    for _, channel in channels:
        role_counts[channel.role] = role_counts.get(channel.role, 0) + 1
    target_models = operator_inputs.get("target_models")
    target_models = target_models if isinstance(target_models, Mapping) else {}
    targets = []
    for group, channel in channels:
        target_id = channel.target_id(group.id)
        role = channel.role
        model = target_models.get(target_id)
        if model in (None, "") and role_counts[role] == 1:
            model = operator_inputs.get(role)
        targets.append({
            "target_id": target_id,
            "role": role,
            "driver_style": channel.driver_style or "unspecified",
            "manufacturer_and_model": _text(
                model, f"operator_inputs.target_models.{target_id}", required=True, max_chars=160,
            ),
        })
    return {
        "targets": targets,
        "build_notes": _text(
            operator_inputs.get("notes"), "operator_inputs.notes", max_chars=1000,
        ),
    }


def validate_research_result_binding(
    result: Mapping[str, Any],
    context: Mapping[str, Any],
) -> None:
    """Require every current target with its model, ignoring case and spacing."""

    expected = {
        target["target_id"]: target["manufacturer_and_model"]
        for target in context["targets"]
    }
    seen = set()
    for driver in result.get("drivers", []):
        target_id = driver.get("target_id")
        if target_id not in expected:
            raise DriverSafetyProfileError(
                f"driver_research names unknown target_id {target_id!r}", code="research_target_unknown"
            )
        models = [
            " ".join(model.split()).casefold()
            for model in (driver["model"], expected[target_id])
        ]
        if models[0] != models[1]:
            raise DriverSafetyProfileError(
                f"driver_research target {target_id!r} has model {driver['model']!r}; "
                f"the current model is {expected[target_id]!r}", code="research_model_mismatch"
            )
        seen.add(target_id)
    missing = expected.keys() - seen
    if missing:
        raise DriverSafetyProfileError(
            "driver_research is missing target_ids: " + ", ".join(sorted(missing)), code="research_targets_missing"
        )


# --- One implausible low limit, two authors, two answers ---------------------
#
# Declared values are the only refusing authority; class tables may prefill,
# disclose and serve as fallback, never refuse a declaration (ADR-0227 §1).
# The plausibility band is anchored on the class table, so it is split by
# AUTHOR:
#
#   * a RESEARCH REPLY outside the band is REFUSED at intake, below — an LLM
#     misreading a datasheet is not an operator's choice, and refusing at the
#     paste keeps the bad number from becoming a declaration;
#   * an OPERATOR-TYPED value outside it lands a loud warning that SAVES
#     (``_target_low_limit_warnings``).
#
# What protects the driver at a low declared figure lives elsewhere: the derived
# protective high-pass proved in the emitted graph, the absolute corner floor,
# the ``path_safety`` load gate, and the excitation level ceilings.


#: How much of a ``driver_style`` one diagnosis sentence may quote verbatim. The
#: style is FREE-FORM up to 80 characters while the longest style this build
#: registers is 23 (``horn_compression_driver``), so one more than that
#: ellipsizes only a value no table here describes.
_DIAGNOSIS_STYLE_MAX_CHARS = 24


def _ellipsised(text: str, max_chars: int) -> str:
    """``text`` shortened to ``max_chars``, marked so a reader sees it was cut."""

    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "..."[:max_chars]
    return text[: max_chars - 3] + "..."


def _low_limit_implausibility_diagnosis(
    *,
    role: Any,
    driver_style: Any,
    frequency_hz: float,
) -> str | None:
    """One sentence naming WHY a declared low limit is not believable, or None.

    Shared by both arms of the split above, so they cannot describe the same
    number differently. Diagnosis only — each arm appends its own action. The
    interpolated style is ellipsized (:data:`_DIAGNOSIS_STYLE_MAX_CHARS`)
    because it is operator free text, and the warning arm's message has a schema
    cap an 80-character style would blow.
    """

    band = driver_low_limit_plausibility_band_hz(role, driver_style=driver_style)
    if band is None or driver_low_limit_plausible(
        frequency_hz, role=role, driver_style=driver_style
    ):
        return None
    # The class anchor, recovered from the band rather than re-read from the
    # profile: the band IS that anchor divided and multiplied by the factor, so
    # this cannot quote a number the band edges disagree with.
    anchor_hz = band[0] * LOW_LIMIT_PLAUSIBILITY_FACTOR
    style = _ellipsised(
        str(driver_style or "").strip() or "undeclared",
        _DIAGNOSIS_STYLE_MAX_CHARS,
    )
    direction = "below" if float(frequency_hz) < band[0] else "above"
    # "(class default N Hz)" rather than a clause: the band already IS that
    # default divided and multiplied by the factor, and the characters saved are
    # the headroom the warning arm needs under its message cap.
    return (
        f"declared {float(frequency_hz):g} Hz is more than "
        f"{LOW_LIMIT_PLAUSIBILITY_FACTOR:g}x {direction} the {style} class "
        f"band of {band[0]:g}-{band[1]:g} Hz (class default {anchor_hz:g} Hz)"
    )


def validate_research_low_limit_plausibility(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> None:
    """Refuse a research reply whose declared low limit is not believable.

    The REFUSING arm of the author split above, and the only one: this reads the
    pasted packet, never a saved declaration.
    """

    styles = {
        str(target.get("target_id") or ""): target.get("driver_style")
        for target in expected_request.get("targets", [])
        if isinstance(target, Mapping)
    }
    roles = {
        str(target.get("target_id") or ""): target.get("role")
        for target in expected_request.get("targets", [])
        if isinstance(target, Mapping)
    }
    for driver in result.get("drivers", []):
        if not isinstance(driver, Mapping):
            continue
        target_id = str(driver.get("target_id") or "")
        frequency = _positive_float(
            driver.get("recommended_highpass_hz"),
            f"driver_research.{target_id}.recommended_highpass_hz",
        )
        if frequency is None:
            continue
        diagnosis = _low_limit_implausibility_diagnosis(
            role=roles.get(target_id),
            driver_style=styles.get(target_id),
            frequency_hz=frequency,
        )
        if diagnosis is None:
            continue
        raise DriverSafetyProfileError(
            f"driver_research {target_id} recommended_highpass_hz is not "
            f"believable for its driver type: {diagnosis}. Ask again with the "
            "datasheet page for this driver, or enter the figure by hand under "
            "Advanced if you have read it yourself.",
            code="research_low_limit_implausible",
        )


def finalise_research_result(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the reply against the current drivers."""

    validate_research_result_binding(result, expected_request)
    validate_research_low_limit_plausibility(result, expected_request)
    return dict(result)


def _research_by_target(
    driver_research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(driver_research, Mapping):
        return {}
    if (
        driver_research.get("artifact_schema_version")
        != DRIVER_RESEARCH_RESULT_SCHEMA_VERSION
    ):
        return {}
    return {
        str(driver.get("target_id")): driver
        for driver in driver_research.get("drivers", [])
        if isinstance(driver, Mapping) and driver.get("target_id")
    }


def validate_manual_target_bindings(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
) -> None:
    """Refuse ambiguous or contradictory physical-target driver rows."""

    if not isinstance(manual_settings, Mapping):
        return
    targets = driver_research_targets(topology)
    by_id = {str(target["target_id"]): target for target in targets}
    by_role: dict[str, list[str]] = {}
    for physical_target in targets:
        by_role.setdefault(str(physical_target["role"]), []).append(
            str(physical_target["target_id"])
        )
    resolved_targets: set[str] = set()
    legacy_roles: set[str] = set()
    for index, driver in enumerate(manual_settings.get("drivers", [])):
        if not isinstance(driver, Mapping):
            raise DriverSafetyProfileError(
                f"manual_settings.drivers[{index}] must be an object"
            )
        role = _text(
            driver.get("role"),
            f"manual_settings.drivers[{index}].role",
            required=True,
            max_chars=40,
        )
        target_id = _text(
            driver.get("target_id"),
            f"manual_settings.drivers[{index}].target_id",
            max_chars=160,
        )
        if target_id:
            target = by_id.get(target_id)
            if target is None:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers[{index}].target_id is not a current physical target",
                    code="manual_target_unknown",
                )
            if role != target.get("role"):
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers[{index}] role does not match target_id",
                    code="manual_target_role_mismatch",
                )
            if target_id in resolved_targets:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers resolves target {target_id} more than once",
                    code="manual_target_bound_twice",
                )
            resolved_targets.add(target_id)
            continue
        if role in legacy_roles:
            raise DriverSafetyProfileError(
                f"manual_settings.drivers contains duplicate legacy role {role}",
                code="manual_duplicate_legacy_role",
            )
        legacy_roles.add(str(role))
        matches = by_role.get(str(role), [])
        if not matches:
            raise DriverSafetyProfileError(
                f"manual_settings.drivers[{index}].role is not a current driver role"
            )
        if len(matches) == 1:
            resolved = matches[0]
            if resolved in resolved_targets:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers resolves target {resolved} more than once",
                    code="manual_target_bound_twice",
                )
            resolved_targets.add(resolved)


def _normalise_profile_manual_settings(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Canonicalize direct safety-builder input before deriving authority."""

    if manual_settings is None:
        return None
    if not isinstance(manual_settings, Mapping):
        raise DriverSafetyProfileError("manual_settings must be an object")
    drivers: list[dict[str, Any]] = []
    for index, raw in enumerate(
        _sequence(
            manual_settings.get("drivers"),
            "manual_settings.drivers",
            limit=16,
        )
    ):
        field_name = f"manual_settings.drivers[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        driver: dict[str, Any] = {
            "role": _text(
                raw.get("role"),
                f"{field_name}.role",
                required=True,
                max_chars=40,
            ),
        }
        for key, max_chars in (("target_id", 160), ("model", 120), ("manufacturer", 120)):
            value = _text(raw.get(key), f"{field_name}.{key}", max_chars=max_chars)
            if value:
                driver[key] = value
        driver.update(
            normalise_driver_safety_fields(
                raw,
                field_name,
                include_research_evidence=False,
            )
        )
        drivers.append(driver)
    for index, raw_candidate in enumerate(
        _sequence(
            manual_settings.get("crossover_candidates"),
            "manual_settings.crossover_candidates",
            limit=16,
        )
    ):
        field_name = f"manual_settings.crossover_candidates[{index}]"
        if not isinstance(raw_candidate, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(raw_candidate, field_name, MANUAL_CANDIDATE_FIELDS)
        _reject_bool_tree(raw_candidate, field_name)
    normalised = {"drivers": drivers, "crossover_candidates": []}
    validate_manual_target_bindings(topology, normalised)
    return normalised


def _manual_by_role(
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(manual_settings, Mapping):
        return {}
    return {
        str(driver.get("role")): driver
        for driver in manual_settings.get("drivers", [])
        if (
            isinstance(driver, Mapping)
            and driver.get("role")
            and not driver.get("target_id")
        )
    }


def _manual_by_target(
    manual_settings: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(manual_settings, Mapping):
        return {}
    return {
        str(driver.get("target_id")): driver
        for driver in manual_settings.get("drivers", [])
        if isinstance(driver, Mapping) and driver.get("target_id")
    }


def _band_subset(inner: Sequence[float], outer: Sequence[float]) -> bool:
    return inner[0] >= outer[0] and inner[1] <= outer[1]


def _target_issues(target: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    role = str(target.get("role") or "driver")
    target_id = str(target.get("target_id") or role)
    if target.get("target_values_binding") == "missing":
        reasons.append(f"{target_id}:target_specific_values_missing")
    if not target.get("model"):
        reasons.append(f"{role}:model_missing")
    hard = target.get("hard_excitation_band_hz")
    measurement = target.get("measurement_band_hz")
    if not isinstance(hard, list):
        reasons.append(f"{role}:hard_excitation_band_missing")
    if not isinstance(measurement, list):
        reasons.append(f"{role}:measurement_band_missing")
    limits = target.get("level_duration_limits")
    # ``max_effective_peak_dbfs`` is deliberately NOT required: most makers
    # publish no level limit, and its ABSENCE is how a target says so —
    # ``resolve_driver_excitation_ceilings`` reads that as the delegation the
    # sensitivity derivation answers.
    required_limit_fields = (
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    )
    if not isinstance(limits, Mapping):
        reasons.append(f"{role}:level_duration_limits_missing")
    else:
        for field in required_limit_fields:
            if limits.get(field) is None:
                reasons.append(f"{role}:{field}_missing")
    if isinstance(hard, list) and isinstance(measurement, list):
        if not _band_subset(measurement, hard):
            reasons.append(f"{role}:measurement_band_outside_hard_band")
    filters = target.get("required_protection_filters")
    filters = filters if isinstance(filters, list) else []
    kinds = {str(item.get("kind")) for item in filters if isinstance(item, Mapping)}
    if role == "tweeter" and "highpass" not in kinds:
        reasons.append("tweeter:required_highpass_missing")
    if role == "mid":
        if "highpass" not in kinds:
            reasons.append("mid:required_highpass_missing")
        if "lowpass" not in kinds:
            reasons.append("mid:required_lowpass_missing")
    if isinstance(hard, list):
        for item in filters:
            if not isinstance(item, Mapping):
                continue
            cutoff = float(item["cutoff_hz"])
            if not hard[0] <= cutoff <= hard[1]:
                reasons.append(f"{role}:{item.get('kind')}_cutoff_outside_hard_band")
    # No class-table veto here: a saved declaration is operator-authored, so an
    # implausible low limit is a loud warning (``_target_low_limit_warnings``)
    # and the refusing arm is the research-reply intake,
    # ``validate_research_low_limit_plausibility``.
    return reasons


def _target_low_limit_warnings(target: Mapping[str, Any]) -> list[dict[str, str]]:
    """Disclose an implausible declared low limit."""

    role = str(target.get("role") or "")
    style = target.get("driver_style")
    low_limit = resolve_driver_low_limit(target, role=role, driver_style=style)
    if low_limit is None:
        return []
    if _low_limit_implausibility_diagnosis(
        role=role, driver_style=style, frequency_hz=low_limit.frequency_hz,
    ) is None:
        return []
    return [issue("warning", f"{role}:low_limit_implausible_for_style",
                  f"{target['target_id']}: " + _ISSUE_MESSAGES["low_limit_implausible_for_style"])]


_ISSUE_MESSAGES = {
    "target_specific_values_missing": "Enter the driver values for this output.",
    "model_missing": "Enter the driver's model.",
    "hard_excitation_band_missing": "Set the driver's hard excitation frequency range.",
    "measurement_band_missing": "Set the driver's measurement frequency range.",
    "level_duration_limits_missing": "Set the driver's sweep duration, repeat count and cooldown.",
    "max_sweep_duration_s_missing": "Set the driver's maximum sweep duration.",
    "max_repeat_count_missing": "Set the driver's maximum repeat count.",
    "minimum_cooldown_s_missing": "Set the driver's minimum cooldown time.",
    "measurement_band_outside_hard_band": "Keep the measurement range inside the hard excitation range.",
    "required_highpass_missing": "Declare the {role}'s minimum crossover frequency.",
    "required_lowpass_missing": "Declare the {role}'s protective low-pass frequency.",
    "highpass_cutoff_outside_hard_band": "Keep the high-pass frequency inside the hard excitation range.",
    "lowpass_cutoff_outside_hard_band": "Keep the low-pass frequency inside the hard excitation range.",
    "low_limit_implausible_for_style": "Check the minimum crossover frequency and driver type against the datasheet.",
}


def compute_driver_safety_profile(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compute limits, provenance and issues from the current declaration."""
    manual_settings = _normalise_profile_manual_settings(topology, manual_settings)
    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    research_by_target = _research_by_target(driver_research)
    physical_targets = active_driver_targets(topology)
    role_counts: dict[str, int] = {}
    for physical in physical_targets:
        role = str(physical.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    driver_styles = {
        channel.target_id(group.id): channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for physical in physical_targets:
        target_id = str(physical["target_id"])
        role = str(physical["role"])
        visible, used_legacy_role_value = _visible_values_for_target(
            target_id=target_id,
            role=role,
            manual_by_target=manual_by_target,
            manual_by_role=manual_by_role,
            role_counts=role_counts,
        )
        research = research_by_target.get(target_id, {})

        provenance: dict[str, Any] = {}
        unknowns = list(research.get("unknowns", []))
        research_provenance = research.get("field_provenance", {})
        research_provenance = (
            research_provenance if isinstance(research_provenance, Mapping) else {}
        )
        for field in DRIVER_SAFETY_FIELDS:
            if field not in visible:
                continue
            if (
                field in research
                and _canonical_json(visible.get(field))
                == _canonical_json(research.get(field))
                and field in research_provenance
            ):
                provenance[field] = research_provenance[field]
            else:
                provenance[field] = {
                    "confidence": "unknown",
                    "basis": (
                        "Operator-entered visible value; no matching research "
                        "assertion is authoritative."
                    ),
                    "sources": [],
                }
                unknown = f"{field}: operator override has no matching research source"
                if unknown not in unknowns:
                    unknowns.append(unknown)
        style = driver_styles.get(target_id) or "unspecified"
        low_limit = resolve_driver_low_limit(visible, role=role, driver_style=style)
        derived = apply_driver_low_limit(visible, role=role, driver_style=style)
        for field in ("hard_excitation_band_hz", "measurement_band_hz",
                      "required_protection_filters"):
            if _canonical_json(derived.get(field)) == _canonical_json(visible.get(field)):
                continue
            provenance[field] = {
                "confidence": "unknown",
                "basis": (
                    "Derived from the declared minimum recommended crossover "
                    "frequency; not an independently sourced value."
                ),
                "sources": [],
            }
            derived_note = (
                f"{field}: derived from recommended_highpass_hz "
                f"({low_limit.frequency_hz:g} Hz, {low_limit.provenance})"
                if low_limit is not None
                else f"{field}: derived from the declared driver low limit"
            )
            # "Derived" alone hides the case that costs an operator something: a
            # value they TYPED, replaced. /sound/ renders an editable high-pass
            # cutoff and slope and the derivation overwrites both, so the
            # replacement is named to stay reviewable before the save.
            replaced = _superseded_typed_highpass(visible, derived) if (
                field == "required_protection_filters"
            ) else ()
            for was, now, what in replaced:
                supersede_note = (
                    f"{field}: the typed high-pass {what} {was:g} was replaced "
                    f"by the derived {now:g}"
                )
                if supersede_note not in unknowns:
                    unknowns.append(supersede_note)
            if derived_note not in unknowns:
                unknowns.append(derived_note)
        declared_limit = (
            low_limit
            if low_limit is not None and low_limit.provenance == LOW_LIMIT_DECLARED
            else None
        )
        entry: dict[str, Any] = {
            "target_id": target_id,
            "target_fingerprint": str(physical["target_fingerprint"]),
            "speaker_group_id": str(physical["speaker_group_id"]),
            "speaker_group_mode": str(physical["speaker_group_mode"]),
            "role": role,
            "driver_style": style,
            "target_values_binding": (
                "explicit_target"
                if target_id in manual_by_target
                else "unique_legacy_role"
                if used_legacy_role_value
                else "missing"
            ),
            "physical_output_index": physical.get("output_index"),
            "model": visible.get("model"),
            "manufacturer": visible.get("manufacturer"),
            # The low limit's OWNER travels with its projections, so a reader
            # can tell the manufacturer's declaration from what this build
            # derived FROM it — the derived slope is
            # ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)`` and no
            # reader could unmix the two otherwise.
            #
            # DECLARED provenance only: ``apply_driver_low_limit`` also fills
            # these on an INFERRED limit, and returning that would promote a
            # guess into a field meaning "the manufacturer published this". The
            # pair travels together — a slope needs a frequency to condition,
            # and a target holding one half would disagree with itself.
            "recommended_highpass_hz": (
                declared_limit.frequency_hz if declared_limit is not None else None
            ),
            "recommended_highpass_slope_db_per_octave": (
                declared_limit.slope_db_per_octave
                if declared_limit is not None
                else None
            ),
            "hard_excitation_band_hz": derived.get("hard_excitation_band_hz"),
            "required_protection_filters": derived.get(
                "required_protection_filters", []
            ),
            "measurement_band_hz": derived.get("measurement_band_hz"),
            "level_duration_limits": visible.get("level_duration_limits", {}),
            "fit_budget": visible.get("fit_budget"),
            "cabinet": visible.get(
                "cabinet",
                {
                    "enclosure_kind": "unknown",
                    "lf_reconstruction_capability": "refused_unknown_enclosure",
                },
            ),
            "unknowns": unknowns,
            "field_provenance": provenance,
            "authority": "operator_visible_values",
        }
        policy = driver_protection_profile(
            role,
            driver_style=driver_styles.get(target_id) or "unspecified",
            declared_floor_hz=driver_excitation_floor_hz(entry),
        )
        entry["code_owned_policy"] = {
            "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
            "max_auto_level_dbfs": policy.max_auto_level_dbfs,
            "min_highpass_hz": policy.min_highpass_hz,
            "floor_test_frequency_hz": policy.floor_test_frequency_hz,
            "floor_test_duration_ms": policy.floor_test_duration_ms,
        }
        entry = {
            key: value
            for key, value in entry.items()
            if value not in (None, {}, [])
            or key
            in {
                "required_protection_filters",
                "unknowns",
                "field_provenance",
            }
        }
        target_issues = [blocker_issue(
            code, f"{target_id}: " + _ISSUE_MESSAGES[code.rsplit(":", 1)[-1]].format(role=role),
        ) for code in _target_issues(entry)]
        target_issues.extend(_target_low_limit_warnings(entry))
        issues.extend({"target_id": target_id, **item} for item in target_issues)
        targets.append(entry)
    return {
        "artifact_schema_version": DRIVER_SAFETY_PROFILE_SCHEMA_VERSION,
        "kind": DRIVER_SAFETY_PROFILE_KIND,
        "topology_id": topology.topology_id,
        "targets": targets,
        "issues": issues,
        "authority": "operator_visible_values",
    }


def _superseded_typed_highpass(
    visible: Mapping[str, Any],
    derived: Mapping[str, Any],
) -> tuple[tuple[float, float, str], ...]:
    """``(typed, derived, what)`` for each high-pass value the projection replaced.

    Only reports a value that actually MOVED, so the disclosure stays a signal
    rather than a line on every save.
    """

    def highpass(source: Mapping[str, Any]) -> Mapping[str, Any] | None:
        entries = source.get("required_protection_filters")
        if not isinstance(entries, list):
            return None
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "").strip().lower() == "highpass":
                return item
        return None

    def number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(float(value)) else None

    typed = highpass(visible)
    now = highpass(derived)
    if typed is None or now is None:
        return ()
    out: list[tuple[float, float, str]] = []
    for key, what in (
        ("cutoff_hz", "cutoff"),
        ("minimum_slope_db_per_octave", "slope"),
    ):
        was_value = number(typed.get(key))
        now_value = number(now.get(key))
        if was_value is None or now_value is None or was_value == now_value:
            continue
        out.append((was_value, now_value, what))
    return tuple(out)


def driver_floor_issues(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Apply needs only driver floors; measurement needs every input (ADR-0323 §2)."""
    return [issue for issue in profile.get("issues", []) if issue["code"] in {
        "tweeter:required_highpass_missing", "mid:required_highpass_missing",
        "mid:required_lowpass_missing",
    }]
