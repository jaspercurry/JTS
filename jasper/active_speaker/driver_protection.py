# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver-aware protection and closed-loop level policy.

This module is intentionally deterministic and side-effect free. It decides
whether a commissioning tone may be considered for a driver role/style, how a
mic observation should move the separate commissioning test level, and — since
the 2026-08-17 ruling — what a driver's LOW LIMIT is and which fields derive
from it. It does not play audio, write CamillaDSP state, or persist level
changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._common import finite_float as _finite_float, issue as _issue
from .calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MAX_TEST_LEVEL_DBFS,
    MIC_USABLE_MAX_DBFS,
    MIC_USABLE_MIN_DBFS,
    MIN_TEST_LEVEL_DBFS,
    TEST_LEVEL_STEP_DB,
    clamp_test_level_dbfs,
    classify_mic_meter,
)

SCHEMA_VERSION = 1
DRIVER_PROTECTION_KIND = "jts_active_speaker_driver_protection"
AUTO_LEVEL_DECISION_KIND = "jts_active_speaker_auto_level_decision"
DRIVER_PROTECTION_POLICY_VERSION = "driver_protection_auto_level_v1"

LOW_FREQUENCY_ROLES = frozenset({"woofer", "mid", "subwoofer"})
HIGH_FREQUENCY_ROLES = frozenset({"tweeter"})
SUPPORTED_AUDIBLE_ROLES = LOW_FREQUENCY_ROLES | HIGH_FREQUENCY_ROLES

_UNKNOWN_HF_STYLE = "unknown_high_frequency"
# Per-style high-pass figures. Since the 2026-08-17 ruling (decisions 8-9,
# issue #2603) this table is NOT a veto over sourced manufacturer data: a
# published minimum recommended crossover wins outright, including below the
# figure here. Its two remaining jobs are (1) the DEFAULT answer when a
# manufacturer publishes nothing, and (2) the anchor for the plausibility band
# that refuses a declared low limit as garbage -- see
# ``resolve_driver_low_limit`` and ``driver_low_limit_plausibility_band_hz``.
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


@dataclass(frozen=True)
class DriverProtectionProfile:
    role: str
    role_class: str
    driver_style: str | None
    min_highpass_hz: float | None
    floor_test_frequency_hz: float
    floor_test_duration_ms: int
    max_auto_level_dbfs: float
    requires_floor_confirmation_above_floor: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "role_class": self.role_class,
            "driver_style": self.driver_style,
            "min_highpass_hz": self.min_highpass_hz,
            "floor_test_frequency_hz": self.floor_test_frequency_hz,
            "floor_test_duration_ms": self.floor_test_duration_ms,
            "max_auto_level_dbfs": self.max_auto_level_dbfs,
            "requires_floor_confirmation_above_floor": (
                self.requires_floor_confirmation_above_floor
            ),
        }


def normalise_driver_role(role: Any) -> str:
    return str(role or "").strip().lower()


def normalise_driver_style(style: Any) -> str | None:
    if style is None:
        return None
    token = str(style or "").strip().lower().replace("-", "_").replace(" ", "_")
    return token or None


def driver_protection_profile(
    role: Any,
    *,
    driver_style: Any = None,
) -> DriverProtectionProfile:
    """Return conservative commissioning bounds for one driver target."""

    role_id = normalise_driver_role(role)
    style = normalise_driver_style(driver_style)
    if role_id in LOW_FREQUENCY_ROLES:
        if role_id == "subwoofer":
            frequency = 50.0
            duration_ms = 300
        elif role_id == "mid":
            frequency = 800.0
            duration_ms = 300
        else:
            frequency = 120.0
            duration_ms = 300
        return DriverProtectionProfile(
            role=role_id,
            role_class="low_frequency",
            driver_style=style,
            min_highpass_hz=None,
            floor_test_frequency_hz=frequency,
            floor_test_duration_ms=duration_ms,
            max_auto_level_dbfs=MAX_TEST_LEVEL_DBFS,
            requires_floor_confirmation_above_floor=True,
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
            floor_test_duration_ms=100,
            # -65 dBFS was sized for a NAKED driver tone with no proven
            # protective HP. On the program-admission path (a graph that
            # carries the crossover HP by construction) this is superseded by
            # the sensitivity-derived ceiling below -- see
            # ``derive_hf_measurement_ceiling_dbfs`` and
            # ``jasper.active_speaker.excitation_safety_plan.resolve_driver_excitation_ceilings``.
            max_auto_level_dbfs=-65.0,
            requires_floor_confirmation_above_floor=True,
        )
    return DriverProtectionProfile(
        role=role_id,
        role_class="unsupported",
        driver_style=style,
        min_highpass_hz=None,
        floor_test_frequency_hz=500.0,
        floor_test_duration_ms=300,
        max_auto_level_dbfs=MIN_TEST_LEVEL_DBFS,
        requires_floor_confirmation_above_floor=True,
    )


