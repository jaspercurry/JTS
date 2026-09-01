# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-research request and confirmed safety-profile contracts.

This module is deliberately silent.  It turns the current physical active-
speaker targets plus operator-visible limits into immutable JSON contracts; it
does not generate a signal, compile a filter, load CamillaDSP, or grant
playback permission.

Research remains advice.  A version-2 research result must echo the exact
server-authored request and target identities, but only the values visible in
``manual_settings`` enter the confirmed safety profile.  Downstream audio code
must additionally perform its own excitation and live-graph admission checks.
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

#: Field caps for one entry in a profile's ``issues`` list, and the whole
#: allowed key set. ONE owner because two places need the same numbers: the
#: shape validator ENFORCES them, and ``_target_low_limit_warnings`` FITS its
#: rendered message to them. Splitting those is what let a warning grow past the
#: message cap and take the save down with it -- see that function's docstring.
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
#: Cap for a provenance entry's single free-text ``source`` citation. Deliberately
#: the same budget as a ``sources[]`` URL (320): the citation slot legitimately
#: holds a datasheet URL, so any URL the list accepts must be promotable here
#: verbatim. A tighter cap would refuse a legal source for being long, and the
#: /sound/ echo-back panel wraps a long citation rather than depending on it
#: fitting one line.
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
    # #1665 component entry -- design_draft.py's own manual-driver allowlist
    # already accepts these; this allowlist re-validates the SAME normalised
    # record when build_driver_safety_profile is called with it (design_draft
    # threads manual_settings straight through), so it must accept them too.
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
    """A stored profile predates the one-owner low-limit collapse (#2603).

    Its own bands and protective high-pass disagree about where the driver
    stops, which is what the 2026-08-17 ruling exists to end. Split out from
    the generic malformed case so the household is told the ACTIONABLE thing --
    review the driver profile at /sound/ and save it again -- rather than
    "schema invalid", which reads as corruption and suggests no remedy.
    Playback is unaffected: the staged CamillaDSP graph is a separate artifact,
    so the speaker keeps working while the profile waits to be rebuilt.
    """


