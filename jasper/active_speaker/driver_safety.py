# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-research request and confirmed safety-profile contracts.

Deliberately silent: it turns the current physical active-speaker targets plus
operator-visible limits into immutable JSON contracts, and never generates a
signal, compiles a filter, loads CamillaDSP or grants playback permission.

Research remains advice. A version-2 result must echo the exact server-authored
request and target identities, but only the values visible in
``manual_settings`` enter the confirmed profile; downstream audio code still
runs its own excitation and live-graph admission checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from jasper.output_topology import OutputTopology

from ._common import LEGACY_DROPPED_DRIVER_FIELDS
from .driver_protection import (
    DRIVER_PROTECTION_POLICY_VERSION,
    LOW_LIMIT_DECLARED,
    LOW_LIMIT_PLAUSIBILITY_FACTOR,
    apply_driver_low_limit,
    driver_excitation_floor_hz,
    driver_low_limit_plausibility_band_hz,
    driver_low_limit_plausible,
    driver_protection_profile,
    driver_style_is_registered,
    format_low_limit,
    resolve_driver_low_limit,
)
from .measurement import active_driver_targets, physical_driver_target

DRIVER_RESEARCH_KIND = "jts_active_crossover_driver_research"
DRIVER_RESEARCH_REQUEST_KIND = "jts_active_crossover_driver_research_request"
DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION = 1
DRIVER_RESEARCH_RESULT_SCHEMA_VERSION = 2

DRIVER_SAFETY_PROFILE_KIND = "jts_active_speaker_driver_safety_profile"
DRIVER_SAFETY_PROFILE_SCHEMA_VERSION = 1

