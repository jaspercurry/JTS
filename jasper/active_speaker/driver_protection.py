# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-aware protection and level-cap policy.

Deterministic and side-effect free: whether a commissioning tone may be
considered for a driver role/style, what level cap that tone may reach, what a
driver's LOW LIMIT is, and which fields derive from it. It does not play audio,
write CamillaDSP state, or persist level changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._common import finite_float as _finite_float, issue as _issue
from .calibration_level import MAX_TEST_LEVEL_DBFS, MIN_TEST_LEVEL_DBFS

SCHEMA_VERSION = 1
DRIVER_PROTECTION_KIND = "jts_active_speaker_driver_protection"
DRIVER_PROTECTION_POLICY_VERSION = "driver_protection_auto_level_v1"

LOW_FREQUENCY_ROLES = frozenset({"woofer", "mid", "subwoofer"})
HIGH_FREQUENCY_ROLES = frozenset({"tweeter"})
#: The one role a ``full_range_passive`` (way-1) speaker declares: a single amp
#: channel covering the whole band.
FULL_RANGE_ROLES = frozenset({"full_range"})

_UNKNOWN_HF_STYLE = "unknown_high_frequency"
# Per-style high-pass figures. NOT a veto over sourced manufacturer data: a
# published minimum recommended crossover wins outright, including below the
# figure here (docs/active-speaker-tuning-layers-design.md decisions 8-9).
# Three jobs: the DEFAULT when a manufacturer publishes nothing, the anchor for
# ``driver_low_limit_plausibility_band_hz``, and the commissioning-tone gate's
# FALLBACK via ``tone_gate_low_limit``. Editing a number here therefore still
# moves an audible-test gate, but only for a driver with nothing declared.
_STYLE_HIGH_PASS_HZ = {
    "compression_driver": 2000.0,
    "horn_compression_driver": 2000.0,
    "dome_tweeter": 3000.0,
    "amt_tweeter": 3000.0,
    "planar_tweeter": 3500.0,
    "ribbon_tweeter": 5000.0,
    "supertweeter": 8000.0,
    _UNKNOWN_HF_STYLE: 5000.0,
}

#: Shared by the ``tweeter`` and ``full_range`` classes: -65 dBFS was sized for
#: a naked driver tone with no proven protective high-pass, and 100 ms is the
#: floor-test duration that figure was validated at. On the program-admission
#: path it is superseded by :func:`derive_hf_measurement_ceiling_dbfs`.
_HIGH_FREQUENCY_FLOOR_TEST_MS = 100
_HIGH_FREQUENCY_MAX_AUTO_LEVEL_DBFS = -65.0


@dataclass(frozen=True)
class DriverProtectionProfile:
    role: str
    role_class: str
    driver_style: str | None
    min_highpass_hz: float | None
    # ``None`` only for a ``full_range`` driver that declares no low edge; the
    # payload below refuses that by name rather than guessing a figure.
    floor_test_frequency_hz: float | None
    floor_test_duration_ms: int
    max_auto_level_dbfs: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "role_class": self.role_class,
            "driver_style": self.driver_style,
            "min_highpass_hz": self.min_highpass_hz,
            "floor_test_frequency_hz": self.floor_test_frequency_hz,
            "floor_test_duration_ms": self.floor_test_duration_ms,
            "max_auto_level_dbfs": self.max_auto_level_dbfs,
        }


def normalise_driver_role(role: Any) -> str:
    return str(role or "").strip().lower()


def normalise_driver_style(style: Any) -> str | None:
    if style is None:
        return None
    token = str(style or "").strip().lower().replace("-", "_").replace(" ", "_")
    return token or None


def driver_style_is_registered(driver_style: Any) -> bool:
    """Whether the per-style table actually DESCRIBES this driver style.

    ``False`` means the style fell through to the unknown-tweeter default, so
    any band or floor derived from it is a fallback. Several spellings land
    there (``"unspecified"``, a typo, a style from a newer build), which is why
    disclosing callers ask this rather than testing one sentinel. Normalises
    first, so the picker's ``"Dome Tweeter"`` counts as registered.
    """

    return normalise_driver_style(driver_style) in _STYLE_HIGH_PASS_HZ