# --- HF measurement-ceiling derivation (two-invariant protection model) ------
#
# Operator ruling (2026-07-19): driver protection is exactly two invariants,
# one owner each -- (1) wrong-frequency-range: the declared hard band plus the
# proven protective high-pass (unrelated to this section, untouched, airtight);
# (2) too-loud: ONE derived ceiling instead of stacked hedges. The -65 dBFS
# ``max_auto_level_dbfs`` above was sized for a naked driver tone with no
# proven HP. On the program-admission path -- a graph that carries the
# driver's crossover high-pass by construction -- it pins a compression-driver
# tweeter far below its optimal measurement level: JTS3 hardware measurement
# (2026-07-18, run 5) showed the tweeter's -65 dBFS cap reading near-inaudible
# at 27 dB in-band SNR, while the woofer's comfortable pilots ran -26 dBFS
# effective -- a 25.2 dB sensitivity delta (B&C DE250-8 ~108.5 dB vs Dayton
# Epique E150HE-44 ~83.3 dB) the class default never accounted for.
HF_MEASUREMENT_ABS_CEILING_DBFS = -35.0  # provisional pending W6 bench validation; a hearing-safety bound, not derived from any driver's declared data


def derive_hf_measurement_ceiling_dbfs(
    *,
    declared_lf_driver_cap_dbfs: float,
    sens_hf_db: float,
    sens_lf_db: float,
) -> float:
    """The sensitivity-referenced HF measurement ceiling (two-invariant model).

    Same acoustic ceiling CLASS as the low-frequency driver's own declared
    cap, corrected for the sensitivity delta between the two declared driver
    specs, bounded by the absolute hearing-safety ceiling. Pure arithmetic --
    the caller owns picking valid inputs (a proven-protective-HP graph, an
    unsuperseded class-default seed, and both drivers' declared sensitivities).
    """

    return min(
        declared_lf_driver_cap_dbfs - (sens_hf_db - sens_lf_db),
        HF_MEASUREMENT_ABS_CEILING_DBFS,
    )


def _current_level(calibration_level: dict[str, Any] | None) -> float:
    if not isinstance(calibration_level, dict):
        return MIN_TEST_LEVEL_DBFS
    test_signal = (
        calibration_level.get("test_signal")
        if isinstance(calibration_level.get("test_signal"), dict)
        else {}
    )
    return clamp_test_level_dbfs(test_signal.get("requested_level_dbfs"))


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

    Reads exactly one thing: the strictest ``kind="highpass"`` ``cutoff_hz`` in
    this payload's own ``required_protection_filters``. It does **not** prove
    the value was confirmed, and callers must not read it as such. On the
    staging path the payload comes from ``crossover_preview``'s role-keyed
    research + manual-settings merge, which can carry a research-only value
    that no confirmation gate has seen.

    Confirmation is validated elsewhere and stays there:
    ``driver_safety._target_issues`` refuses a *visible* declaration below this
    module's ``min_highpass_hz`` code policy (``<role>:highpass_below_code_policy``),
    and ``build_driver_safety_profile`` raises rather than confirm a profile
    carrying that issue. So a confirmed declaration is at or above policy — but
    that is a property of the confirmed profile, not of this read.

    An unvalidated floor arriving here can only ever *tighten*: the derived
    protection clamp is ``max(floor, multiplier x fc)``, so a floor can raise
    the protective corner and never lower it, and the load gate can only refuse
    more than it did before. That is why this reader stays permissive about
    provenance while the clamp and the gate stay monotone.

    ``None`` means *no floor is declared* — never a guessed default. Consumers
    must treat that as "unchanged behaviour", not as "floor of zero" and not as
    an invitation to substitute the class-default policy floor: inventing a
    floor where the operator declared none is exactly the nanny behaviour the
    2026-08-14 never-nanny ruling excludes.
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