@dataclass(frozen=True)
class DriverSafetyProfileEvaluation:
    """Fail-closed freshness result for one persisted safety profile.

    ``confirmed_and_current`` means the declaration is schema-valid, its
    fingerprint is intact, it is bound to the CURRENT hardware targets, and it
    carries no blocking issues.  It is explicitly not permission to emit audio;
    excitation and live protected-graph checks remain separate downstream
    gates.  It is also not a record that a human clicked anything: saving the
    declaration is declaring it, so every write of structurally sound values
    lands ``confirmed``.  What still evaluates false is what was always worth
    failing closed on -- ``missing``, ``malformed``, ``stale`` (the outputs
    moved underneath it), and ``incomplete`` (the declared values carry
    blocking issues).
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

    Active two/three-way targets reuse the measurement contract verbatim. A
    passive full-range layout has no independently measurable crossover target,
    but it still has a physical component the user can identify and research.
    Keep that research-only case here rather than broadening
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
    load that knows the topology re-stamps it, the same way
    ``driver_safety_profile_evaluation`` is re-stamped, so a saved copy can
    never be read back as current policy.  (``load_design_draft`` without a
    topology cannot re-derive either one and returns the disk copy untouched;
    the /sound/ page always loads with a topology.)

    It exists because the browser has to answer one question before anything is
    saved, and it is policy the browser must not own a second copy of: *has
    this target delegated its level?*  Since the 2026-08-23 ruling that is
    normally answered by the peak being ABSENT, but a profile saved under the
    retired contract carries the class default itself and means the same thing,
    so the page still needs that target's ``max_auto_level_dbfs`` to recognise
    it.  The confirmed safety profile
    carries the same number in ``code_owned_policy``, but only **after** a
    save; the echo-back panel renders straight after a paste.

    It answered a second question until 2026-08-20 — *what bounds the level
    protection then picks?* — by publishing ``HF_MEASUREMENT_ABS_CEILING_DBFS``.
    That constant is retired (see the block comment above
    ``derive_hf_measurement_ceiling_dbfs``): the bound is now the per-driver
    sensitivity derivation, which this topology-only view cannot compute — it
    has neither the confirmed caps nor the declared sensitivities.  So the
    field is gone rather than restated as the global test ceiling, which would
    have told the household a true but empty number.

    ``role_class`` travels with each target so the page never has to keep its
    own copy of which roles are high-frequency.

    It published the raw class-table ``min_highpass_hz`` until #2874, on the
    reasoning that it was the only field that *discriminates* — every
    high-frequency style shares ``max_auto_level_dbfs = -65``, so it proved the
    view came from ``driver_protection_profile`` rather than a restated
    constant.  That is a self-referential reason to keep an AMBIGUITY: on a
    tweeter declared at B&C's published 1.6 kHz the draft then showed 1600 and
    2000 side by side with nothing saying which one bounds the corner, and two
    readers independently took the 2000 for a second floor.  What replaced it
    resolves that and discriminates just as well, because provenance IS a
    discriminator: ``low_limit_hz`` with ``low_limit_provenance`` and a rendered
    ``low_limit_summary`` — "1600 Hz (manufacturer declared)" where the
    household declared one, "5000 Hz (class fallback; nothing declared)" where
    nobody did.  So the class figure still travels; it just never travels
    unlabelled beside a declared one.

    Resolving that needs the operator's visible values, which is why
    ``manual_settings`` is a parameter.  Without them every target reports the
    class fallback — honestly labelled as such rather than silently passed off
    as the declaration.

    ``policy_version`` has no reader on the page yet and is kept deliberately:
    it is staleness detection on a view whose whole contract is that it gets
    re-stamped, and without it a copy captured under an older policy is
    indistinguishable from a current one.  ``role`` had no such property and is
    not emitted: ``role_class`` answers every question the page asks.
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
    legacy per-role entry when that role appears exactly once — because two
    surfaces now need it: ``_profile_core``, which freezes those values, and
    ``driver_protection_policy_view``, which must resolve the SAME declaration
    the freeze will.  A second copy of this rule would let the page explain one
    number while the profile stored another.

    The second element is ``True`` only for the legacy per-role read, which is
    what ``target_values_binding`` records.
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

    ``target_id`` is ``f"{group.id}:{channel.role}"`` throughout this module;
    keep that shape here so this stays the same lookup ``_profile_core`` and
    ``build_driver_research_request`` build inline.
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
            # "This filter is required but its numbers are unpublished" has no
            # encoding here, and deliberately still does not get one: under the
            # best-estimate contract (see ``build_driver_research_prompt``) the
            # honest answer to an unpublished protective cutoff is the
            # researcher's best engineering estimate, declared as one, that an
            # operator can see and correct -- not a marker that leaves the
            # driver unprotected-but-declared. The message says that out loud
            # because the refusal is what the operator reads.
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

    **The provenance contract (owner ruling, issue #2195).**  A value carries
    three facts: the number itself, whether it is a published figure or the
    researcher's best engineering estimate, and one citation either way.

    ``source`` is that citation, and it is a short *free string* rather than a
    URL because the honest answer is often a name -- "Dayton CX120-8 datasheet,
    p.2" -- and for an estimate it is the one fact the estimate leaned on.  It
    is separate from ``sources`` (a URL list) for that reason, and it is
    **optional**: this is a tinker box, and a reply that gives a number without
    a citation must still land rather than be stonewalled.  The absent key is
    omitted from the output rather than stored as ``None``, so a provenance
    entry written before this key existed normalises byte-identically and an
    already-confirmed safety profile stays canonical.

    There is deliberately no ``state`` key.  ``confidence`` is the single
    writer of "published or estimated"; display derives the badge from it (see
    ``deploy/assets/sound-profile/js/main.js``, ``driverProvenanceState``).
    Accepting both would give one fact two owners that can disagree.
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
    # The OWNER of this driver's low limit (2026-08-17 ruling, #2603) is parsed
    # here rather than alongside the advisory display fields, because that is
    # what it is: a safety field, and the one every other low-limit field
    # derives from. Parsing it at this shared point is what gives it a single
    # parse site across the design draft, the research result, and the safety
    # profile's own manual settings.
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
    #: The same targets as ``targets``, except that any retired key the stored
    #: context still carries is put BACK, verbatim. It reconstructs the core the
    #: build that wrote this request fingerprinted; see the fingerprint check
    #: below for why tolerating the allowlist is not enough on its own.
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
            # Tolerated, never stored: this context is built by
            # ``normalise_driver_safety_fields``, so a request PERSISTED in the
            # draft by an older build carries whatever that build's normaliser
            # emitted -- and ``design_draft.build_design_draft`` re-validates the
            # stored request on EVERY save. Refusing here would make an old
            # draft unsaveable over a key the ask no longer sends. The
            # normaliser below drops it. See LEGACY_DROPPED_DRIVER_FIELDS.
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
        # The retired keys as this request STORED them. Copied verbatim rather
        # than re-derived: the stored context IS an older
        # ``normalise_driver_safety_fields`` output, so the stored value is
        # already in that build's normalised form and copying it reproduces the
        # fingerprinted bytes exactly, whatever the key's type was.
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
    # sufficient**, because the context is FINGERPRINTED. The build that wrote
    # this request hashed a core whose context still carried the key; dropping
    # it here recomputes a different digest, and a request a real box stored
    # would be refused "fingerprint is invalid" on the deploy that retired the
    # field -- on the very save the household's remedy copy tells them to make.
    #
    # Same posture, and the same sentence, as
    # ``crossover_v2.topology_prescription._parse_prescription``'s read-back:
    # a document this repository already wrote must stay readable across the
    # deploy that retired the field. So a stored digest is accepted when it
    # matches the core computed WITH the retired context field present.
    #
    # TRANSITIONAL by construction: the returned record is re-stamped with
    # ``current_fingerprint`` below, so the draft that saves it is written in
    # the new shape and matches on its own terms next time. The re-stamp is also
    # load-bearing rather than hygiene -- the staleness check immediately below
    # compares this record against a freshly built one, which carries the
    # current digest.
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


# --- One implausible low limit, two authors, two answers (#2874) -------------
#
# Owner ruling, 2026-08-22: declared values are the only refusing authority;
# class tables may prefill, disclose, and serve as fallback -- they must never
# refuse a declaration. The plausibility band is anchored on the class table,
# so as a BLOCKER over a typed number it was the code overruling its owner with
# a class guess, on a field where nothing else gets a chaperone (sensitivity,
# level caps and hard bands all accept typed garbage). It is split by AUTHOR
# instead, because the two authors are not the same kind of claim:
#
#   * a RESEARCH REPLY outside the band is still REFUSED at intake, below. An
#     LLM misreading a datasheet is not an operator's choice, "ask again with
#     the datasheet" is the right answer, and refusing at the paste means the
#     bad number never becomes a value the household has to un-declare;
#   * an OPERATOR-TYPED value outside the band lands a loud warning that SAVES
#     (``_target_low_limit_warnings``). The tinker box trusts its owner -- and
#     says so, concretely, first.
#
# What actually protects the driver at a low declared figure is unchanged and
# lives elsewhere: the derived protective high-pass sitting AT that frequency
# and proved in the emitted graph by ``graph_safety``, the absolute corner
# floor, the commissioning high-pass at a multiple of the crossover corner, the
# ``path_safety`` load gate, and the excitation level ceilings.


#: How much of a ``driver_style`` one diagnosis sentence may quote verbatim.
#: The style is FREE-FORM up to 80 characters (``output_topology`` takes any
#: safe id), while the longest style this build registers is 23
#: (``horn_compression_driver``) -- so one more than that ellipsizes only a
#: value no table here describes, and keeps the rendered sentence a bounded
#: length whatever the household typed into the picker.
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

    Shared by both arms of the split above so the refusal and the warning
    cannot describe the same number differently. Diagnosis only -- each arm
    appends its own action, because the actions are what differ.

    The interpolated style is ellipsized (:data:`_DIAGNOSIS_STYLE_MAX_CHARS`)
    because it is operator-supplied free text, not a value from any table here.
    Quoting it verbatim made the sentence's length an operator input, and the
    warning arm's message has a schema cap -- so an 80-character style turned a
    DISCLOSURE into a save that hard-failed, on exactly the out-of-band
    declaration the disclosure exists to allow.
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
    # default divided and multiplied by the factor, so the long form spent ~15
    # characters restating it -- and those characters are the headroom the
    # warning arm needs under its message cap once the unknown-style branch is
    # reachable (both review lenses, #2884).
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

    The REFUSING arm of the author split above, and the only one: this reads
    the pasted packet, never a saved declaration. An operator who has actually
    read the datasheet can still enter the same number by hand under Advanced,
    and the message says so rather than leaving them to guess that a route
    exists.
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


_PROMPT_TARGET_KEYS = (
    "target_id",
    "target_fingerprint",
    "role",
    "manufacturer_and_model",
    "driver_style",
    "speaker_group_id",
    "speaker_group_mode",
    "operator_declared_context",
)

# Keys whose value directly bounds what the speaker is allowed to excite. Only
# these carry per-field provenance in the ask: demanding confidence + basis +
# URLs for every field is what turned a data reply into an essay, and the
# remaining fields are advisory prefill an operator reviews anyway.
_PROMPT_PROVENANCE_KEYS = (
    "hard_excitation_band_hz",
    "recommended_highpass_hz",
    "required_protection_filters",
    "level_duration_limits",
    "sensitivity_db_2v83_1m",
)


def _driver_research_prompt_targets(request: Mapping[str, Any]) -> str:
    """Return the compact target projection the assistant actually needs."""

    targets = [
        {
            key: target[key]
            for key in _PROMPT_TARGET_KEYS
            if target.get(key) is not None
        }
        for target in request.get("targets", [])
        if isinstance(target, Mapping)
    ]
    projection: dict[str, Any] = {
        "request_fingerprint": request.get("request_fingerprint"),
        "targets": targets,
    }
    if request.get("build_notes"):
        projection["build_notes"] = request["build_notes"]
    return json.dumps(projection, indent=1, sort_keys=True)


def _prompt_example_highpass_hz(request: Mapping[str, Any]) -> float:
    """The worked RESULT SHAPE example's tweeter cutoff, in Hz.

    Read from policy for the same reason ``_driver_research_prompt_limits``
    is: a fixed constant here suits only the styles whose class default it
    happens to sit near.  A hard-coded 3000 reads as a worked answer to a
    ribbon (class default 5000) or supertweeter (8000) target whose own LIMITS
    line one screen above states a plausibility band anchored somewhere else —
    a worked example arguing with the bounds printed beside it is precisely the
    deadlock #2186 exists to end.  (Until the 2026-08-17 ruling that mismatch
    was also REFUSED, as ``tweeter:highpass_below_code_policy``; a sourced
    figure now wins outright, so the cost is a confusing example rather than a
    rejected reply.)

    The strictest high-frequency floor among this request's targets is used, so
    a mixed request cannot teach a cutoff that is illegal for one of its own
    drivers.  The floor itself is a legal and conservative worked answer; a
    request with no high-frequency target falls back to the class default for
    an undeclared tweeter, which is the most conservative entry in the table.
    """

    floors = [
        policy.min_highpass_hz
        for target in request.get("targets", [])
        if isinstance(target, Mapping)
        for policy in (
            driver_protection_profile(
                str(target.get("role") or ""),
                driver_style=target.get("driver_style"),
            ),
        )
        if policy.min_highpass_hz is not None
    ]
    if floors:
        return max(floors)
    return driver_protection_profile("tweeter").min_highpass_hz or 5000.0


def _driver_research_prompt_limits(request: Mapping[str, Any]) -> list[str]:
    """Return the LIMITS section: its heading, its preamble, and its bounds.

    One gate enforces them, on the one line emitted per target: the low-limit
    band by :func:`validate_research_low_limit_plausibility` at intake.

    Read from the low-limit band owner rather than restated as prose constants,
    so the ask cannot drift from what the gate actually refuses.  Naming the
    bound in the ask is what lets an *estimated* answer land instead of
    deadlocking: the gate still refuses an out-of-bounds value on its own, so
    telling the researcher the bound can only raise the chance of an acceptable
    first reply, never widen it.

    They are bounds, not recommended values.  The prompt says so out loud
    because a floor stated without that caveat reads as a target, and would
    talk a researcher *down* from a stricter published requirement.

    **There is no level bound here, and its absence is the point.**  The ask
    carried ``max_effective_peak_dbfs at or below <class default>`` until
    2026-08-23, which made a code figure the ceiling a researcher had to clear
    — for a tweeter, −65 dBFS — and then treated the number that came back as a
    declaration.  The owner struck it: "There should not be any nannies in our
    audio pipeline... No limit unless we have no headroom to give."  A level
    limit is now asked for only where a manufacturer publishes one.

    The heading is emitted only when a bound exists.  Every remaining bound is
    per-target and optional (a low-frequency role has no low-limit anchor at
    all), so a request can legitimately have none — and a heading whose two
    preamble sentences say "these bounds" above an empty list describes a gate
    that is not there.

    These lines are prompt text only.  ``request`` itself, and therefore
    ``request_fingerprint``, is untouched.
    """

    lines: list[str] = []
    for target in request.get("targets", []):
        if not isinstance(target, Mapping):
            continue
        band = driver_low_limit_plausibility_band_hz(
            str(target.get("role") or ""),
            driver_style=target.get("driver_style"),
        )
        if band is None:
            continue
        lines.append(
            f"- {target.get('target_id')}: recommended_highpass_hz between "
            f"{band[0]:g} and {band[1]:g} if published, else null."
        )
    if not lines:
        return []
    return [
        "LIMITS",
        "This build refuses a reply outside these bounds. They are outer bounds, not recommended values: when a published requirement is stricter, the published one wins.",
        "The minimum-crossover bound is a PLAUSIBILITY range, not a target. A published figure inside it is believed even when it sits below what is typical for the driver type; a figure outside it is refused as a mis-read rather than believed.",
        *lines,
        "",
    ]


def build_driver_research_prompt(request: Mapping[str, Any]) -> str:
    """Return the copyable v2 research prompt for one exact request.

    The contract with the assistant is exactly one fenced ``json`` block back,
    so the browser's paste box can recover the object from an ordinary chat
    reply.  What the prompt embeds is a *projection* of ``request`` — the
    target identities, models, and operator-declared context — never the whole
    request: the ``hardware`` block and the physical output/topology labels are
    server bookkeeping the assistant cannot use, and they were most of the
    prompt's length.  ``request`` itself stays the single source of truth; it
    is what the browser posts back and what the server binds against.

    The ask is deliberately a strict *subset* of what the parser accepts
    (see ``_V2_RESEARCH_DRIVER_FIELDS``).  Asking for less cannot invalidate a
    previously-saved or more verbose result; acceptance is unchanged.

    **The estimate contract (owner ruling, issue #2186).**  The ask used to
    forbid estimating and to call null "a correct answer".  Fields like
    ``hard_excitation_band_hz`` and ``level_duration_limits`` appear in
    essentially no consumer datasheet, while ``_target_issues`` requires them —
    so honest research plus a strict gate deadlocked most real drivers, and the
    only way through was nine hand-typed numbers most people do not have.  The
    ask now orders the answer: published value first, then an engineering
    estimate tagged ``confidence: "low"`` with its derivation in ``basis``, and
    null only for the genuinely unknowable.

    **Best estimate, declared, with a source (owner ruling, issue #2195).**
    The ask no longer requests a *conservative* estimate.  It asks for the
    researcher's best reality-grounded number from the driver's published facts
    and physics, declared as an estimate, with one citation.  Safety never
    lived in a number's timidity: it lives in ``_target_issues`` (below), the
    per-style plausibility screen on the reply, and the quiet-start ramp.  A
    wrong best-estimate degrades a measurement; it cannot
    blow a driver, while
    prompt-level lowballing costs real performance — a needlessly high cutoff
    robs usable range, a needlessly low level under-drives the measurement —
    and buys nothing the clamps do not already guarantee.  The operator is the
    arbiter: /sound/ echoes every consumed value back with its published or
    estimated badge and its source before anything is saved.

    **``max_effective_peak_dbfs`` is asked for as a published fact or not at
    all (owner ruling, 2026-08-23).**  The ask used to name a class-default
    ceiling under LIMITS and tell the assistant to "send exactly the ceiling"
    for a tweeter with no published limit — so the reply's number was a figure
    this file had injected, and everything downstream then read it as a
    declaration.  On the new-horn box that round trip pinned a 75 dB SPL seat
    target at 68.3 dB with ~30 dB of digital headroom unused.  The demand is
    gone, the key is optional, and the level a measurement runs at comes from
    the sensitivity derivation instead
    (:func:`jasper.active_speaker.driver_protection.derive_hf_measurement_ceiling_dbfs`).
    A manufacturer-published level limit is still asked for and still binds.

    What did **not** move is the protection itself.  A REPLY that lands outside
    code policy is still refused by name rather than silently clamped, so an
    operator sees the bound and decides — band nesting in ``_target_issues``,
    and the per-style low-limit band in
    :func:`validate_research_low_limit_plausibility`, which screens the paste
    before any of it becomes a declaration.  (That last one used to refuse from
    ``_target_issues`` too, over the SAVED values; #2874 split it by author, and
    a human-typed value outside the band is now disclosed rather than refused.
    A researcher reading this ask is unaffected either way: the bound the ask
    names is the bound the reply must clear.)  The quiet-start commissioning
    ramp and its acknowledgements are untouched.  Widening the *sourcing* of a
    proposed number is a different question from widening the *bound* it must
    clear, and only the first one changed.
    """

    # Dropped from the ASK (still accepted, still normalised, still prefilled
    # when a reply includes them):
    #   manufacturer          -- display-only; the browser import never copies
    #                            it into manual_settings, and the one reader
    #                            (staging._driver_spec_from_preview) has an
    #                            "Operator research" fallback.
    #   recommended_lowpass_hz-- no computational consumer; an Advanced field
    #                            the operator can type.
    #   gain_offset_db        -- a guessed level that outranks a derived one.
    #                            The browser tags an imported gain
    #                            "research_estimate", and baseline_profile's
    #                            ladder is measured > pinned > ESTIMATE >
    #                            sensitivity -- so the guess pre-empts the trim
    #                            that same reply's sensitivities would have
    #                            produced through a physical model. Level
    #                            authority is measurement's and the operator's.
    # The crossover vocabulary the KEY GUIDE states is READ from the compiler,
    # never spelled here: the reply is refused against exactly these sets when
    # it is saved (design_draft._normalise_candidate), and a prompt that asked
    # for a vocabulary the saver would reject is a deadlock nobody can see from
    # either end. Imported inside the call because this module is the research
    # surface, not an audio-graph consumer.
    from .staging import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    # The result shape is fenced because a chat UI's copy button copies the
    # code block's contents, not the prose around it.
    target_count = len(request.get("targets", []))
    entries = "entry" if target_count == 1 else "entries"
    # The worked example's own numbers, derived so they are legal for THIS
    # request's drivers and mutually nested. Round by construction (every
    # policy floor is a round number and every offset here is a multiple of
    # 500), because the ask tells the assistant an estimate should look like
    # one and the example has to model that.
    hp = int(_prompt_example_highpass_hz(request))
    hard_low = hp - 500
    return "\n".join(
        (
            "You are a loudspeaker-driver datasheet researcher. Your entire reply is data for a machine to parse, not prose for a human.",
            "",
            "OUTPUT RULE",
            "Reply with exactly one ```json fenced code block containing exactly one JSON object. No text before the fence, no text after it. Do not ask clarifying questions; record any ambiguity in unknowns instead.",
            "",
            "TASK",
            "Research the real published specifications of each target below. Use the manufacturer datasheet first and reputable independent measurements second. When sources conflict, prefer the datasheet and record the conflict in unknowns.",
            "If you can browse, browse silently: no search narration, no source summaries outside the JSON.",
            "",
            "TARGETS",
            _driver_research_prompt_targets(request),
            "",
            "ACCURACY",
            'When a datasheet or a reputable independent measurement gives the number, report that number, with provenance confidence "high" for a datasheet and "medium" for a measurement.',
            'When the number is not published, give your best reality-grounded engineering estimate from the driver\'s published facts and physics. Tag it confidence "low", say in basis how you derived it (for example "estimated: 25 mm soft dome, Fs unpublished"), and round it — an estimate should look like one.',
            "Declare every estimate as an estimate and name one source in that field's provenance either way: the datasheet or measurement for a published number, and for an estimate the one fact or document it leaned on.",
            "Use null only for a field with no engineering basis at all, and add one entry to that driver's unknowns saying which fact is missing.",
            "Never infer physical installation choices such as enclosure kind or horn or waveguide use. Treat operator_declared_context as authoritative; if an installation choice is undeclared, leave it unknown.",
            "For cabinet geometry, research radiator count, effective radiating diameter, and baffle width only when supported by evidence, while preserving any operator-declared enclosure choice.",
            "For a tweeter or compression driver, recommended_highpass_hz is the priority lookup.",
            "",
            "THE MINIMUM CROSSOVER FREQUENCY, AND HOW IT IS PUBLISHED",
            "recommended_highpass_hz is the manufacturer's minimum recommended crossover frequency for this driver. It is the single most important number in this reply: this build derives the driver's protective high-pass, the bottom of its allowed excitation band, and the bottom of its analysis window from it.",
            "Horn and compression-driver makers print it on a dedicated spec line. The exact wording varies — \"Recommended Crossover\" (B&C, BMS, 18 Sound), \"Minimum Crossover Frequency\" or \"Recommended min. crossover\" (FaitalPro, Celestion) — so match the meaning, not the phrase.",
            "Dome tweeters usually have no such line at all. Look instead at the test condition footnoted to the POWER HANDLING rating, which states the filter used: \"IEC 268-5, high-pass Butterworth, 2600 Hz, 12 dB/oct\" or \"X-over: 2. order HP Butterworth, 2.5 kHz\". That frequency is the answer.",
            "recommended_highpass_slope_db_per_octave is the slope CONDITION the manufacturer attaches to that frequency, reported separately. Convert a filter order to dB/octave: 2nd order is 12, 3rd is 18, 4th is 24. Send it only when the manufacturer states one — it is not universal, and some datasheets give the frequency with no slope at all.",
            "If the manufacturer publishes no minimum crossover frequency, send null for both and add an entry to unknowns. Absent is a correct answer here. Do NOT estimate this one: a safety margin is computed downstream by this build, and an invented number would be indistinguishable from a datasheet figure.",
            "The numbers in RESULT SHAPE below are format placeholders, not answers — recommended_highpass_hz especially. Send this driver's own published figure, or null. Never copy the example's number, and never badge a number you did not read on a datasheet as confidence \"high\".",
            "",
            "ESTIMATING THE REMAINING PROTECTION FIELDS",
            "required_protection_filters: send this ONLY for a mid, which needs a low-pass. A high-pass requirement is DERIVED from recommended_highpass_hz and must not be sent. cutoff_hz and minimum_slope_db_per_octave are both numbers, never null.",
            'A mid therefore adds exactly one entry, in this shape: "required_protection_filters": [{"kind":"lowpass","cutoff_hz":3000,"minimum_slope_db_per_octave":24,"family_or_equivalent":"equivalent_or_steeper"}]. No other role sends this key.',
            "hard_excitation_band_hz: the published usable range when there is one, otherwise the range typical for that type, tightened at both ends. Its LOWER edge is derived from recommended_highpass_hz, so what matters here is the upper edge.",
            "measurement_band_hz is the driver's published frequency-response range — for example a compression driver rated 1.0-18.0 kHz sends [1000, 18000]. Send the published range even when it extends below the minimum crossover; this build clamps the analysis window up into the allowed band itself.",
            "Nest the bands: the measurement band sits inside the hard excitation band. A reply that does not nest is refused.",
            "level_duration_limits: measurement-protocol discipline, not datasheet facts. Send max_sweep_duration_s 4, max_repeat_count 3, minimum_cooldown_s 2 unless a datasheet says stricter.",
            "max_effective_peak_dbfs is the one key in that object that IS a datasheet fact, so send it ONLY when the manufacturer publishes a level limit for this driver — a maximum input level, or a power rating stated as a limit you can convert. Omit the key entirely when they publish none; that is the ordinary answer and it is not a gap to record in unknowns. Never estimate it, and never send a protocol default in its place: this build chooses the measurement level from the driver's declared sensitivity against its low-frequency sibling's own limit, and a made-up number here would override that with a guess.",
            "",
            *_driver_research_prompt_limits(request),
            "RESULT SHAPE",
            "```json",
            "{",
            '  "artifact_schema_version": 2,',
            f'  "kind": "{DRIVER_RESEARCH_KIND}",',
            '  "request_fingerprint": "echo from TARGETS",',
            '  "drivers": [{',
            '    "target_id": "echo from TARGETS",',
            '    "target_fingerprint": "echo from TARGETS",',
            '    "role": "full_range|woofer|mid|tweeter",',
            '    "model": "echo manufacturer_and_model from TARGETS",',
            '    "nominal_impedance_ohm": 8,',
            '    "sensitivity_db_2v83_1m": 90,',
            f'    "usable_frequency_range_hz": [{hp - 1000}, 20000],',
            f'    "recommended_highpass_hz": {hp},',
            '    "recommended_highpass_slope_db_per_octave": 12,',
            f'    "hard_excitation_band_hz": [{hard_low}, 20000],',
            f'    "measurement_band_hz": [{hp - 1000}, 18000],',
            '    "level_duration_limits": {"max_sweep_duration_s":4,"max_repeat_count":3,"minimum_cooldown_s":2},',
            '    "cabinet": {"enclosure_kind":"sealed|vented|passive_radiator|open_baffle|transmission_line|unknown","radiator_count":1,"effective_radiating_diameter_mm":null,"baffle_width_mm":null},',
            '    "driver_class": "compression_horn|soft_dome|metal_dome|beryllium_diamond_dome|ribbon_amt|unknown",',
            '    "radiating_diameter_mm": 25,',
            '    "unknowns": ["facts that could not be established"],',
            # The high-confidence exemplar is deliberately NOT
            # recommended_highpass_hz. Every other value in this shape is a
            # visible placeholder ("echo from TARGETS", "a|b|c", "https://..."),
            # but that field's example is a concrete number this build COMPUTED
            # from its own style table -- so badging it high/datasheet modelled
            # exactly the mistake this prompt forbids two screens up, and is the
            # shape jts3 shipped: an unpublished 2000 wearing a datasheet badge.
            # sensitivity_db_2v83_1m carries the published exemplar instead; it
            # is genuinely a datasheet line, so nothing false is taught.
            '    "field_provenance": {"sensitivity_db_2v83_1m":{"confidence":"high","basis":"datasheet sensitivity line","source":"manufacturer datasheet","sources":["https://..."]},"level_duration_limits":{"confidence":"low","basis":"estimated: protocol default, no published limit","source":"measurement protocol, no published limit","sources":[]}},',
            '    "notes": "one short sentence",',
            '    "sources": ["https://..."]',
            "  }],",
            '  "crossover_candidates": [{"between_roles":["woofer","tweeter"],"frequency_hz":2500,"filter_type":"Linkwitz-Riley","slope_db_per_octave":24,"confidence":"low|medium|high","rationale":"why this point","warnings":[]}]',
            "}",
            "```",
            "",
            f"Return exactly {target_count} {entries} in drivers[] — one per target above, in the same order. Copy target_id, target_fingerprint, and model verbatim.",
            "",
            "KEY GUIDE",
            "- Every numeric key names its own unit (_hz, _ohm, _db, _mm, _s, _dbfs).",
            "- All numbers are bare JSON numbers. No units, no comments, no text after a value.",
            '- Confidence vocabulary is "low", "medium", "high", or "unknown".',
            "- field_provenance covers only these five keys: "
            + ", ".join(_PROMPT_PROVENANCE_KEYS)
            + ". Each entry is confidence + basis (12 words or fewer) + source (one citation, 12 words or fewer) + at most 2 source URLs.",
            "- sources: at most 3 URLs you actually consulted for that driver.",
            "- notes: one sentence, 15 words or fewer.",
            "- crossover_candidates[].filter_type is one of: "
            + ", ".join(supported_declaration_filter_types())
            + ". crossover_candidates[].slope_db_per_octave is one of: "
            + ", ".join(
                f"{slope:g}" for slope in supported_declaration_slopes_db_per_octave()
            )
            + ". Anything else is refused when the reply is saved. This is the "
            "crossover this build compiles, and is a different question from "
            "the slope condition a datasheet states for "
            "recommended_highpass_slope_db_per_octave above.",
            "- crossover_candidates[].rationale: 15 words or fewer.",
            "",
            "STOP",
            'No introduction ("Here is the JSON:"), no summary, no caveats outside the object, nothing after the closing fence.',
            "Begin the ```json block now.",
        )
    )


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
    # ``max_effective_peak_dbfs`` is deliberately NOT in this tuple. It is the
    # one key in the object that would be a datasheet fact, so it is optional:
    # most makers publish no level limit, and requiring one only ever produced
    # an invented number. Its absence is how a target says "no published level
    # limit", which resolve_driver_excitation_ceilings reads as the delegation
    # the sensitivity derivation answers (owner ruling, 2026-08-23).
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
    # The style table used to VETO here: a declared high-pass below the class
    # default landed ``<role>:highpass_below_code_policy`` and the profile
    # landed incomplete. The 2026-08-17 ruling (#2603) reversed that -- a
    # sourced manufacturer figure wins outright, and B&C's published 1.6 kHz
    # for the DE250 is exactly the number the 2 kHz class default was rejecting.
    # A plausibility BLOCKER survived that reversal here and was itself
    # reversed by the 2026-08-22 ruling (#2874): a saved declaration is
    # operator-authored, and refusing one on a class anchor is the same
    # class-over-declaration inversion one layer down. It is now a loud
    # warning -- ``_target_low_limit_warnings`` -- and the refusing arm moved
    # to the research-reply intake, ``validate_research_low_limit_plausibility``.
    return reasons


def _target_low_limit_warnings(target: Mapping[str, Any]) -> list[dict[str, str]]:
    """Non-blocking disclosures for one stored target's declared low limit.

    Pure in the same way :func:`_target_issues` is, and for the same reason:
    ``evaluate_driver_safety_profile`` re-derives these from the stored targets
    and checks them against what the profile carries, so a hand-edited artifact
    cannot quietly drop its own warning. That check compares severity and code,
    never the sentence — see :func:`_comparable_issue_payload` for why editing
    warning copy must not un-confirm a box whose declaration never changed.

    The list holds exactly one warning: a declared low limit outside its
    style's plausibility band. It teaches rather than merely flagging -- it
    names the number, the band it missed, the anchor that band came from, and
    the two things it is most likely to be. A warning that does not say what to
    check is a nanny that mumbles.

    **The rendered message is FITTED to the schema cap, never merely expected to
    fit.** An issue message over ``PROFILE_ISSUE_FIELD_MAX_CHARS["message"]``
    fails shape validation, which lands the whole profile ``malformed`` and
    makes ``build_driver_safety_profile`` refuse the save outright -- so a
    length overrun here does not degrade a disclosure, it REFUSES the very
    out-of-band declaration the disclosure exists to permit, which is the ruling
    inverted by a rendering concern. The style is ellipsized upstream so the cut
    lands on operator free text rather than on the guidance, and this final
    clamp then guarantees the bound for every remaining input.
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
    # unknown-tweeter default, which is how a genuinely published 800 Hz on a
    # large-format horn reads as implausible on a box whose type nobody set.
    # Naming the picker as the first thing to check keeps that from reading as
    # an accusation about the datasheet -- the clause the retired JS phrasing
    # carried.
    #
    # The question is REGISTERED, not empty. An empty style never reaches here
    # from a saved profile: ``_profile_core`` stamps ``"unspecified"`` and the
    # shape validator requires the field non-empty, so a test against emptiness
    # was dead on the shipped path -- a box whose type nobody set shipped the
    # no-caveat copy. Asking the table also catches what no sentinel list would:
    # a typo'd or newer-build style that looks declared and is silently judged
    # against the same fallback.
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
        # One owner, every consumer derives (#2603). The stored target carries
        # the PROJECTION of this driver's declared low limit, not four
        # independently-typed numbers -- so the Fc sweep's floor, the
        # linearization fit band, the protection posture, and the grading bands
        # cannot disagree about where this driver stops.
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
            # "Derived" alone hides the case that actually costs an operator
            # something: a value they TYPED, replaced. /sound/ still renders an
            # editable high-pass cutoff and slope, and the derivation overwrites
            # both -- so a household that deliberately entered a stricter
            # number was told only that the field was derived, never that their
            # own entry had been superseded and by what. Naming it is what
            # makes the replacement reviewable on the page, where every unknown
            # is shown before the save that freezes it.
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
            # The low limit's OWNER travels with its projections (#2603), so a
            # reader of the confirmed profile can tell the manufacturer's
            # declaration from what this build derived FROM it. Until #2897 the
            # target carried only the projections, and the derived
            # ``required_protection_filters`` high-pass was the only slope on
            # the record -- a ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)``
            # that no reader could unmix, which is how the topology gate came
            # to refuse a household's order-2 pin against a 24 the datasheet
            # never printed.
            #
            # DECLARED provenance only. ``apply_driver_low_limit`` also fills
            # these two on an INFERRED limit (one read back out of a stored
            # protective high-pass), and persisting that would promote a
            # guess into the field whose whole meaning is "the manufacturer
            # published this" -- so the inferred case stores neither and a
            # reader of such a target still finds the limit exactly where it
            # has always been. The pair travels together because
            # ``normalise_driver_safety_fields`` refuses a slope with no
            # frequency to condition, and because a target holding one half
            # would resolve its own low limit by the inferred path and
            # disagree with itself.
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
        # Read off ``entry``, not ``visible``: the shape validator re-derives
        # this policy from the STORED target and compares it for equality, so
        # both sides must resolve the floor from the same three stamped fields.
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

    Every entry's ``severity`` and ``code`` — and, for a BLOCKER, its message
    too, because a blocker's message is mechanically derived from its own code
    and so cannot drift on its own.

    A WARNING's message is excluded, and that exclusion is the point. Warning
    prose is hand-written and interpolates the household's numbers, so putting
    it inside this check made **editing the copy a breaking change for every
    profile carrying that warning**: the stored text no longer matched the
    derived text, the profile read ``malformed``, and ``confirmed_and_current``
    flipped false on a box whose declared values had not changed by one digit.
    That was measured, not feared — re-evaluating profiles written one commit
    earlier flipped both warned cases (#2884 review round, item 4).

    What the check still guarantees is the half that was ever load-bearing: a
    warning cannot be dropped, invented, re-coded, or downgraded in severity
    without this mismatching, because all of that moves the severity/code set.
    Only the sentence can differ, and no gate reads the sentence — every
    protection decision reads the declared values. The fingerprint never
    covered ``issues`` at all (verified: it hashes
    ``artifact_schema_version, kind, topology_id, targets, research, authority,
    authorizes_playback``), so this loosens nothing the digest was holding.
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
    CODE cannot carry (see :func:`_target_low_limit_warnings`); blockers keep
    the mechanical code-to-prose rendering they have always had. Both live in
    one list because ``severity`` is what separates them and the schema has
    carried that field since it shipped -- a parallel ``warnings`` key would be
    a second place to look for the same kind of fact.
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
    over the values it just wrote, so the only two outcomes are ``incomplete``
    (the declared values carry blocking issues) and ``confirmed``. There is no
    separate operator confirm step and no confirmation to preserve across a
    rebuild -- which is why this takes neither a ``prior_profile`` nor a
    ``confirm`` flag.

    Why that is not a dropped safety gate: the confirmation bit never
    authorized playback (``authorizes_playback`` is unconditionally ``False``),
    and every physical protection -- the excitation ledger and its permitted
    bands, the limiters, the output ceiling, the commissioning level stops --
    reads the declared LIMITS, not the bit. What the bit actually gated was a
    second human acknowledgement of numbers a human had already typed -- which
    the crossover-accept seam could not supply for its own machine-measured
    write. That seam saved with ``confirm=False``, so it could only ever CARRY
    an existing confirmation forward; wherever there was nothing to carry (a
    prior profile already stale or malformed, or a driver detail edited between
    measurements) the machine's own write landed ``needs_confirmation`` and then
    refused the very measurement loop that produced the value. ``issues`` still
    blocks, so a garbage or half-declared profile is still fail-closed.
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
            # The stored literal is unchanged so profiles already on disk stay
            # canonical, and it stays true: every write of this declaration
            # originates from an operator action on a page that shows the values
            # -- the /sound/ save, or Apply on the crossover review screen that
            # picked the measured candidate. There is no headless writer.
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
    # ``needs_confirmation`` is no longer WRITTEN -- the builder emits only
    # ``incomplete`` or ``confirmed`` -- but stays readable so a box carrying a
    # profile saved before the confirm step was retired is not reported as
    # corrupt. ``evaluate_driver_safety_profile`` reads it as current.
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
                # Optional, not required: a profile stored before #2897 carries
                # neither, and it is still a sound declaration -- its
                # projections re-derive from its own protective high-pass by
                # the legacy path, so it stays confirmed rather than being
                # invalidated by a deploy. It simply has no published slope to
                # gate a pinned crossover with until the next /sound/ save.
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
        # is NOT re-derived below, and the split is what keeps a pre-#2897
        # profile readable: it carries neither owner field, so both sides of
        # this comparison omit them and agree -- while re-deriving it would
        # ADD a ``recommended_highpass_hz`` the stored target never had and
        # report every such profile stale on the deploy that shipped the field.
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
        # declared low limit (#2603). A stored profile whose bands and
        # protective high-pass disagree about where the driver stops is exactly
        # the drift this ruling collapsed; it is refused rather than read,
        # because reading it would pick one of two answers silently.
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

    Only reports a value that actually MOVED. A declaration whose typed
    high-pass already equals its derivation -- the ordinary case, including
    every profile whose low limit was inferred from that same filter -- reports
    nothing, so the disclosure stays a signal rather than a line on every save.
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
#: RETIRED.  ``crossover_search_band_hz`` declared where a driver "may be
#: crossed"; the 2026-08-22 owner ruling deleted it (#2870) because the only
#: bounds entitled to refuse a corner are the drivers' declared hard excitation
#: bands, which it did not narrow toward — it narrowed AWAY from them, refusing
#: corners both drivers admit.
#:
#: **Deliberately not** :data:`~._common.LEGACY_DROPPED_DRIVER_FIELDS`, and the
#: difference is the question each answers.  That set is "which retired keys may
#: a stored DRIVER RECORD still carry at a write gate" — tolerated and dropped,
#: so an old draft stays saveable.  This one is "which retired keys, found on a
#: stored PROFILE TARGET, mean the profile is stale-but-fixable rather than
#: corrupt" — reported, never dropped, because the profile is re-derived rather
#: than edited.  Their memberships differ too: a profile target never carried
#: ``horn_coverage_deg``.  **Retiring another per-driver field means deciding
#: for BOTH** — whether a stored record may carry it, and whether a stored
#: profile carrying it is fixable.
_RETIRED_TARGET_FIELDS = frozenset({"crossover_search_band_hz"})

#: A stored profile that is structurally fine except that it names a retired
#: field.  Reported under its own name rather than the generic schema-invalid
#: one, on :class:`DriverSafetyProfileStaleLowLimitError`'s rule: the household's
#: only question is "will saving fix it?", and here the answer is a plain yes.
#: No auto-migration is written for it — a rebuild re-derives every target from
#: the values the operator can see, and silently rewriting a confirmed safety
#: declaration behind their back is the wrong direction for a declaration whose
#: whole point is that a human made it.
DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON = "driver_safety_profile_retired_field"


def _retired_fields_present(profile: Mapping[str, Any]) -> bool:
    """Whether any stored target still carries a field this build retired.

    Total by construction, like :func:`_stale_low_limit_rebuild_issues` below:
    it runs inside an except branch whose contract is to REPORT rather than
    raise, so an unreadable target contributes ``False`` rather than an error.
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

    Same derivation and same issue vocabulary the rebuild itself uses, applied
    to the stored targets, so the two cannot disagree about whether confirming
    is possible. Total by construction: a target this cannot read contributes
    nothing rather than raising, because this runs inside an except branch
    whose whole contract is to REPORT instead of raise.
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
        # so a box carrying a pre-#2603 split declaration reports and waits
        # rather than crash-looping a daemon.
        #
        # The reasons carry MORE than the name, because the name alone cannot
        # answer the only question the household has: will saving fix it?
        # Deriving the low limit moves the hard band's lower edge up to it, and
        # that can leave an already-declared analysis window outside the hard
        # band it must nest in, or leave the derived limit implausible for the
        # declared driver type -- at which point the rebuild lands
        # ``incomplete``, and the save the copy just recommended changes
        # nothing. Appending the rebuild's own blocking issues lets /sound/
        # name the field to fix first, instead of sending the household round a
        # loop. (Until #2870 the commonest such blocker was a declared
        # crossover-search band pushed under its own hard band; that field is
        # gone, and the surviving relationships are the ones named above.)
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
        # saying "JTS could not read these limits" about one sends the
        # household looking for damage that is not there. Named separately for
        # the same reason the stale-low-limit case above is: the remedy is
        # specific (open /sound/setup/ and save the visible values again), and
        # a name is what lets /sound/ say so.
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
        # A profile written before the confirm step was retired, read under the
        # CURRENT definition of confirmed: the declared values are structurally
        # sound and bound to this hardware, which is the whole of what the word
        # means now. The only thing this artifact is missing is a second human
        # acknowledgement that no longer exists, so it reads as current here
        # rather than locking the measurement loop out of an already-deployed
        # box until somebody re-saves the same numbers. The next save collapses
        # the stored status; no migration pass is needed.
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