def driver_protection_profile(
    role: Any,
    *,
    driver_style: Any = None,
    declared_floor_hz: Any = None,
) -> DriverProtectionProfile:
    """Return conservative commissioning bounds for one driver target.

    ``declared_floor_hz`` (:func:`driver_excitation_floor_hz`) is read only by
    the ``full_range`` class, which has no code figure of its own.
    """

    role_id = normalise_driver_role(role)
    style = normalise_driver_style(driver_style)
    if role_id in FULL_RANGE_ROLES:
        floor = _finite_float(declared_floor_hz)
        return DriverProtectionProfile(
            role=role_id,
            role_class="full_range",
            driver_style=style,
            min_highpass_hz=None,
            floor_test_frequency_hz=floor if floor is not None and floor > 0 else None,
            floor_test_duration_ms=_HIGH_FREQUENCY_FLOOR_TEST_MS,
            max_auto_level_dbfs=_HIGH_FREQUENCY_MAX_AUTO_LEVEL_DBFS,
        )
    if role_id in LOW_FREQUENCY_ROLES:
        if role_id == "subwoofer":
            frequency = 50.0
        elif role_id == "mid":
            frequency = 800.0
        else:
            frequency = 120.0
        return DriverProtectionProfile(
            role=role_id,
            role_class="low_frequency",
            driver_style=style,
            min_highpass_hz=None,
            floor_test_frequency_hz=frequency,
            floor_test_duration_ms=300,
            max_auto_level_dbfs=MAX_TEST_LEVEL_DBFS,
        )
    if role_id in HIGH_FREQUENCY_ROLES:
        hf_style = style or _UNKNOWN_HF_STYLE
        min_highpass = _STYLE_HIGH_PASS_HZ.get(hf_style, _STYLE_HIGH_PASS_HZ[_UNKNOWN_HF_STYLE])
        return DriverProtectionProfile(
            role=role_id,
            role_class="high_frequency",
            driver_style=hf_style,
            min_highpass_hz=min_highpass,
            floor_test_frequency_hz=max(min_highpass, 3000.0),
            floor_test_duration_ms=_HIGH_FREQUENCY_FLOOR_TEST_MS,
            # Superseded on the program-admission path by
            # ``derive_hf_measurement_ceiling_dbfs``.
            max_auto_level_dbfs=_HIGH_FREQUENCY_MAX_AUTO_LEVEL_DBFS,
        )
    return DriverProtectionProfile(
        role=role_id,
        role_class="unsupported",
        driver_style=style,
        min_highpass_hz=None,
        floor_test_frequency_hz=500.0,
        floor_test_duration_ms=300,
        max_auto_level_dbfs=MIN_TEST_LEVEL_DBFS,
    )


# --- HF measurement-ceiling derivation (two-invariant protection model) ------
#
# Driver protection is exactly two invariants, one owner each: wrong-frequency-
# range (the declared hard band plus the proven protective high-pass, owned
# elsewhere) and too-loud — ONE derived ceiling rather than stacked hedges. A
# code figure may only refuse on a named damage mechanism
# (docs/measurement-loop-doctrine.md §5), which is why no absolute dBFS hedge
# sits above the derivation.
#
# What bounds a high-frequency driver's measurement level: the derivation
# below, which admits it at the same ACOUSTIC level its low-frequency sibling
# is already admitted at, clamped by MAX_TEST_LEVEL_DBFS. A negative delta (a
# less-sensitive tweeter under a high-sensitivity pro woofer) is a legitimate
# configuration, not an error, and clamping at full scale is still safe by the
# same acoustic argument. Outside this module: the hard band, the preset's
# ``max_commissioning_level_db_spl``, and the leveling ramp's guards.
#
# Residual, named rather than hidden: declared sensitivities carry no
# plausibility validation, so a household that swaps the two rows empties the
# derivation for that box's composed tweeter level. Refusing on delta <= 0
# cannot separate that from the legitimate case — both clamp to
# MAX_TEST_LEVEL_DBFS — so validating the declaration against the preset's own
# ``sensitivity_db`` is the fix. Issue #2765.