def protection_highpass_floor_satisfied(
    *,
    highpass_hz: float | None,
    floor_hz: float | None,
) -> bool:
    """Whether a high-pass corner honours a declared protection floor.

    The single comparison rule every protection-floor consumer shares (the
    derived-protection clamp, the crossover-preview disclosure, and the
    path-safety load gate), so the three surfaces cannot drift apart on the
    boundary case. An absent floor is satisfied; an absent high-pass against a
    real floor is not.
    """

    if floor_hz is None:
        return True
    return highpass_hz is not None and highpass_hz >= floor_hz


def format_protection_hz(value: float) -> str:
    """Render one protection-floor frequency for an operator-facing message.

    Shared by the crossover-preview disclosure and the path-safety refusal so a
    non-integer declared floor cannot render as two different numbers on the
    two surfaces a household compares.
    """

    return f"{float(value):g} Hz"


# --- A driver's low limit: one declared owner, every consumer derives -------
#
# Owner ruling, 2026-08-17 (docs/active-speaker-tuning-layers-design.md
# decisions 8 and 9; issue #2603). A driver's bottom allowed frequency IS the
# manufacturer's minimum recommended crossover frequency, carrying whatever
# slope condition the manufacturer attaches to it. It is entered ONCE, at
# component entry, as ``recommended_highpass_hz`` plus its optional
# ``recommended_highpass_slope_db_per_octave``. That pair is the OWNER.
#
# Everything the same fact used to be co-declared as is DERIVED here, by named
# code, with the rationale carried on the result rather than left implicit:
#
#   required_protection_filters[highpass].cutoff_hz  = the owner's frequency
#   required_protection_filters[highpass].minimum_slope_db_per_octave
#                                                    = the owner's slope raised
#                                                      to the commissioning
#                                                      floor below
#   hard_excitation_band_hz[0]                       = the owner's frequency
#   measurement_band_hz[0]                           = max(published, owner)
#
# ``do_not_test_below_hz`` is RETIRED rather than derived. It was a second,
# optional declaration of this same line, and its only consumer was a
# crossover-preview blocker. Collapsed onto the owner it would have made that
# blocker fire on exactly the condition #2491 deliberately routes elsewhere --
# preview DISCLOSES a corner below the declared floor, ``path_safety`` REFUSES
# it at load -- and blocking at preview would have made the load gate
# unreachable. Retiring it is also strictly safer than what it replaced: the
# disclosure and the load gate now fire from one always-derived number,
# where the blocker only fired when a separate optional field happened to be
# declared (the #2132 fail-open). The key is still ACCEPTED by the schemas so
# drafts written before this load unchanged; nothing writes or reads it.
#
# ``measurement_band_hz`` itself stays a SEPARATE published fact -- the
# datasheet's frequency-response range -- and only has its lower edge clamped
# up into the allowed band, because an analysis window cannot honestly extend
# below the frequency the driver may not be excited under. For the B&C DE250
# the published range is 1.0-18.0 kHz while the published minimum crossover is
# 1.6 kHz, so the stored window is [1600, 18000] and the two facts stay
# distinguishable.
#
# The slope split is decision 9 implemented literally: the DECLARATION carries
# what the manufacturer printed ("12 dB/oct. or higher slope high-pass filter"
# for the DE250), and the commissioning safety margin is computed HERE, named,
# rather than smuggled into a datasheet field as though the manufacturer had
# published it.