#: Field caps for one entry in a profile's ``issues`` list. ONE owner: the shape
#: validator ENFORCES them and ``_target_low_limit_warnings`` FITS its rendered
#: message to them, and a warning over the cap takes the whole save down.
PROFILE_ISSUE_FIELD_MAX_CHARS = {"severity": 20, "code": 160, "message": 320}

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
    "hard_excitation_band_hz",
    "required_protection_filters",
    "measurement_band_hz",
    "level_duration_limits",
    "cabinet",
    "source",
    # design_draft.py's manual-driver allowlist already accepts these, and this
    # allowlist re-validates the SAME normalised record, so it must too.
    "driver_class",
    "radiating_diameter_mm",
    "pad",
}
_MANUAL_CANDIDATE_FIELDS = {
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


class DriverSafetyProfileError(ValueError):
    """Raised when research or safety-profile input is malformed."""


class DriverSafetyProfileStaleLowLimitError(DriverSafetyProfileError):
    """A stored profile predates the one-owner low-limit collapse.

    Its own bands and protective high-pass disagree about where the driver
    stops. Split out from the generic malformed case so the household is told
    the ACTIONABLE thing — save the profile again — rather than "schema
    invalid". Playback is unaffected: the staged graph is a separate artifact.
    """


@dataclass(frozen=True)
class DriverSafetyProfileEvaluation:
    """Fail-closed freshness result for one persisted safety profile.

    ``confirmed_and_current`` means schema-valid, fingerprint intact, bound to
    the CURRENT hardware targets, and no blocking issues. It is NOT permission
    to emit audio — excitation and live protected-graph checks stay separate
    downstream gates — and not a record that a human clicked anything: saving
    the declaration is declaring it. False for ``missing``, ``malformed``,
    ``stale`` (the outputs moved underneath it) and ``incomplete``.
    """

    status: str
    confirmed_and_current: bool
    profile_fingerprint: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confirmed_and_current": self.confirmed_and_current,
            "profile_fingerprint": self.profile_fingerprint,
            "reasons": list(self.reasons),
            "authorizes_playback": False,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def driver_research_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return physical components that the component/research flow describes.

    Active two/three-way targets reuse the measurement contract verbatim. The
    research-only passive full-range case lives here rather than in
    ``measurement.active_driver_targets()``, whose callers require active
    commissioning semantics.
    """

    active_targets = active_driver_targets(topology)
    if active_targets:
        return active_targets

    targets: list[dict[str, Any]] = []
    for group in topology.speaker_groups:
        if group.mode != "full_range_passive":
            continue
        for channel in group.channels:
            if channel.role != "full_range":
                continue
            targets.append(physical_driver_target(topology, group, channel))
    return targets


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
    """The topology-owned driver style for one physical target, or None.

    ``target_id`` is ``f"{group.id}:{channel.role}"`` throughout this module.
    """

    for group in topology.speaker_groups:
        for channel in group.channels:
            if f"{group.id}:{channel.role}" == target_id:
                return channel.driver_style
    return None


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _text(
    value: Any,
    field_name: str,
    *,
    required: bool = False,
    max_chars: int = 320,
) -> str | None:
    if value in (None, ""):
        if required:
            raise DriverSafetyProfileError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise DriverSafetyProfileError(f"{field_name} must be a string")
    out = " ".join(value.split())
    if not out:
        if required:
            raise DriverSafetyProfileError(f"{field_name} is required")
        return None
    if len(out) > max_chars:
        raise DriverSafetyProfileError(f"{field_name} must be <= {max_chars} chars")
    return out


def _finite_float(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must be numeric")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise DriverSafetyProfileError(f"{field_name} must be numeric") from exc
    if not math.isfinite(out):
        raise DriverSafetyProfileError(f"{field_name} must be finite")
    return out


def _positive_float(value: Any, field_name: str) -> float | None:
    out = _finite_float(value, field_name)
    if out is not None and out <= 0:
        raise DriverSafetyProfileError(f"{field_name} must be > 0")
    return out


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


def _sequence(
    value: Any,
    field_name: str,
    *,
    maximum: int,
) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DriverSafetyProfileError(f"{field_name} must be a list")
    if len(value) > maximum:
        raise DriverSafetyProfileError(f"{field_name} must contain <= {maximum} items")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any],
    field_name: str,
    allowed: set[str],
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise DriverSafetyProfileError(
            f"{field_name} has unknown fields: {', '.join(unknown)}"
        )


def _reject_bool_tree(value: Any, field_name: str) -> None:
    if isinstance(value, bool):
        raise DriverSafetyProfileError(f"{field_name} must not be boolean")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_bool_tree(item, f"{field_name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_bool_tree(item, f"{field_name}[{index}]")


def _frequency_band(value: Any, field_name: str) -> list[float] | None:
    if value is None:
        return None
    items = _sequence(value, field_name, maximum=2)
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
    for index, raw in enumerate(_sequence(value, field_name, maximum=2)):
        prefix = f"{field_name}[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{prefix} must be an object")
        _reject_unknown_keys(
            raw,
            prefix,
            {
                "kind",
                "cutoff_hz",
                "minimum_slope_db_per_octave",
                "family_or_equivalent",
            },
        )
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
                "best engineering estimate, not null"
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
    _reject_unknown_keys(
        value,
        field_name,
        {
            "enclosure_kind",
            "radiator_count",
            "effective_radiating_diameter_mm",
            "baffle_width_mm",
            "lf_reconstruction_capability",
        },
    )
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
    _reject_unknown_keys(
        value,
        field_name,
        {
            "max_effective_peak_dbfs",
            "max_sweep_duration_s",
            "max_repeat_count",
            "minimum_cooldown_s",
        },
    )
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
    for index, raw in enumerate(_sequence(value, field_name, maximum=MAX_UNKNOWNS)):
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
            raw_assertion,
            f"{field_name}.{key}",
            {"confidence", "basis", "source", "sources"},
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
                maximum=MAX_PROVENANCE_SOURCES,
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
        # Omitted, never null: see this function's docstring -- a pre-#2195
        # provenance entry has to normalise to the exact bytes it used to, or
        # every stored profile that carries one is refused as noncanonical.
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
        target_fingerprint = _text(
            value.get("target_fingerprint"),
            f"{field_name}.target_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(target_fingerprint):
            raise DriverSafetyProfileError(
                f"{field_name}.target_fingerprint must be a lowercase SHA-256"
            )
        out["target_fingerprint"] = target_fingerprint
        out["unknowns"] = _normalise_unknowns(
            value.get("unknowns"), f"{field_name}.unknowns"
        )
        out["field_provenance"] = _normalise_field_provenance(
            value.get("field_provenance"), f"{field_name}.field_provenance"
        )
    return out


_V2_RESEARCH_TOP_LEVEL_FIELDS = {
    "artifact_schema_version",
    "kind",
    "request_fingerprint",
    "result_fingerprint",
    "drivers",
    "crossover_candidates",
    "human_review",
}
_V2_RESEARCH_DRIVER_FIELDS = {
    "target_id",
    "target_fingerprint",
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
    "hard_excitation_band_hz",
    "required_protection_filters",
    "measurement_band_hz",
    "level_duration_limits",
    "cabinet",
    "unknowns",
    "field_provenance",
    "gain_offset_db",
    "gain_offset_db_provenance",
    "notes",
    "sources",
    # #1665 component entry: build_driver_research_prompt asks for
    # driver_class and radiating_diameter_mm. pad is not prompted
    # (operator-only fact) but is accepted here too for structural parity
    # with the shared _normalise_driver_common schema -- a v2 result never
    # legitimately carries it, but rejecting it here would just be a second,
    # redundant place that gate could drift.
    "driver_class",
    "radiating_diameter_mm",
    "pad",
}
_V2_RESEARCH_CANDIDATE_FIELDS = {
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
}


def validate_driver_research_result_shape(raw: Any) -> None:
    """Reject ambiguous or extension-by-typo fields in the v2 result schema."""

    if not isinstance(raw, Mapping):
        raise DriverSafetyProfileError("driver_research must be an object")
    _reject_unknown_keys(
        raw,
        "driver_research",
        _V2_RESEARCH_TOP_LEVEL_FIELDS,
    )
    if type(raw.get("artifact_schema_version")) is not int:  # noqa: E721
        raise DriverSafetyProfileError(
            "driver_research.artifact_schema_version must be integer 2"
        )
    if raw.get("artifact_schema_version") != DRIVER_RESEARCH_RESULT_SCHEMA_VERSION:
        raise DriverSafetyProfileError(
            "driver_research.artifact_schema_version must be integer 2"
        )
    if raw.get("kind") != DRIVER_RESEARCH_KIND:
        raise DriverSafetyProfileError(
            f"driver_research.kind must be {DRIVER_RESEARCH_KIND}"
        )
    if not _is_sha256(raw.get("request_fingerprint")):
        raise DriverSafetyProfileError(
            "driver_research.request_fingerprint must be a lowercase SHA-256"
        )
    for index, driver in enumerate(
        _sequence(raw.get("drivers"), "driver_research.drivers", maximum=16)
    ):
        if not isinstance(driver, Mapping):
            raise DriverSafetyProfileError(
                f"driver_research.drivers[{index}] must be an object"
            )
        _reject_unknown_keys(
            driver,
            f"driver_research.drivers[{index}]",
            # Tolerated, never stored: a persisted v2 result from an older
            # build (or a chat that still volunteers the key) passes this gate
            # and is dropped by _normalise_driver_common's explicit output.
            _V2_RESEARCH_DRIVER_FIELDS | LEGACY_DROPPED_DRIVER_FIELDS,
        )
        _reject_bool_tree(driver, f"driver_research.drivers[{index}]")
    for index, candidate in enumerate(
        _sequence(
            raw.get("crossover_candidates"),
            "driver_research.crossover_candidates",
            maximum=8,
        )
    ):
        if not isinstance(candidate, Mapping):
            raise DriverSafetyProfileError(
                f"driver_research.crossover_candidates[{index}] must be an object"
            )
        _reject_unknown_keys(
            candidate,
            f"driver_research.crossover_candidates[{index}]",
            _V2_RESEARCH_CANDIDATE_FIELDS,
        )
        _reject_bool_tree(
            candidate,
            f"driver_research.crossover_candidates[{index}]",
        )


def build_driver_research_request(
    topology: OutputTopology,
    operator_inputs: Mapping[str, Any],
    manual_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact physical-target-bound request copied by ``/sound/``."""

    validate_manual_target_bindings(topology, manual_settings)
    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    current_targets = driver_research_targets(topology)
    role_counts: dict[str, int] = {}
    for target in current_targets:
        role = str(target.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    for target in current_targets:
        role = str(target.get("role") or "")
        target_id = str(target["target_id"])
        target_models = operator_inputs.get("target_models")
        target_models = target_models if isinstance(target_models, Mapping) else {}
        model_value = target_models.get(target_id)
        if model_value in (None, "") and role_counts.get(role) == 1:
            model_value = operator_inputs.get(role)
        model = _text(
            model_value,
            f"operator_inputs.target_models.{target_id}",
            required=True,
            max_chars=160,
        )
        visible = manual_by_target.get(target_id)
        if visible is None and role_counts.get(role) == 1:
            visible = manual_by_role.get(role, {})
        visible = visible or {}
        declared_context = (
            normalise_driver_safety_fields(
                visible,
                f"manual_settings.{role}",
                include_research_evidence=False,
            )
            if visible
            else {}
        )
        # ``manual_settings.drivers[].notes`` predates the single visible Build
        # notes field and may contain either operator prose or an imported
        # research summary. Preserve it in the design/safety record, but never
        # send invisible legacy text as authoritative prompt context. Its one
        # reader is ``crossover_v2.operator_notes``, which carries it to the
        # TUNING LLM inside the evidence packet's quarantined block and repeats
        # this ambiguity there as that carrier's ``authored_by`` — the research
        # prompt still never sees it.
        request_target = {
            "target_id": target_id,
            "target_fingerprint": str(target["target_fingerprint"]),
            "speaker_group_id": str(target["speaker_group_id"]),
            "speaker_group_mode": str(target["speaker_group_mode"]),
            "role": role,
            "driver_style": driver_styles.get(str(target["target_id"]))
            or "unspecified",
            "physical_output_index": target.get("output_index"),
            "physical_output_label": target.get("output_label"),
            "manufacturer_and_model": model,
            "operator_declared_context": declared_context or None,
        }
        targets.append(
            {key: value for key, value in request_target.items() if value is not None}
        )
    if not targets:
        raise DriverSafetyProfileError(
            "driver research requires an active two-way or three-way topology"
        )
    core: dict[str, Any] = {
        "artifact_schema_version": DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION,
        "kind": DRIVER_RESEARCH_REQUEST_KIND,
        "topology_id": topology.topology_id,
        "hardware": topology.hardware.to_dict(),
        "targets": targets,
        "build_notes": _text(
            operator_inputs.get("notes"),
            "operator_inputs.notes",
            max_chars=1000,
        ),
    }
    core = {key: value for key, value in core.items() if value is not None}
    return {**core, "request_fingerprint": _fingerprint(core)}


def validate_driver_research_request(
    request: Any,
    topology: OutputTopology,
    operator_inputs: Mapping[str, Any],
    manual_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical current request or refuse stale/self-invalid input."""

    if not isinstance(request, Mapping):
        raise DriverSafetyProfileError("driver_research_request must be an object")
    _reject_unknown_keys(
        request,
        "driver_research_request",
        {
            "artifact_schema_version",
            "kind",
            "topology_id",
            "hardware",
            "targets",
            "build_notes",
            "request_fingerprint",
        },
    )
    if (
        type(request.get("artifact_schema_version")) is not int  # noqa: E721
        or request.get("artifact_schema_version")
        != DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION
        or request.get("kind") != DRIVER_RESEARCH_REQUEST_KIND
    ):
        raise DriverSafetyProfileError(
            "driver_research_request schema or kind is unsupported"
        )
    if request.get("topology_id") != topology.topology_id:
        raise DriverSafetyProfileError(
            "driver_research_request topology does not match the current topology"
        )
    if request.get("hardware") != topology.hardware.to_dict():
        raise DriverSafetyProfileError(
            "driver_research_request hardware does not match the current topology"
        )
    raw_targets = _sequence(
        request.get("targets"),
        "driver_research_request.targets",
        maximum=16,
    )
    current_targets = driver_research_targets(topology)
    role_counts: dict[str, int] = {}
    for target in current_targets:
        role = str(target.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    if len(raw_targets) != len(current_targets):
        raise DriverSafetyProfileError(
            "driver_research_request targets do not exactly match the current physical targets"
        )
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    #: ``targets`` with any retired key the stored context still carries put
    #: BACK verbatim, reconstructing the core the writing build fingerprinted.
    legacy_targets: list[dict[str, Any]] = []
    saw_retired_context_field = False
    for index, (raw_target, current) in enumerate(zip(raw_targets, current_targets)):
        field_name = f"driver_research_request.targets[{index}]"
        if not isinstance(raw_target, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            raw_target,
            field_name,
            {
                "target_id",
                "target_fingerprint",
                "speaker_group_id",
                "speaker_group_mode",
                "role",
                "driver_style",
                "physical_output_index",
                "physical_output_label",
                "manufacturer_and_model",
                "operator_declared_context",
            },
        )
        _reject_bool_tree(raw_target, field_name)
        role = str(current["role"])
        model = _text(
            raw_target.get("manufacturer_and_model"),
            f"{field_name}.manufacturer_and_model",
            required=True,
            max_chars=160,
        )
        target_models = operator_inputs.get("target_models")
        target_models = target_models if isinstance(target_models, Mapping) else {}
        current_model_value = target_models.get(str(current["target_id"]))
        if current_model_value in (None, "") and role_counts.get(role) == 1:
            current_model_value = operator_inputs.get(role)
        current_model = _text(
            current_model_value,
            f"operator_inputs.target_models.{current['target_id']}",
            required=True,
            max_chars=160,
        )
        expected_fields = {
            "target_id": str(current["target_id"]),
            "target_fingerprint": str(current["target_fingerprint"]),
            "speaker_group_id": str(current["speaker_group_id"]),
            "speaker_group_mode": str(current["speaker_group_mode"]),
            "role": role,
            "physical_output_index": current.get("output_index"),
            "physical_output_label": current.get("output_label"),
        }
        for key, expected in expected_fields.items():
            if raw_target.get(key) != expected:
                raise DriverSafetyProfileError(
                    "driver_research_request targets do not exactly match "
                    "the current physical targets"
                )
        if model != current_model:
            raise DriverSafetyProfileError(
                f"driver_research_request model is stale for {role}"
            )
        expected_style = driver_styles.get(str(current["target_id"])) or "unspecified"
        if raw_target.get("driver_style") != expected_style:
            if raw_target.get("driver_style") is not None or expected_style is not None:
                raise DriverSafetyProfileError(
                    "driver_research_request driver style is stale"
                )
        context_raw = raw_target.get("operator_declared_context")
        context: dict[str, Any] = {}
        if context_raw is not None:
            if not isinstance(context_raw, Mapping):
                raise DriverSafetyProfileError(
                    f"{field_name}.operator_declared_context must be an object"
                )
            # Tolerated, never stored: a request persisted by an older build
            # carries that build's normaliser output and is re-validated on
            # EVERY save, so refusing here would make an old draft unsaveable.
            # The normaliser below drops it. See LEGACY_DROPPED_DRIVER_FIELDS.
            _reject_unknown_keys(
                context_raw,
                f"{field_name}.operator_declared_context",
                {
                    "hard_excitation_band_hz",
                    "required_protection_filters",
                    "measurement_band_hz",
                    "cabinet",
                    "level_duration_limits",
                    "operator_notes",
                } | LEGACY_DROPPED_DRIVER_FIELDS,
            )
            context = normalise_driver_safety_fields(
                context_raw,
                f"{field_name}.operator_declared_context",
                include_research_evidence=False,
            )
            notes = _text(
                context_raw.get("operator_notes")
                if isinstance(context_raw, Mapping)
                else None,
                f"{field_name}.operator_declared_context.operator_notes",
                max_chars=2048,
            )
            if notes:
                context["operator_notes"] = notes
        # The retired keys as this request STORED them, copied verbatim rather
        # than re-derived: the stored value is already in the writing build's
        # normalised form, so copying reproduces the fingerprinted bytes.
        legacy_context = dict(context)
        if isinstance(context_raw, Mapping):
            for retired in sorted(LEGACY_DROPPED_DRIVER_FIELDS):
                if retired in context_raw:
                    legacy_context[retired] = context_raw[retired]
                    saw_retired_context_field = True
        target = {
            **expected_fields,
            "driver_style": expected_style,
            "manufacturer_and_model": model,
            "operator_declared_context": context or None,
        }
        targets.append(
            {key: value for key, value in target.items() if value is not None}
        )
        legacy_targets.append(
            {
                key: value
                for key, value in {**target, "operator_declared_context":
                                   legacy_context or None}.items()
                if value is not None
            }
        )
    core: dict[str, Any] = {
        "artifact_schema_version": DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION,
        "kind": DRIVER_RESEARCH_REQUEST_KIND,
        "topology_id": topology.topology_id,
        "hardware": topology.hardware.to_dict(),
        "targets": targets,
        "build_notes": _text(
            request.get("build_notes"),
            "driver_research_request.build_notes",
            max_chars=1000,
        ),
    }
    core = {key: value for key, value in core.items() if value is not None}
    fingerprint = request.get("request_fingerprint")
    current_fingerprint = _fingerprint(core)
    # **Tolerating the retired key in the allowlist is necessary and NOT
    # sufficient**, because the context is FINGERPRINTED: the writing build
    # hashed a core whose context still carried the key, so dropping it here
    # recomputes a different digest and a real box's request would be refused on
    # the very save its remedy copy asks for. A stored digest is therefore also
    # accepted when it matches the core computed WITH the retired field present.
    # Transitional: the record is re-stamped with ``current_fingerprint`` below,
    # which the staleness check immediately after also depends on.
    if not _is_sha256(fingerprint):
        raise DriverSafetyProfileError("driver_research_request fingerprint is invalid")
    if fingerprint != current_fingerprint:
        legacy_core = {
            key: (legacy_targets if key == "targets" else value)
            for key, value in core.items()
        }
        if not saw_retired_context_field or fingerprint != _fingerprint(legacy_core):
            raise DriverSafetyProfileError(
                "driver_research_request fingerprint is invalid"
            )
    canonical = {**core, "request_fingerprint": current_fingerprint}
    expected = build_driver_research_request(
        topology,
        operator_inputs,
        manual_settings,
    )
    if _canonical_json(canonical) != _canonical_json(expected):
        raise DriverSafetyProfileError(
            "driver_research_request is stale for the current visible inputs"
        )
    return canonical


def validate_research_result_binding(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> None:
    """Refuse a v2 result that is stale, incomplete, or target-mismatched."""

    expected_fingerprint = expected_request.get("request_fingerprint")
    if result.get("request_fingerprint") != expected_fingerprint:
        raise DriverSafetyProfileError(
            "driver_research.request_fingerprint does not match the current request"
        )
    expected = {
        str(target.get("target_id")): (
            str(target.get("target_fingerprint")),
            str(target.get("role")),
            str(target.get("manufacturer_and_model")),
        )
        for target in expected_request.get("targets", [])
        if isinstance(target, Mapping)
    }
    observed: dict[str, tuple[str, str, str]] = {}
    for driver in result.get("drivers", []):
        if not isinstance(driver, Mapping):
            continue
        target_id = str(driver.get("target_id") or "")
        target_fingerprint = str(driver.get("target_fingerprint") or "")
        if target_id in observed:
            raise DriverSafetyProfileError(
                f"driver_research has duplicate target_id: {target_id}"
            )
        observed[target_id] = (
            target_fingerprint,
            str(driver.get("role") or ""),
            str(driver.get("model") or ""),
        )
    if observed != expected:
        raise DriverSafetyProfileError(
            "driver_research targets do not exactly match the current physical targets"
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
            "Advanced if you have read it yourself."
        )


def finalise_research_result(
    result: Mapping[str, Any],
    expected_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate binding and add the server-computed immutable result digest."""

    validate_research_result_binding(result, expected_request)
    validate_research_low_limit_plausibility(result, expected_request)
    core = dict(result)
    core.pop("result_fingerprint", None)
    return {**core, "result_fingerprint": _fingerprint(core)}


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
                    f"manual_settings.drivers[{index}].target_id is not a current physical target"
                )
            if role != target.get("role"):
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers[{index}] role does not match target_id"
                )
            if target_id in resolved_targets:
                raise DriverSafetyProfileError(
                    f"manual_settings.drivers resolves target {target_id} more than once"
                )
            resolved_targets.add(target_id)
            continue
        if role in legacy_roles:
            raise DriverSafetyProfileError(
                f"manual_settings.drivers contains duplicate legacy role {role}"
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
                    f"manual_settings.drivers resolves target {resolved} more than once"
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
    _reject_unknown_keys(
        manual_settings,
        "manual_settings",
        _MANUAL_SETTINGS_FIELDS,
    )
    drivers: list[dict[str, Any]] = []
    for index, raw in enumerate(
        _sequence(
            manual_settings.get("drivers"),
            "manual_settings.drivers",
            maximum=16,
        )
    ):
        field_name = f"manual_settings.drivers[{index}]"
        if not isinstance(raw, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        # Tolerated, never stored: same legacy-key contract as design_draft's
        # own manual-driver gate, which re-validates this record.
        _reject_unknown_keys(
            raw,
            field_name,
            _MANUAL_DRIVER_FIELDS | LEGACY_DROPPED_DRIVER_FIELDS,
        )
        _reject_bool_tree(raw, field_name)
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
            maximum=16,
        )
    ):
        field_name = f"manual_settings.crossover_candidates[{index}]"
        if not isinstance(raw_candidate, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            raw_candidate,
            field_name,
            _MANUAL_CANDIDATE_FIELDS,
        )
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
    """Non-blocking disclosures for one stored target's declared low limit.

    Pure, because ``evaluate_driver_safety_profile`` re-derives these from the
    stored targets so a hand-edited artifact cannot drop its own warning; that
    check compares severity and code, never the sentence (see
    :func:`_comparable_issue_payload`).

    **The rendered message is FITTED to the schema cap**, never merely expected
    to fit: a message over ``PROFILE_ISSUE_FIELD_MAX_CHARS["message"]`` fails
    shape validation and refuses the save outright — so an overrun would refuse
    the very out-of-band declaration the disclosure exists to permit.
    """

    role = str(target.get("role") or "")
    style = target.get("driver_style")
    low_limit = resolve_driver_low_limit(target, role=role, driver_style=style)
    if low_limit is None:
        return []
    diagnosis = _low_limit_implausibility_diagnosis(
        role=role,
        driver_style=style,
        frequency_hz=low_limit.frequency_hz,
    )
    if diagnosis is None:
        return []
    # A style the table does not describe is judged against the cautious
    # unknown-tweeter default, so a published 800 Hz on a large-format horn
    # reads as implausible on a box whose type nobody set; naming the picker
    # first keeps that from reading as an accusation about the datasheet.
    #
    # The question is REGISTERED, not empty: ``_profile_core`` stamps
    # ``"unspecified"``, so a test against emptiness would be dead — and asking
    # the table also catches a typo'd or newer-build style that looks declared.
    check_the_type = (
        "and set the driver type above -- unknown types get a cautious default."
        if not driver_style_is_registered(style)
        else "and that the driver type above is right."
    )
    message = (
        f"{role}: {diagnosis}. JTS is using it as declared -- confirm it is "
        f"the datasheet figure and not a transposed digit, {check_the_type}"
    )
    return [
        {
            "severity": "warning",
            "code": f"{role}:low_limit_implausible_for_style",
            "message": _ellipsised(
                message, PROFILE_ISSUE_FIELD_MAX_CHARS["message"]
            ),
        }
    ]


def _profile_core(
    topology: OutputTopology,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    manual_by_role = _manual_by_role(manual_settings)
    manual_by_target = _manual_by_target(manual_settings)
    research_by_target = _research_by_target(driver_research)
    physical_targets = active_driver_targets(topology)
    role_counts: dict[str, int] = {}
    for physical in physical_targets:
        role = str(physical.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    driver_styles = {
        f"{group.id}:{channel.role}": channel.driver_style
        for group in topology.speaker_groups
        for channel in group.channels
        if channel.driver_style
    }
    targets: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[dict[str, str]] = []
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
        safety_field_names = (
            "hard_excitation_band_hz",
            "required_protection_filters",
            "measurement_band_hz",
            "level_duration_limits",
            "cabinet",
        )
        provenance: dict[str, Any] = {}
        unknowns = list(research.get("unknowns", []))
        research_provenance = research.get("field_provenance", {})
        research_provenance = (
            research_provenance if isinstance(research_provenance, Mapping) else {}
        )
        for field in safety_field_names:
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
        # One owner, every consumer derives: the stored target carries the
        # PROJECTION of this driver's declared low limit, not four
        # independently-typed numbers, so nothing downstream can disagree about
        # where this driver stops.
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
            # these on an INFERRED limit, and persisting that would promote a
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
        # Read off ``entry``: the shape validator re-derives this policy from
        # the STORED target and compares it for equality.
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
        issues.extend(_target_issues(entry))
        warnings.extend(_target_low_limit_warnings(entry))
        targets.append(entry)
    if not targets:
        issues.append("active_driver_targets_missing")
    research_digest = None
    request_digest = None
    if isinstance(driver_research, Mapping):
        research_digest = driver_research.get("result_fingerprint")
        request_digest = driver_research.get("request_fingerprint")
    core = {
        "artifact_schema_version": DRIVER_SAFETY_PROFILE_SCHEMA_VERSION,
        "kind": DRIVER_SAFETY_PROFILE_KIND,
        "topology_id": topology.topology_id,
        "targets": targets,
        "research": {
            "request_fingerprint": request_digest,
            "result_fingerprint": research_digest,
            "advisory_only": True,
        },
        "authority": "operator_visible_values",
        "authorizes_playback": False,
    }
    return core, issues, warnings


def _comparable_issue_payload(issues: Any) -> str:
    """The part of an ``issues`` list that re-derivation must reproduce exactly.

    Every entry's ``severity`` and ``code``, plus a BLOCKER's message (which is
    mechanically derived from its own code and so cannot drift on its own).

    A WARNING's message is excluded, and that exclusion is the point: warning
    prose is hand-written and interpolates the household's numbers, so including
    it made editing copy a breaking change — the profile read ``malformed`` and
    ``confirmed_and_current`` flipped false on a box whose declared values had
    not changed. What the check still guarantees is the load-bearing half: a
    warning cannot be dropped, invented, re-coded or downgraded without
    mismatching, and no gate reads the sentence. The fingerprint never covered
    ``issues``, so this loosens nothing the digest was holding.
    """

    return _canonical_json([
        {
            "severity": str(issue.get("severity") or ""),
            "code": str(issue.get("code") or ""),
            **(
                {}
                if str(issue.get("severity") or "") == "warning"
                else {"message": str(issue.get("message") or "")}
            ),
        }
        for issue in (issues if isinstance(issues, list) else [])
        if isinstance(issue, Mapping)
    ])


def _profile_issue_payload(
    issues: Sequence[str],
    warnings: Sequence[Mapping[str, str]] = (),
) -> list[dict[str, str]]:
    """The profile's ``issues`` list: every blocker, then every warning.

    Warnings arrive already rendered because their copy names numbers a reason
    CODE cannot carry; blockers keep the mechanical code-to-prose rendering.
    Both live in one list because ``severity`` is what separates them.
    """

    return [
        *(
            {
                "severity": "blocker",
                "code": reason,
                "message": reason.replace(":", " ").replace("_", " "),
            }
            for reason in issues
        ),
        *(dict(warning) for warning in warnings),
    ]


def build_driver_safety_profile(
    topology: OutputTopology,
    *,
    manual_settings: Mapping[str, Any] | None,
    driver_research: Mapping[str, Any] | None,
    saved_at: str,
) -> dict[str, Any]:
    """Build the immutable profile for the visible current values.

    Saving the declaration IS declaring it: every write stamps the confirmation
    over the values it just wrote, so the only outcomes are ``incomplete`` (the
    declared values carry blocking issues) and ``confirmed``, and this takes
    neither a ``prior_profile`` nor a ``confirm`` flag.

    Not a dropped safety gate: the confirmation bit never authorized playback
    (``authorizes_playback`` is unconditionally ``False``) and every physical
    protection reads the declared LIMITS, not the bit. ``issues`` still blocks,
    so a garbage or half-declared profile is still fail-closed.
    """

    normalised_manual = _normalise_profile_manual_settings(topology, manual_settings)
    core, issues, warnings = _profile_core(topology, normalised_manual, driver_research)
    fingerprint = _fingerprint(core)
    confirmation: dict[str, Any] | None = None
    if not issues:
        confirmation = {
            "confirmed_fingerprint": fingerprint,
            "confirmed_at": _text(
                saved_at,
                "driver_safety_profile.confirmed_at",
                required=True,
                max_chars=64,
            ),
            # Every write of this declaration originates from an operator action
            # on a page that shows the values; there is no headless writer.
            "method": "operator_reviewed_visible_values",
        }
    status = "incomplete" if issues else "confirmed"
    profile = {
        **core,
        "profile_fingerprint": fingerprint,
        "status": status,
        "confirmation": confirmation,
        "issues": _profile_issue_payload(issues, warnings),
    }
    evaluation = evaluate_driver_safety_profile(profile, topology)
    if evaluation.status != status:
        raise DriverSafetyProfileError(
            "driver safety profile builder produced an incoherent artifact"
        )
    return profile


def _require_canonical_text_field(
    value: Mapping[str, Any],
    key: str,
    field_name: str,
    *,
    required: bool,
    max_chars: int,
) -> str | None:
    normalised = _text(
        value.get(key),
        field_name,
        required=required,
        max_chars=max_chars,
    )
    if normalised is None:
        if key in value:
            raise DriverSafetyProfileError(f"{field_name} must be omitted when empty")
    elif value.get(key) != normalised:
        raise DriverSafetyProfileError(f"{field_name} is not canonical")
    return normalised


def _validate_driver_safety_profile_shape(profile: Mapping[str, Any]) -> None:
    _reject_unknown_keys(
        profile,
        "driver_safety_profile",
        {
            "artifact_schema_version",
            "kind",
            "topology_id",
            "targets",
            "research",
            "authority",
            "authorizes_playback",
            "profile_fingerprint",
            "status",
            "confirmation",
            "issues",
        },
    )
    if type(profile.get("artifact_schema_version")) is not int:  # noqa: E721
        raise DriverSafetyProfileError(
            "driver_safety_profile.artifact_schema_version must be integer 1"
        )
    if profile.get("artifact_schema_version") != DRIVER_SAFETY_PROFILE_SCHEMA_VERSION:
        raise DriverSafetyProfileError(
            "driver_safety_profile.artifact_schema_version must be integer 1"
        )
    if profile.get("kind") != DRIVER_SAFETY_PROFILE_KIND:
        raise DriverSafetyProfileError("driver_safety_profile kind is unsupported")
    _require_canonical_text_field(
        profile,
        "topology_id",
        "driver_safety_profile.topology_id",
        required=True,
        max_chars=160,
    )
    # ``needs_confirmation`` is no longer WRITTEN but stays readable, so a box
    # carrying an older profile is not reported as corrupt.
    if profile.get("status") not in {
        "incomplete",
        "needs_confirmation",
        "confirmed",
    }:
        raise DriverSafetyProfileError("driver_safety_profile status is unsupported")
    if not _is_sha256(profile.get("profile_fingerprint")):
        raise DriverSafetyProfileError(
            "driver_safety_profile.profile_fingerprint is invalid"
        )
    if profile.get("authority") != "operator_visible_values":
        raise DriverSafetyProfileError("driver_safety_profile authority is invalid")
    if profile.get("authorizes_playback") is not False:
        raise DriverSafetyProfileError(
            "driver_safety_profile must not authorize playback"
        )
    research = profile.get("research")
    if not isinstance(research, Mapping):
        raise DriverSafetyProfileError(
            "driver_safety_profile.research must be an object"
        )
    _reject_unknown_keys(
        research,
        "driver_safety_profile.research",
        {"request_fingerprint", "result_fingerprint", "advisory_only"},
    )
    if research.get("advisory_only") is not True:
        raise DriverSafetyProfileError(
            "driver_safety_profile.research must remain advisory"
        )
    for digest_field in ("request_fingerprint", "result_fingerprint"):
        digest = research.get(digest_field)
        if digest is not None and not _is_sha256(digest):
            raise DriverSafetyProfileError(
                f"driver_safety_profile.research.{digest_field} is invalid"
            )
    targets = _sequence(
        profile.get("targets"),
        "driver_safety_profile.targets",
        maximum=16,
    )
    for index, target in enumerate(targets):
        field_name = f"driver_safety_profile.targets[{index}]"
        if not isinstance(target, Mapping):
            raise DriverSafetyProfileError(f"{field_name} must be an object")
        _reject_unknown_keys(
            target,
            field_name,
            {
                "target_id",
                "target_fingerprint",
                "speaker_group_id",
                "speaker_group_mode",
                "role",
                "driver_style",
                "target_values_binding",
                "physical_output_index",
                "model",
                "manufacturer",
                # Optional, not required: an older profile carries neither and
                # is still a sound declaration — its projections re-derive from
                # its own protective high-pass by the legacy path. It simply has
                # no published slope to gate a pinned crossover with.
                "recommended_highpass_hz",
                "recommended_highpass_slope_db_per_octave",
                "hard_excitation_band_hz",
                "required_protection_filters",
                "measurement_band_hz",
                "level_duration_limits",
                "cabinet",
                "unknowns",
                "field_provenance",
                "authority",
                "code_owned_policy",
            },
        )
        _require_canonical_text_field(
            target,
            "target_id",
            f"{field_name}.target_id",
            required=True,
            max_chars=160,
        )
        target_fingerprint = _require_canonical_text_field(
            target,
            "target_fingerprint",
            f"{field_name}.target_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(target_fingerprint):
            raise DriverSafetyProfileError(
                f"{field_name}.target_fingerprint is invalid"
            )
        for key, max_chars in (
            ("speaker_group_id", 160),
            ("speaker_group_mode", 64),
            ("role", 32),
        ):
            _require_canonical_text_field(
                target,
                key,
                f"{field_name}.{key}",
                required=True,
                max_chars=max_chars,
            )
        for key in ("model", "manufacturer"):
            _require_canonical_text_field(
                target,
                key,
                f"{field_name}.{key}",
                required=False,
                max_chars=120,
            )
        _require_canonical_text_field(
            target,
            "driver_style",
            f"{field_name}.driver_style",
            required=True,
            max_chars=80,
        )
        if target.get("target_values_binding") not in {
            "explicit_target",
            "unique_legacy_role",
            "missing",
        }:
            raise DriverSafetyProfileError(
                f"{field_name}.target_values_binding is invalid"
            )
        code_policy = target.get("code_owned_policy")
        if not isinstance(code_policy, Mapping):
            raise DriverSafetyProfileError(
                f"{field_name}.code_owned_policy must be an object"
            )
        _reject_unknown_keys(
            code_policy,
            f"{field_name}.code_owned_policy",
            {
                "policy_version",
                "max_auto_level_dbfs",
                "min_highpass_hz",
                "floor_test_frequency_hz",
                "floor_test_duration_ms",
            },
        )
        current_policy = driver_protection_profile(
            target.get("role"),
            driver_style=target.get("driver_style"),
            declared_floor_hz=driver_excitation_floor_hz(target),
        )
        expected_policy = {
            "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
            "max_auto_level_dbfs": current_policy.max_auto_level_dbfs,
            "min_highpass_hz": current_policy.min_highpass_hz,
            "floor_test_frequency_hz": current_policy.floor_test_frequency_hz,
            "floor_test_duration_ms": current_policy.floor_test_duration_ms,
        }
        if _canonical_json(code_policy) != _canonical_json(expected_policy):
            raise DriverSafetyProfileError(
                f"{field_name}.code_owned_policy is stale or noncanonical"
            )
        if "physical_output_index" in target:
            output_index = target.get("physical_output_index")
            if isinstance(output_index, bool) or not isinstance(output_index, int):
                raise DriverSafetyProfileError(
                    f"{field_name}.physical_output_index must be an integer"
                )
            if output_index < 0:
                raise DriverSafetyProfileError(
                    f"{field_name}.physical_output_index must be >= 0"
                )
        normalised_safety = normalise_driver_safety_fields(
            target,
            field_name,
            include_research_evidence=False,
        )
        safety_fields = {
            "hard_excitation_band_hz",
            "required_protection_filters",
            "measurement_band_hz",
            "level_duration_limits",
            "cabinet",
        }
        # The low limit's OWNER pair is canonicalised with the projections but
        # NOT re-derived below: an older profile carries neither field, so both
        # sides of this comparison omit them and agree, while re-deriving would
        # ADD a ``recommended_highpass_hz`` the stored target never had.
        declared_fields = safety_fields | {
            "recommended_highpass_hz",
            "recommended_highpass_slope_db_per_octave",
        }
        raw_declared = {key: target[key] for key in declared_fields if key in target}
        if _canonical_json(raw_declared) != _canonical_json(normalised_safety):
            raise DriverSafetyProfileError(
                f"{field_name} safety fields are not canonical"
            )
        raw_safety = {key: target[key] for key in safety_fields if key in target}
        # ... and they must still be the PROJECTION of this target's own
        # declared low limit. A stored profile whose bands and protective
        # high-pass disagree about where the driver stops is refused rather than
        # read, because reading it would pick one of two answers silently.
        rederived = apply_driver_low_limit(
            normalised_safety,
            role=target.get("role"),
            driver_style=target.get("driver_style"),
        )
        rederived_safety = {
            key: rederived[key] for key in safety_fields if key in rederived
        }
        if _canonical_json(raw_safety) != _canonical_json(rederived_safety):
            raise DriverSafetyProfileStaleLowLimitError(
                f"{field_name} safety fields no longer match this driver's "
                "declared low limit; review the driver profile at /sound/ and "
                "save it again"
            )
        normalised_unknowns = _normalise_unknowns(
            target.get("unknowns"),
            f"{field_name}.unknowns",
        )
        if "unknowns" not in target or _canonical_json(
            target.get("unknowns")
        ) != _canonical_json(normalised_unknowns):
            raise DriverSafetyProfileError(f"{field_name}.unknowns are not canonical")
        normalised_provenance = _normalise_field_provenance(
            target.get("field_provenance"),
            f"{field_name}.field_provenance",
        )
        if "field_provenance" not in target or _canonical_json(
            target.get("field_provenance")
        ) != _canonical_json(normalised_provenance):
            raise DriverSafetyProfileError(
                f"{field_name}.field_provenance is not canonical"
            )
        if target.get("authority") != "operator_visible_values":
            raise DriverSafetyProfileError(f"{field_name}.authority is invalid")
    confirmation = profile.get("confirmation")
    if confirmation is not None:
        if not isinstance(confirmation, Mapping):
            raise DriverSafetyProfileError(
                "driver_safety_profile.confirmation must be an object or null"
            )
        _reject_unknown_keys(
            confirmation,
            "driver_safety_profile.confirmation",
            {"confirmed_fingerprint", "confirmed_at", "method"},
        )
        confirmed_fingerprint = _require_canonical_text_field(
            confirmation,
            "confirmed_fingerprint",
            "driver_safety_profile.confirmation.confirmed_fingerprint",
            required=True,
            max_chars=64,
        )
        if not _is_sha256(confirmed_fingerprint):
            raise DriverSafetyProfileError(
                "driver_safety_profile.confirmation.confirmed_fingerprint is invalid"
            )
        _require_canonical_text_field(
            confirmation,
            "confirmed_at",
            "driver_safety_profile.confirmation.confirmed_at",
            required=True,
            max_chars=64,
        )
        _require_canonical_text_field(
            confirmation,
            "method",
            "driver_safety_profile.confirmation.method",
            required=True,
            max_chars=80,
        )
    for index, issue in enumerate(
        _sequence(profile.get("issues"), "driver_safety_profile.issues", maximum=64)
    ):
        if not isinstance(issue, Mapping):
            raise DriverSafetyProfileError(
                f"driver_safety_profile.issues[{index}] must be an object"
            )
        _reject_unknown_keys(
            issue,
            f"driver_safety_profile.issues[{index}]",
            set(PROFILE_ISSUE_FIELD_MAX_CHARS),
        )
        for key, max_chars in PROFILE_ISSUE_FIELD_MAX_CHARS.items():
            _require_canonical_text_field(
                issue,
                key,
                f"driver_safety_profile.issues[{index}].{key}",
                required=True,
                max_chars=max_chars,
            )


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


#: Target field names a stored safety PROFILE may carry that this build has
#: RETIRED. The only bounds entitled to refuse a corner are the drivers'
#: declared hard excitation bands.
#:
#: **Deliberately not** :data:`~._common.LEGACY_DROPPED_DRIVER_FIELDS`: that set
#: answers "may a stored DRIVER RECORD still carry this at a write gate"
#: (tolerated and dropped), this one answers "does a stored PROFILE TARGET
#: carrying it read as stale-but-fixable rather than corrupt" (reported, never
#: dropped, because the profile is re-derived rather than edited). Retiring
#: another per-driver field means deciding for BOTH.
_RETIRED_TARGET_FIELDS = frozenset({"crossover_search_band_hz"})

#: A stored profile that is structurally fine except that it names a retired
#: field. Reported under its own name rather than the generic schema-invalid
#: one, because the household's only question is "will saving fix it?" and the
#: answer is yes. No auto-migration: silently rewriting a confirmed safety
#: declaration behind the operator's back is the wrong direction for a
#: declaration whose whole point is that a human made it.
DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON = "driver_safety_profile_retired_field"


def _retired_fields_present(profile: Mapping[str, Any]) -> bool:
    """Whether any stored target still carries a field this build retired.

    Total by construction: it runs inside an except branch whose contract is to
    REPORT rather than raise, so an unreadable target contributes ``False``.
    """
    targets = profile.get("targets")
    if not isinstance(targets, list):
        return False
    return any(
        isinstance(target, Mapping) and not _RETIRED_TARGET_FIELDS.isdisjoint(target)
        for target in targets
    )


def _stale_low_limit_rebuild_issues(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """The blocking issues a REBUILD of this stale profile would carry.

    Same derivation and vocabulary the rebuild uses, so the two cannot disagree
    about whether confirming is possible. Total by construction: a target this
    cannot read contributes nothing rather than raising.
    """

    targets = profile.get("targets")
    if not isinstance(targets, list):
        return ()
    issues: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        try:
            derived = apply_driver_low_limit(
                target,
                role=target.get("role"),
                driver_style=target.get("driver_style"),
            )
            found = _target_issues(derived)
        except (KeyError, TypeError, ValueError):
            continue
        for issue in found:
            if issue not in issues:
                issues.append(issue)
    return tuple(issues)


def evaluate_driver_safety_profile(
    profile: Any,
    topology: OutputTopology,
) -> DriverSafetyProfileEvaluation:
    """Evaluate schema, integrity, confirmation, and current target binding."""

    if not isinstance(profile, Mapping):
        return DriverSafetyProfileEvaluation(
            "missing", False, None, ("driver_safety_profile_missing",)
        )
    try:
        _validate_driver_safety_profile_shape(profile)
    except DriverSafetyProfileStaleLowLimitError:
        # Named separately from the generic malformed case so /sound/ can say
        # "save it again" instead of "corrupt". Still a RETURN, never a raise,
        # so a box carrying a split declaration reports rather than crash-looping.
        #
        # The reasons carry MORE than the name, because the name alone cannot
        # answer "will saving fix it?": deriving the low limit raises the hard
        # band's lower edge, which can leave the analysis window outside it or
        # the limit implausible, and the rebuild then lands ``incomplete``.
        # Appending the rebuild's own blockers lets /sound/ name the field to
        # fix first instead of sending the household round a loop.
        fingerprint = profile.get("profile_fingerprint")
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint) if isinstance(fingerprint, str) else None,
            (
                "driver_safety_profile_low_limit_stale",
                *_stale_low_limit_rebuild_issues(profile),
            ),
        )
    except DriverSafetyProfileError:
        fingerprint = profile.get("profile_fingerprint")
        # A profile written before a field was RETIRED is not corrupt, and
        # saying "JTS could not read these limits" sends the household looking
        # for damage that is not there. Named separately so /sound/ can state
        # the specific remedy: save the visible values again.
        if _retired_fields_present(profile):
            return DriverSafetyProfileEvaluation(
                "malformed",
                False,
                str(fingerprint) if isinstance(fingerprint, str) else None,
                (DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON,),
            )
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint) if isinstance(fingerprint, str) else None,
            ("driver_safety_profile_schema_invalid",),
        )
    fingerprint = profile.get("profile_fingerprint")
    if (
        profile.get("artifact_schema_version") != DRIVER_SAFETY_PROFILE_SCHEMA_VERSION
        or profile.get("kind") != DRIVER_SAFETY_PROFILE_KIND
        or not _is_sha256(fingerprint)
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint) if isinstance(fingerprint, str) else None,
            ("driver_safety_profile_schema_invalid",),
        )
    core = {
        key: profile.get(key)
        for key in (
            "artifact_schema_version",
            "kind",
            "topology_id",
            "targets",
            "research",
            "authority",
            "authorizes_playback",
        )
    }
    if _fingerprint(core) != fingerprint:
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_fingerprint_mismatch",),
        )
    current_targets = active_driver_targets(topology)
    saved_targets = profile.get("targets", [])
    targets_match = len(saved_targets) == len(current_targets)
    if targets_match:
        for saved, current in zip(saved_targets, current_targets):
            expected = {
                "target_id": str(current["target_id"]),
                "target_fingerprint": str(current["target_fingerprint"]),
                "speaker_group_id": str(current["speaker_group_id"]),
                "speaker_group_mode": str(current["speaker_group_mode"]),
                "role": str(current["role"]),
                "physical_output_index": current.get("output_index"),
            }
            group = next(
                (
                    item
                    for item in topology.speaker_groups
                    if item.id == current["speaker_group_id"]
                ),
                None,
            )
            channel = next(
                (
                    item
                    for item in (group.channels if group is not None else ())
                    if item.role == current["role"]
                ),
                None,
            )
            expected["driver_style"] = (
                channel.driver_style
                if channel and channel.driver_style
                else "unspecified"
            )
            if any(saved.get(key) != value for key, value in expected.items()):
                targets_match = False
                break
    if profile.get("topology_id") != topology.topology_id or not targets_match:
        return DriverSafetyProfileEvaluation(
            "stale",
            False,
            str(fingerprint),
            ("driver_safety_profile_target_mismatch",),
        )
    derived_issues: list[str] = []
    derived_warnings: list[dict[str, str]] = []
    for target in saved_targets:
        derived_issues.extend(_target_issues(target))
        derived_warnings.extend(_target_low_limit_warnings(target))
    if not saved_targets:
        derived_issues.append("active_driver_targets_missing")
    expected_issue_payload = _profile_issue_payload(derived_issues, derived_warnings)
    if _comparable_issue_payload(profile.get("issues")) != _comparable_issue_payload(
        expected_issue_payload
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_derived_state_mismatch",),
        )
    confirmation = profile.get("confirmation")
    if derived_issues:
        if profile.get("status") != "incomplete" or confirmation is not None:
            return DriverSafetyProfileEvaluation(
                "malformed",
                False,
                str(fingerprint),
                ("driver_safety_profile_derived_state_mismatch",),
            )
        return DriverSafetyProfileEvaluation(
            "incomplete",
            False,
            str(fingerprint),
            tuple(derived_issues),
        )
    if profile.get("status") == "needs_confirmation" and confirmation is None:
        # Read under the CURRENT definition of confirmed: structurally sound and
        # bound to this hardware is the whole of what the word means now, and
        # the missing second human acknowledgement no longer exists. The next
        # save collapses the stored status; no migration pass is needed.
        return DriverSafetyProfileEvaluation("confirmed", True, str(fingerprint), ())
    if (
        profile.get("status") != "confirmed"
        or not isinstance(confirmation, Mapping)
        or confirmation.get("confirmed_fingerprint") != fingerprint
        or confirmation.get("method") != "operator_reviewed_visible_values"
    ):
        return DriverSafetyProfileEvaluation(
            "malformed",
            False,
            str(fingerprint),
            ("driver_safety_profile_derived_state_mismatch",),
        )
    return DriverSafetyProfileEvaluation("confirmed", True, str(fingerprint), ())