def derive_hf_measurement_ceiling_dbfs(
    *,
    declared_lf_driver_cap_dbfs: float,
    sens_hf_db: float,
    sens_lf_db: float,
) -> float:
    """The sensitivity-referenced HF measurement ceiling (two-invariant model).

    The low-frequency driver's own declared cap corrected for the sensitivity
    delta between the two declared specs, bounded by :data:`MAX_TEST_LEVEL_DBFS`.
    Pure arithmetic: the caller owns picking valid inputs (a proven-protective-HP
    graph, an HF target with no level limit of its own, both sensitivities).
    """

    return min(
        declared_lf_driver_cap_dbfs - (sens_hf_db - sens_lf_db),
        MAX_TEST_LEVEL_DBFS,
    )


def _level_at_floor(level: float) -> bool:
    return level <= MIN_TEST_LEVEL_DBFS + 1e-6


def _band_highpass_hz(band_limit: Any) -> float | None:
    if not isinstance(band_limit, dict):
        return None
    if band_limit.get("type") not in {"highpass", "bandpass"}:
        return None
    return _finite_float(band_limit.get("highpass_hz"))


def declared_protection_highpass_floor_hz(driver: Any) -> float | None:
    """The declared protective high-pass floor carried by one driver payload.

    The strictest ``kind="highpass"`` ``cutoff_hz`` in this payload's own
    ``required_protection_filters``. It does NOT prove the value was frozen into
    a validated declaration — a staging payload can carry a research-only value
    — and believability is judged in ``driver_safety``, not here. Safe anyway
    for the two monotone consumers: the derived protection clamp is
    ``max(floor, multiplier x fc)`` and the load gate can only refuse more.
    The commissioning-tone gate is deliberately NOT monotone (a published floor
    below the class default admits a tone the class default refused).

    ``None`` means *no floor is declared* — never a floor of zero and never
    the class-default policy floor (ADR-0227 §1).
    """

    if not isinstance(driver, Mapping):
        return None
    filters = driver.get("required_protection_filters")
    if not isinstance(filters, list):
        return None
    floors: list[float] = []
    for item in filters:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "").strip().lower() != "highpass":
            continue
        cutoff = _finite_float(item.get("cutoff_hz"))
        if cutoff is not None and cutoff > 0:
            floors.append(cutoff)
    return max(floors) if floors else None


def declared_protection_lowpass_ceiling_hz(driver: Any) -> float | None:
    """The declared protective low-pass ceiling carried by one driver payload.

    The mirror of :func:`declared_protection_highpass_floor_hz`. Strictest wins,
    so ``min`` here where the floor takes ``max`` — both directions tighten.
    ``None`` means *no ceiling is declared*, never a guessed default.
    """

    if not isinstance(driver, Mapping):
        return None
    filters = driver.get("required_protection_filters")
    if not isinstance(filters, list):
        return None
    ceilings: list[float] = []
    for item in filters:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "").strip().lower() != "lowpass":
            continue
        cutoff = _finite_float(item.get("cutoff_hz"))
        if cutoff is not None and cutoff > 0:
            ceilings.append(cutoff)
    return min(ceilings) if ceilings else None


def protection_highpass_floor_satisfied(
    *,
    highpass_hz: float | None,
    floor_hz: float | None,
) -> bool:
    """Whether a high-pass corner honours a declared protection floor.

    The single comparison rule all four protection-floor consumers share. An
    absent floor is satisfied; an absent high-pass against a real floor is not.
    """

    if floor_hz is None:
        return True
    return highpass_hz is not None and highpass_hz >= floor_hz


def format_protection_hz(value: float) -> str:
    """Render one protection-floor frequency for an operator-facing message.

    Shared, so a non-integer declared floor cannot render three different ways.
    """

    return f"{float(value):g} Hz"