#: Commissioning floor on a derived protective high-pass slope, dB/octave. The
#: manufacturer's published minimum is the FLOOR of what the driver tolerates;
#: this build has always commissioned at 24 dB/octave or steeper (the research
#: ask said so, and ``crossover_preview`` warns below it), so the derived
#: REQUIREMENT is ``max(published, this)``. Raising a published 12 dB/oct to 24
#: cannot hurt the driver -- a steeper filter passes strictly less energy below
#: the corner -- and it keeps every already-emitted graph legal.
PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE = 24.0

#: How far a DECLARED low limit may sit from its style's default before it is
#: refused as garbage rather than believed. The style table is no longer a veto
#: over sourced manufacturer data (a published 1.6 kHz for a compression driver
#: must win over the 2 kHz class default); it is a plausibility anchor. A
#: factor of 4 admits every real datasheet the field publishes -- large-format
#: compression drivers publish recommended crossovers as low as 500-800 Hz and
#: small ones as high as 8 kHz, both inside the compression-driver band -- while
#: still catching a transposed digit or a woofer's number pasted into a tweeter
#: row. One factor rather than a per-style band on purpose: the anchor is
#: already per-style, and a second table would be a second thing to keep in
#: sync (including its JS mirror) for a bound nothing has earned.
LOW_LIMIT_PLAUSIBILITY_FACTOR = 4.0

LOW_LIMIT_DECLARED = "declared"
LOW_LIMIT_LEGACY_PROTECTION_FILTER = "legacy_protection_filter"
LOW_LIMIT_STYLE_DEFAULT = "style_default"


@dataclass(frozen=True)
class DriverLowLimit:
    """One driver's bottom allowed frequency, and where the number came from.

    ``frequency_hz`` is the low limit itself. ``slope_db_per_octave`` is the
    manufacturer's published slope CONDITION and is ``None`` when the maker
    prints none (BMS's 4590 is a real example) -- it is deliberately NOT
    defaulted here, because a code default wearing a datasheet's clothes is the
    exact failure decision 9 was written against. The derived commissioning
    requirement is :attr:`protection_slope_db_per_octave`.
    """

    frequency_hz: float
    slope_db_per_octave: float | None
    provenance: str
    rationale: str

    @property
    def protection_slope_db_per_octave(self) -> float:
        """The slope the emitted protective high-pass must meet, dB/octave."""

        published = self.slope_db_per_octave
        return max(
            float(published) if published is not None else 0.0,
            PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frequency_hz": self.frequency_hz,
            "slope_db_per_octave": self.slope_db_per_octave,
            "protection_slope_db_per_octave": self.protection_slope_db_per_octave,
            "provenance": self.provenance,
            "rationale": self.rationale,
        }


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

    Anchored on the style default. Roles with no style entry (every
    low-frequency role) have no anchor and therefore no plausibility bound --
    inventing one we cannot justify is the nanny behaviour the 2026-08-14
    ruling excludes.
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
    a published number is wise: what actually protects the driver at a low
    declared figure is the derived protective high-pass sitting AT that
    frequency (proved in the emitted graph by ``graph_safety``), the absolute
    corner floor that module owns, the commissioning high-pass at a multiple of
    the crossover corner, the ``path_safety`` load gate, and the excitation
    level ceilings -- none of which this factor touches.
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

    1. The OWNER (``recommended_highpass_hz``). A sourced manufacturer figure
       wins outright, including below the style default -- that is the whole
       point of the 2026-08-17 ruling.
    2. A stored ``required_protection_filters`` high-pass, when no owner is
       declared. This is the backwards-compatible read for drafts and profiles
       written before the owner existed; it is labelled as inferred, never as a
       datasheet fact, and it resolves to the STRICTER of the two numbers a
       legacy artifact carries, so an already-deployed box never loosens.
    3. The style default, when the manufacturer publishes nothing at all.
       ``absent`` is a legitimate research answer (decision 9), so this path is
       ordinary rather than exceptional -- but it is labelled as a code
       default, never as published data, and see
       :func:`apply_driver_low_limit` for the one thing it may not do.

    ``None`` means the driver has no low limit at all: no owner, no stored
    high-pass requirement, and no style anchor (every low-frequency role).
    Consumers must read that as "unchanged behaviour", never as a floor of
    zero.
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


def _band_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    low = _finite_float(value[0])
    high = _finite_float(value[1])
    if low is None or high is None:
        return None
    return low, high