# --- A driver's low limit: one declared owner, every consumer derives -------
#
# A driver's bottom allowed frequency IS the manufacturer's minimum recommended
# crossover frequency, entered once at component entry as
# ``recommended_highpass_hz`` plus its optional
# ``recommended_highpass_slope_db_per_octave``. That pair is the OWNER
# (docs/active-speaker-tuning-layers-design.md decisions 8-9). Derived from it:
#
#   required_protection_filters[highpass].cutoff_hz  = the owner's frequency
#   required_protection_filters[highpass].minimum_slope_db_per_octave
#                                                    = the owner's slope raised
#                                                      to the commissioning
#                                                      floor below
#   hard_excitation_band_hz[0]                       = the owner's frequency
#   measurement_band_hz[0]                           = max(published, owner)
#
# ``measurement_band_hz`` stays a SEPARATE published fact (the datasheet's
# response range) with only its lower edge clamped up into the allowed band.
# ``do_not_test_below_hz`` is retired: accepted by the schemas so old drafts
# load, but no policy, band, filter or gate derives from it.

#: Commissioning floor on the protective high-pass THIS BUILD EMITS, dB/octave.
#: A code number, so it may only PREFILL (raise a published slope in
#: :attr:`DriverLowLimit.derived_protection_slope_db_per_octave`, which
#: :func:`apply_driver_low_limit` stamps and ``graph_safety`` later proves) and
#: DISCLOSE (``crossover_preview``'s ``tweeter_slope_below_recommended_floor``).
#: It may NEVER refuse a crossover a household pinned: the manufacturer's
#: published condition, :attr:`DriverLowLimit.slope_db_per_octave`, is the only
#: slope entitled to refuse a corner.
PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE = 24.0

#: How far a DECLARED low limit may sit from its style's default before it stops
#: being believed on sight. A factor of 4 admits every real datasheet the field
#: publishes — large-format compression drivers publish recommended crossovers
#: as low as 500-800 Hz and small ones as high as 8 kHz — while still catching a
#: transposed digit or a woofer's number pasted into a tweeter row.
#:
#: What "stops being believed" MEANS depends on the author: a research reply
#: outside the band is refused at intake, a human-typed value saves under a
#: warning. ``driver_safety`` owns that split.
LOW_LIMIT_PLAUSIBILITY_FACTOR = 4.0

LOW_LIMIT_DECLARED = "declared"
LOW_LIMIT_LEGACY_PROTECTION_FILTER = "legacy_protection_filter"
LOW_LIMIT_STYLE_DEFAULT = "style_default"

#: One operator-facing phrase per low-limit provenance. Every surface that
#: PRINTS a resolved low limit renders it through :func:`format_low_limit`, so
#: an unlabelled class-table figure cannot read as a second floor.
LOW_LIMIT_PROVENANCE_LABELS = {
    LOW_LIMIT_DECLARED: "manufacturer declared",
    LOW_LIMIT_LEGACY_PROTECTION_FILTER: (
        "inferred from a stored protective high-pass"
    ),
    LOW_LIMIT_STYLE_DEFAULT: "class fallback; nothing declared",
}


@dataclass(frozen=True)
class DriverLowLimit:
    """One driver's bottom allowed frequency, and where the number came from.

    ``slope_db_per_octave`` is the manufacturer's published slope CONDITION,
    ``None`` when the maker prints none and never defaulted — it is the only
    slope entitled to refuse anything. The build's own commissioning figure is
    :attr:`derived_protection_slope_db_per_octave`, named DERIVED so a call site
    cannot mistake which of the two it holds.
    """

    frequency_hz: float
    slope_db_per_octave: float | None
    provenance: str
    rationale: str

    @property
    def derived_protection_slope_db_per_octave(self) -> float:
        """The slope the protective high-pass this build EMITS must meet.

        A prefill, never a bound on what the household may cross at.
        """

        published = self.slope_db_per_octave
        return max(
            float(published) if published is not None else 0.0,
            PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
        )


def _declared_highpass_filter(driver: Mapping[str, Any]) -> Mapping[str, Any] | None:
    filters = driver.get("required_protection_filters")
    if not isinstance(filters, list):
        return None
    best: Mapping[str, Any] | None = None
    for item in filters:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "").strip().lower() != "highpass":
            continue
        cutoff = _finite_float(item.get("cutoff_hz"))
        if cutoff is None or cutoff <= 0:
            continue
        if best is None or cutoff > float(best["cutoff_hz"]):
            best = item
    return best


def driver_low_limit_plausibility_band_hz(
    role: Any,
    *,
    driver_style: Any = None,
) -> tuple[float, float] | None:
    """The band a declared low limit must land inside, or ``None`` if no anchor.

    Anchored on the style default; roles with no style entry (every
    low-frequency role) get no bound rather than an invented one.
    """

    anchor = driver_protection_profile(role, driver_style=driver_style).min_highpass_hz
    if anchor is None:
        return None
    return (
        anchor / LOW_LIMIT_PLAUSIBILITY_FACTOR,
        anchor * LOW_LIMIT_PLAUSIBILITY_FACTOR,
    )


def driver_low_limit_plausible(
    frequency_hz: float,
    *,
    role: Any,
    driver_style: Any = None,
) -> bool:
    """Whether a declared low limit is believable for this driver style.

    Inclusive at both edges. A garbage catcher, never a judgement about whether
    a published number is wise — what protects the driver at a low declared
    figure is the derived protective high-pass, the absolute
    ``graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ`` corner, the
    ``path_safety`` load gate and the excitation ceilings, none of which this
    factor touches. It answers only *believable?*; ``driver_safety`` owns what
    happens next.
    """

    band = driver_low_limit_plausibility_band_hz(role, driver_style=driver_style)
    if band is None:
        return True
    return band[0] <= float(frequency_hz) <= band[1]


def resolve_driver_low_limit(
    driver: Any,
    *,
    role: Any,
    driver_style: Any = None,
) -> DriverLowLimit | None:
    """Resolve one driver's low limit from its declaration.

    Order, and why:

    1. The OWNER (``recommended_highpass_hz``): a sourced manufacturer figure
       wins outright, including below the style default.
    2. A stored ``required_protection_filters`` high-pass, when no owner is
       declared — the backwards-compatible read, labelled as inferred and
       resolving to the STRICTER number so a deployed box never loosens.
    3. The style default, labelled as a code default; see
       :func:`apply_driver_low_limit` for the one thing it may not do.

    ``None`` means no low limit at all (no owner, no stored high-pass, no style
    anchor) — "unchanged behaviour", never a floor of zero.
    """

    if not isinstance(driver, Mapping):
        return None
    declared = _finite_float(driver.get("recommended_highpass_hz"))
    if declared is not None and declared > 0:
        return DriverLowLimit(
            frequency_hz=declared,
            slope_db_per_octave=_finite_float(
                driver.get("recommended_highpass_slope_db_per_octave")
            ),
            provenance=LOW_LIMIT_DECLARED,
            rationale=(
                "the manufacturer's declared minimum recommended crossover "
                "frequency"
            ),
        )
    legacy = _declared_highpass_filter(driver)
    if legacy is not None:
        return DriverLowLimit(
            frequency_hz=float(legacy["cutoff_hz"]),
            slope_db_per_octave=_finite_float(
                legacy.get("minimum_slope_db_per_octave")
            ),
            provenance=LOW_LIMIT_LEGACY_PROTECTION_FILTER,
            rationale=(
                "inferred from a stored protective high-pass requirement; no "
                "minimum recommended crossover frequency is declared"
            ),
        )
    anchor = driver_protection_profile(role, driver_style=driver_style).min_highpass_hz
    if anchor is None:
        return None
    return DriverLowLimit(
        frequency_hz=anchor,
        slope_db_per_octave=None,
        provenance=LOW_LIMIT_STYLE_DEFAULT,
        rationale=(
            "code default for this driver style; the manufacturer publishes "
            "no minimum recommended crossover frequency"
        ),
    )


def format_low_limit_provenance(provenance: Any) -> str:
    """Render one low-limit provenance as the phrase an operator reads.

    An unrecognised token renders as itself rather than as a guessed phrase.
    """

    token = str(provenance or "")
    return LOW_LIMIT_PROVENANCE_LABELS.get(token, token or "unknown provenance")


def format_low_limit(limit: DriverLowLimit) -> str:
    """Render one resolved low limit as ``"1600 Hz (manufacturer declared)"``.

    The single renderer for the frequency AND its provenance, produced together
    so no surface can print the number unlabelled.
    """

    return (
        f"{format_protection_hz(limit.frequency_hz)} "
        f"({format_low_limit_provenance(limit.provenance)})"
    )