def apply_driver_low_limit(
    driver: Any,
    *,
    role: Any,
    driver_style: Any = None,
) -> dict[str, Any]:
    """Return ``driver`` with every low-limit-derived field recomputed.

    The projection half of the one-owner rule: the caller supplies a driver
    declaration, this returns the same declaration with the derived fields
    stamped from :func:`resolve_driver_low_limit`. Idempotent -- stamping an
    already-stamped payload changes nothing, which is what lets the safety
    profile's shape validator re-derive and refuse a hand-edited artifact whose
    derived fields no longer match its own declared owner.

    A band whose upper edge sits at or below the low limit is left ALONE rather
    than stamped into an inverted range: that declaration is broken, and the
    existing ``<role>:highpass_cutoff_outside_hard_band`` /
    ``measurement_band_outside_hard_band`` vocabulary is what names it.

    **A code default may UNBLOCK; it may never REFUSE.** A ``style_default``
    low limit is deliberately NOT stamped. The stamped
    ``required_protection_filters`` high-pass is what
    :func:`declared_protection_highpass_floor_hz` reads into the preset, the
    derived protection clamp, and the ``path_safety`` load gate -- so inventing
    one where the operator declared nothing would refuse a design the household
    chose, on a number this module made up. That is exactly the nanny behaviour
    the 2026-08-14 ruling excludes (and the #2491 regression it would cause is
    pinned), which is why the style figure stays what it is: the plausibility
    anchor and the research prompt's worked number.
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
            "minimum_slope_db_per_octave": limit.protection_slope_db_per_octave,
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
    profile: DriverProtectionProfile,
    band_limit: Any,
) -> bool:
    return protection_highpass_floor_satisfied(
        highpass_hz=_band_highpass_hz(band_limit),
        floor_hz=profile.min_highpass_hz,
    )


def driver_protection_payload(
    role: Any,
    *,
    driver_style: Any = None,
    protection_status: Any = None,
    band_limit: Any = None,
) -> dict[str, Any]:
    """Return the protection envelope for one target.

    ``audio_allowed`` means the driver role/style has enough deterministic
    protection evidence to be considered by higher-level readiness gates. It
    does not bypass safe-session, backend, floor-confirmation, or Stop checks.
    """

    profile = driver_protection_profile(role, driver_style=driver_style)
    status = str(protection_status or "").strip().lower()
    issues: list[dict[str, str]] = []
    if profile.role_class == "unsupported":
        issues.append(_issue(
            "blocker",
            "driver_role_not_supported",
            "this driver role is not enabled for active-speaker audible tests",
        ))
    if profile.role_class == "high_frequency":
        if status not in {"present", "software_guard_requested"}:
            issues.append(_issue(
                "blocker",
                "high_frequency_protection_missing",
                "high-frequency drivers require marked physical protection or software-guarded bring-up",
            ))
        if not _highpass_satisfied(profile=profile, band_limit=band_limit):
            issues.append(_issue(
                "blocker",
                "high_frequency_highpass_missing",
                "high-frequency driver tone requires a protective high-pass band limit",
            ))
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DRIVER_PROTECTION_KIND,
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        **profile.to_dict(),
        "protection_status": status or None,
        "band_limit_highpass_ok": _highpass_satisfied(
            profile=profile,
            band_limit=band_limit,
        ),
        "audio_allowed": not issues and profile.role_class in {
            "low_frequency",
            "high_frequency",
        },
        "issues": issues,
    }


def _meter_from_inputs(
    *,
    calibration_level: dict[str, Any] | None,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
) -> dict[str, Any]:
    if observed_mic_dbfs is not None or mic_clipping:
        return classify_mic_meter(
            observed_dbfs=observed_mic_dbfs,
            clipping=mic_clipping,
        )
    if isinstance(calibration_level, dict) and isinstance(
        calibration_level.get("mic_meter"),
        dict,
    ):
        return dict(calibration_level["mic_meter"])
    return classify_mic_meter()


def auto_level_decision(
    calibration_level: dict[str, Any] | None,
    *,
    role: Any,
    driver_style: Any = None,
    protection_status: Any = None,
    band_limit: Any = None,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
    floor_audio_confirmed: bool = False,
    stop_control_available: bool = True,
) -> dict[str, Any]:
    """Return one bounded closed-loop level decision.

    The decision is deliberately one bounded ramp step only. Callers that
    persist state must run this again after each observed tone, which keeps the
    loop interruptible and makes every upward move inspectable without forcing
    one-dB discovery clicks.
    """

    protection = driver_protection_payload(
        role,
        driver_style=driver_style,
        protection_status=protection_status,
        band_limit=band_limit,
    )
    profile = driver_protection_profile(role, driver_style=driver_style)
    current = _current_level(calibration_level)
    meter = _meter_from_inputs(
        calibration_level=calibration_level,
        observed_mic_dbfs=observed_mic_dbfs,
        mic_clipping=mic_clipping,
    )
    meter_status = str(meter.get("status") or "unmeasured")
    max_level = min(MAX_TEST_LEVEL_DBFS, profile.max_auto_level_dbfs)
    issues = [issue for issue in protection["issues"] if isinstance(issue, dict)]
    if not stop_control_available:
        issues.append(_issue(
            "blocker",
            "stop_control_required",
            "closed-loop active-speaker level changes require Stop to be available",
        ))

    action = "hold"
    status = "blocked" if any(issue.get("severity") == "blocker" for issue in issues) else "hold"
    next_level = current
    reason = "level held"

    if meter_status == "clipping":
        action = "reset_to_floor"
        status = "reset"
        next_level = MIN_TEST_LEVEL_DBFS
        reason = "microphone clipped; reset to the quietest setting"
    elif issues:
        action = "hold"
        status = "blocked"
        reason = "selected driver is not ready for a quiet test"
    elif meter_status == "too_loud":
        action = "lower"
        status = "lower"
        next_level = max(MIN_TEST_LEVEL_DBFS, current - TEST_LEVEL_STEP_DB)
        reason = "microphone reading is too loud"
    elif meter_status in {"too_quiet", "low", "unmeasured"}:
        operator_controlled = meter_status == "unmeasured"
        if (
            profile.requires_floor_confirmation_above_floor
            and not floor_audio_confirmed
        ):
            action = "hold_for_floor_confirmation"
            status = "waiting_for_floor_confirmation"
            next_level = current
            reason = "quietest-level audio must be confirmed before raising"
        elif current >= max_level - 1e-6:
            action = "hold_at_cap"
            status = "maxed"
            next_level = max_level
            reason = "driver-specific auto-level cap reached"
            issues.append(_issue(
                "warning",
                "auto_level_cap_reached",
                (
                    "operator-controlled raise reached the driver-specific level cap"
                    if operator_controlled
                    else "mic target was not reached before the driver-specific level cap"
                ),
            ))
        else:
            action = "raise"
            status = "raise"
            next_level = min(current + AUDIBLE_RAMP_STEP_DB, max_level)
            reason = (
                "operator-controlled raise toward audible"
                if operator_controlled
                else "microphone reading is below the usable window"
            )
    elif meter_status == "usable":
        action = "hold"
        status = "locked"
        next_level = current
        reason = "microphone reading is in the usable window"
    next_level = clamp_test_level_dbfs(next_level)
    if next_level > max_level:
        next_level = max_level
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": AUTO_LEVEL_DECISION_KIND,
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        "status": status,
        "action": action,
        "reason": reason,
        "current_level_dbfs": current,
        "next_level_dbfs": next_level,
        "applied_delta_db": round(next_level - current, 3),
        "max_auto_level_dbfs": max_level,
        "step_db": AUDIBLE_RAMP_STEP_DB,
        "manual_step_db": TEST_LEVEL_STEP_DB,
        "mic_meter": {
            **meter,
            "usable_min_dbfs": MIC_USABLE_MIN_DBFS,
            "usable_max_dbfs": MIC_USABLE_MAX_DBFS,
        },
        "floor_audio_confirmed": bool(floor_audio_confirmed),
        "stop_control_available": bool(stop_control_available),
        "driver_protection": protection,
        "issues": issues,
    }