def tone_gate_low_limit(
    role: Any,
    *,
    driver_style: Any = None,
    declared_low_limit_hz: Any = None,
) -> DriverLowLimit | None:
    """The floor the commissioning-tone gate compares a staged high-pass against.

    ``declared_low_limit_hz`` is this driver's OWN declared low limit when the
    caller knows it; ``None`` leaves the style default standing as the FALLBACK.
    The declared frequency is handed to :func:`resolve_driver_low_limit` as the
    OWNER, so the gate, the profile-confirm path and the preview cannot disagree
    about which number wins. A ``None`` return means "no floor at all".
    """

    return resolve_driver_low_limit(
        {"recommended_highpass_hz": declared_low_limit_hz},
        role=role,
        driver_style=driver_style,
    )


def _band_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    low = _finite_float(value[0])
    high = _finite_float(value[1])
    if low is None or high is None:
        return None
    return low, high


def driver_excitation_floor_hz(driver: Any) -> float | None:
    """The declared low edge below which this driver may not be excited.

    The declared owner, then a stored protective high-pass, then the declared
    ``measurement_band_hz`` low edge. The style default is deliberately NOT
    reached: this bounds a SWEEP, and the class table is a tone-gate fallback,
    not a frequency a driver may be driven to. ``None`` means undeclared.
    """

    if not isinstance(driver, Mapping):
        return None
    declared = _finite_float(driver.get("recommended_highpass_hz"))
    if declared is None or declared <= 0:
        declared = declared_protection_highpass_floor_hz(driver)
    if declared is not None and declared > 0:
        return declared
    band = _band_pair(driver.get("measurement_band_hz"))
    return band[0] if band is not None and band[0] > 0 else None


def apply_driver_low_limit(
    driver: Any,
    *,
    role: Any,
    driver_style: Any = None,
) -> dict[str, Any]:
    """Return ``driver`` with every low-limit-derived field recomputed.

    Idempotent, which is what lets the safety profile's shape validator
    re-derive and refuse a hand-edited artifact whose derived fields no longer
    match its own declared owner. A band whose upper edge sits at or below the
    low limit is left ALONE rather than stamped into an inverted range; the
    ``..._outside_hard_band`` vocabulary is what names that.

    **A code default may UNBLOCK; it may never REFUSE** (ADR-0227 §1), so a
    ``style_default`` low limit is NOT stamped: the stamped high-pass feeds the
    preset, the derived protection clamp and the ``path_safety`` load gate, and
    inventing one would refuse a design the household chose on a number this
    module made up.
    """

    if not isinstance(driver, Mapping):
        return {}
    out = dict(driver)
    limit = resolve_driver_low_limit(driver, role=role, driver_style=driver_style)
    if limit is None or limit.provenance == LOW_LIMIT_STYLE_DEFAULT:
        return out
    frequency = limit.frequency_hz
    out["recommended_highpass_hz"] = frequency
    if limit.slope_db_per_octave is None:
        out.pop("recommended_highpass_slope_db_per_octave", None)
    else:
        out["recommended_highpass_slope_db_per_octave"] = limit.slope_db_per_octave
    filters = [
        dict(item)
        for item in (driver.get("required_protection_filters") or [])
        if isinstance(item, Mapping)
        and str(item.get("kind") or "").strip().lower() != "highpass"
    ]
    filters.append(
        {
            "kind": "highpass",
            "cutoff_hz": frequency,
            "minimum_slope_db_per_octave": (
                limit.derived_protection_slope_db_per_octave
            ),
            "family_or_equivalent": "equivalent_or_steeper",
        }
    )
    out["required_protection_filters"] = sorted(
        filters, key=lambda item: str(item["kind"])
    )
    hard = _band_pair(driver.get("hard_excitation_band_hz"))
    if hard is not None and frequency < hard[1]:
        out["hard_excitation_band_hz"] = [frequency, hard[1]]
    measurement = _band_pair(driver.get("measurement_band_hz"))
    if measurement is not None and frequency < measurement[1]:
        out["measurement_band_hz"] = [max(measurement[0], frequency), measurement[1]]
    return out


def _highpass_satisfied(
    *,
    low_limit: DriverLowLimit | None,
    band_limit: Any,
) -> bool:
    return protection_highpass_floor_satisfied(
        highpass_hz=_band_highpass_hz(band_limit),
        floor_hz=low_limit.frequency_hz if low_limit is not None else None,
    )


def driver_protection_payload(
    role: Any,
    *,
    driver_style: Any = None,
    protection_status: Any = None,
    band_limit: Any = None,
    declared_low_limit_hz: Any = None,
    declared_floor_hz: Any = None,
) -> dict[str, Any]:
    """Return the protection envelope for one target.

    ``audio_allowed`` means the driver role/style has enough deterministic
    protection evidence to be considered by higher-level readiness gates. It
    does not bypass safe-session, backend, floor-confirmation, or Stop checks.

    ``declared_low_limit_hz`` is this driver's own declared low limit when the
    caller knows it; see :func:`tone_gate_low_limit` for what absent means.
    ``declared_floor_hz`` is :func:`driver_excitation_floor_hz`'s wider answer,
    which only the ``full_range`` class reads.
    """

    profile = driver_protection_profile(
        role, driver_style=driver_style, declared_floor_hz=declared_floor_hz
    )
    low_limit = tone_gate_low_limit(
        role,
        driver_style=driver_style,
        declared_low_limit_hz=declared_low_limit_hz,
    )
    staged_highpass_hz = _band_highpass_hz(band_limit)
    highpass_ok = _highpass_satisfied(low_limit=low_limit, band_limit=band_limit)
    status = str(protection_status or "").strip().lower()
    issues: list[dict[str, str]] = []
    if profile.role_class == "unsupported":
        issues.append(_issue(
            "blocker",
            "driver_role_not_supported",
            "this driver role is not enabled for active-speaker audible tests",
        ))
    if profile.role_class == "full_range" and profile.floor_test_frequency_hz is None:
        issues.append(_issue(
            "blocker",
            "full_range_low_edge_undeclared",
            "a full-range driver has no crossover and no class figure to stand "
            "in for one, so it may not be excited until its declaration names a "
            "recommended crossover frequency or a measurement band",
        ))
    if profile.role_class == "high_frequency":
        if status not in {"present", "software_guard_requested"}:
            issues.append(_issue(
                "blocker",
                "high_frequency_protection_missing",
                "high-frequency drivers require marked physical protection or software-guarded bring-up",
            ))
        # Absent and below-floor are different facts with different fixes, so
        # they get different codes, both naming the floor and its provenance.
        # ``low_limit is not None`` narrows for the renderer rather than
        # guarding an unreachable state.
        if not highpass_ok and low_limit is not None:
            if staged_highpass_hz is None:
                issues.append(_issue(
                    "blocker",
                    "high_frequency_highpass_missing",
                    (
                        "high-frequency driver tone requires a protective "
                        "high-pass band limit at or above this driver's low "
                        f"limit of {format_low_limit(low_limit)}; none is staged"
                    ),
                ))
            else:
                issues.append(_issue(
                    "blocker",
                    "high_frequency_highpass_below_low_limit",
                    (
                        "the staged protective high-pass "
                        f"{format_protection_hz(staged_highpass_hz)} sits below "
                        "this driver's low limit of "
                        f"{format_low_limit(low_limit)}"
                    ),
                ))
    envelope = {
        # The raw class figure is deliberately NOT republished beside the
        # resolved floor: two floats one key apart with only one labelled is an
        # ambiguity. ``low_limit_hz`` carries the class number when the class
        # number is operative, with ``low_limit_provenance`` saying so.
        key: value
        for key, value in profile.to_dict().items()
        if key != "min_highpass_hz"
    }
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DRIVER_PROTECTION_KIND,
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        **envelope,
        "low_limit_hz": low_limit.frequency_hz if low_limit is not None else None,
        "low_limit_provenance": low_limit.provenance if low_limit is not None else None,
        "low_limit_summary": (
            format_low_limit(low_limit) if low_limit is not None else None
        ),
        "protection_status": status or None,
        "band_limit_highpass_ok": highpass_ok,
        "audio_allowed": not issues and profile.role_class in {
            "low_frequency",
            "high_frequency",
            "full_range",
        },
        "issues": issues,
    }
