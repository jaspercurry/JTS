# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.biquad import FilterSpec, PeqFilter
from jasper.camilla_emit import emit_gain_filter, emit_linkwitz_riley, emit_peaking_biquad, fmt
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.speaker_layout import SUB_CROSSOVER_ORDER

from ..camilla_names import (
    STARTUP_MUTE_GAIN_DB,
    bass_management_hp_name,
    driver_baseline_gain_name,
    driver_baseline_limiter_name,
    driver_delay_name,
    driver_limiter_name,
    driver_linearization_peak_name,
    driver_linearization_shelf_name,
    driver_linearization_taper_name,
    name_token,
    output_commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name,
    sub_baseline_limiter_name,
    sub_lowpass_name,
    sub_startup_limiter_name,
)
from ..profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    lowest_driver_role,
    required_driver_roles,
)
from ..test_signal_plan import protective_tweeter_highpass_frequency_hz

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
from .devices import _finite_float
from .ledger import (
    MAX_PROGRAM_HEADROOM_DB,
    _branch_context,
    _correction_bool,
    _correction_value,
    linearization_has_boost,
    program_headroom_db,
)
from .topology import _ordered_regions, _output_count

STARTUP_HEADROOM_DB = 40.0

COMMISSIONING_HEADROOM_DB = 0.0

STARTUP_LIMITER_CLIP_LIMIT_DB = -12.0

COMMISSIONING_FILTER_MODE = "protected_startup"

APPLIED_RESPONSE_FILTER_MODE = "applied_crossover_response"

BASELINE_LIMITER_CLIP_LIMIT_DB = -1.0


def _emit_delay_filter(name: str, delay_ms: float = 0.0) -> list[str]:
    return [
        f"  {name}:",
        "    type: Delay",
        "    parameters:",
        f"      delay: {fmt(delay_ms)}",
        "      unit: ms",
    ]


def _emit_limiter_filter(
    name: str,
    *,
    clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    soft_clip: bool = True,
) -> list[str]:
    soft_clip_s = "true" if soft_clip else "false"
    return [
        f"  {name}:",
        "    type: Limiter",
        "    parameters:",
        f"      soft_clip: {soft_clip_s}",
        f"      clip_limit: {fmt(clip_limit_db)}",
    ]


def _crossover_filter_name(
    role: str,
    region: CrossoverRegion,
    *,
    highpass: bool,
) -> str:
    suffix = "hp" if highpass else "lp"
    return f"as_{name_token(role)}_{name_token(region.id)}_{suffix}"


def _driver_mute_name(role: str) -> str:
    return f"as_{name_token(role)}_startup_mute"


def _room_peq_name(index: int) -> str:
    return f"room_peq_{index}"


def _program_protection_name(role: str, index: int) -> str:
    return f"as_{name_token(role)}_program_protection_{index}"


def _sub_startup_mute_name() -> str:
    return "as_sub_startup_mute"


def crossover_highpass_for_role(
    preset: ActiveSpeakerPreset, role: str
) -> tuple[str, float, int] | None:
    """Return the applied crossover high-pass protecting ``role``."""

    for region in _ordered_regions(preset):
        if region.upper_driver == role:
            return (
                _crossover_filter_name(role, region, highpass=True),
                region.fc_hz,
                region.order,
            )
    return None


def _protective_tweeter_hp_frequency(
    preset: ActiveSpeakerPreset,
    role: str,
) -> float | None:
    return protective_tweeter_highpass_frequency_hz(preset, role)


def _emit_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    lines.extend(_emit_bass_management_hp_definition(preset))
    for role in required_driver_roles(preset.way_count):
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        lines.extend(_emit_delay_filter(driver_delay_name(role)))
        lines.extend(emit_gain_filter(
            _driver_mute_name(role),
            STARTUP_MUTE_GAIN_DB,
            mute=True,
        ))
        lines.extend(_emit_limiter_filter(
            driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_startup_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    return "\n".join(lines)


def _validated_driver_corrections(
    preset: ActiveSpeakerPreset,
    corrections: dict[str, dict[str, float | bool]] | None,
) -> dict[str, dict[str, float | bool]]:
    """Normalize the final per-driver correction gate shared by both emitters."""

    safe_corrections: dict[str, dict[str, float | bool]] = {}
    for role, values in (corrections or {}).items():
        if role not in required_driver_roles(preset.way_count):
            continue
        if not isinstance(values, dict):
            continue
        gain_db = _correction_value({role: values}, role, "gain_db", 0.0)
        delay_ms = _correction_value({role: values}, role, "delay_ms", 0.0)
        if gain_db > 0:
            raise ActiveSpeakerConfigError(
                f"baseline correction gain for {role} must not be positive"
            )
        if delay_ms < 0 or delay_ms > 20:
            raise ActiveSpeakerConfigError(
                f"baseline delay for {role} must be between 0 and 20 ms"
            )
        safe_corrections[role] = {
            "gain_db": gain_db,
            "delay_ms": delay_ms,
            "inverted": bool(values.get("inverted")),
        }
    return safe_corrections


# --- Layer-1a driver-linearization emission ----------------------------------
#
# Reduced shape only: {role: [{biquad_type, freq, q, gain}, ...]}. The richer
# LinearizationFit.to_dict() is candidate/profile evidence, not emitter input;
# linearization_fit.linearization_filters_by_role() reduces it before any caller
# reaches this module.

# Hard cap on filters per driver (shelf + peaking combined). LOCKSTEP DUPLICATE
# of linearization_fit.MAX_FILTERS_PER_DRIVER — deliberately not imported,
# because the emitter independently re-validates whatever a persisted candidate
# claims rather than inheriting the fit engine's policy. A pinning test asserts
# the two stay numerically equal.
MAX_LINEARIZATION_FILTERS_PER_DRIVER = 8

LINEARIZATION_BIQUAD_TYPES = frozenset({"Peaking", "Highshelf", "Lowshelf"})


# A linearization shelf carries NO steepness of its own. Every shelf reaches
# CamillaDSP through ``emit_filter_spec``, which spells the one Butterworth
# ``biquad.SHELF_Q`` — the same Q the fit engine designed the
# shelf at and scored its residual with. Both shelf types share it.
#
# CamillaDSP's Butterworth is ``slope: 12`` (S = slope/12, S = 1); at
# ``slope: 6`` the realized Q falls with the shelf's gain (0.476 at -11 dB,
# missing the designed curve by up to 1.7 dB across the tweeter band). See
# ``SHELF_Q`` for the formula and the upstream test that pins it.


def linearization_slot(
    index: int, count: int, filters: Sequence[Mapping[str, Any]],
) -> str:
    """Classify one filter's role in a linearization chain by POSITION:
    ``"shelf"`` (a leading Highshelf/Lowshelf at index 0), ``"taper"`` (a
    trailing Highshelf after a Lowshelf lead), else ``"peak"``.

    The single source of the shelf-first / taper-last rule, shared by the
    validation gate, the chain namer and the definition emitter. It classifies
    whatever order the input carries; enforcing that order is
    ``_validate_linearization_shelf_structure``'s job.
    """
    biquad_type = filters[index]["biquad_type"]
    leading_is_lowshelf = count > 0 and filters[0]["biquad_type"] == "Lowshelf"
    if index == 0 and biquad_type in ("Highshelf", "Lowshelf"):
        return "shelf"
    if (
        index == count - 1
        and index != 0
        and biquad_type == "Highshelf"
        and leading_is_lowshelf
    ):
        return "taper"
    return "peak"


def _validated_biquad_entry(
    entry: Any,
    *,
    label: str,
    allowed_types: frozenset[str],
    max_gain_db: float | None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, Any]:
    """Re-validate ONE persisted biquad record, or raise.

    Shared by every emitter gate that accepts a caller-supplied biquad list;
    each list crosses a JSON round trip before it reaches this module, so this
    is a never-trust-the-caller re-validation. Each caller keeps its own POLICY
    (permitted types, gain ceiling, entry count, order); this owns the
    per-entry field contract, and RAISES rather than clamping — a value out of
    range means the record was not written by the code that claims to own it.

    ``label`` names the owner in the message. The Nyquist refusal
    (``freq >= sample_rate / 2``) is this module's own proof: a biquad at or
    above Nyquist is not a realizable digital filter corner at all and
    CamillaDSP refuses it at ``--check``, so admitting one would stage a config
    guaranteed to fail load.
    """

    if not isinstance(entry, Mapping):
        raise ActiveSpeakerConfigError(f"{label} filter must be a mapping")
    biquad_type = entry.get("biquad_type")
    if biquad_type not in allowed_types:
        raise ActiveSpeakerConfigError(
            f"{label} biquad_type must be one of "
            f"{sorted(allowed_types)}, not {biquad_type!r}"
        )
    freq = _finite_float(entry.get("freq"), f"{label} freq")
    q = _finite_float(entry.get("q"), f"{label} q")
    gain = _finite_float(entry.get("gain"), f"{label} gain")
    if freq <= 0:
        raise ActiveSpeakerConfigError(f"{label} freq must be positive")
    nyquist_hz = sample_rate / 2.0
    if freq >= nyquist_hz:
        raise ActiveSpeakerConfigError(
            f"{label} freq must be below Nyquist ({nyquist_hz} Hz at "
            f"{sample_rate} Hz sample rate)"
        )
    if q <= 0:
        raise ActiveSpeakerConfigError(f"{label} q must be positive")
    if max_gain_db is not None and gain > max_gain_db:
        raise ActiveSpeakerConfigError(
            f"{label} gain must not exceed {max_gain_db} dB"
        )
    return {"biquad_type": biquad_type, "freq": freq, "q": q, "gain": gain}


def _validated_linearization(
    preset: ActiveSpeakerPreset,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Validate per-driver filter shape; the composed chain owns headroom."""

    safe: dict[str, list[dict[str, Any]]] = {}
    for role, filters in (linearization or {}).items():
        if role not in required_driver_roles(preset.way_count):
            continue
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            raise ActiveSpeakerConfigError(
                f"linearization filters for {role} must be a list"
            )
        if len(filters) > MAX_LINEARIZATION_FILTERS_PER_DRIVER:
            raise ActiveSpeakerConfigError(
                f"linearization filter count for {role} exceeds "
                f"{MAX_LINEARIZATION_FILTERS_PER_DRIVER}"
            )
        role_filters: list[dict[str, Any]] = []
        for entry in filters:
            role_filters.append(_validated_biquad_entry(
                entry,
                label=f"linearization {role}",
                allowed_types=LINEARIZATION_BIQUAD_TYPES,
                max_gain_db=None,
            ))
        _validate_linearization_shelf_structure(role, role_filters)
        if role_filters:
            safe[role] = role_filters
    return safe


# Ceiling on how many blend-correction cuts a candidate may carry, held here for
# the reason ``MAX_LINEARIZATION_FILTERS_PER_DRIVER`` is. A pinning test asserts
# it stays numerically equal to the solver's own constant.
MAX_BLEND_CORRECTION_FILTERS = 2


# The blend correction is CUTS-ONLY. Two independent places hold that: the
# solver cannot represent a boost, and this gate REFUSES one — between them sits
# a JSON round trip through a persisted candidate, which is where a value the
# solver never produced could appear. A refusal rather than a clamp, because a
# positive gain means the record was not written by its claimed owner.
MAX_BLEND_CORRECTION_GAIN_DB = 0.0

_BLEND_CORRECTION_BIQUAD_TYPES = frozenset({"Peaking"})


def _blend_correction_name(index: int) -> str:
    return f"as_blend_{index}"


def _validated_blend_correction(
    blend_correction: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Normalize + independently re-validate the pre-split blend correction.

    The crossover blend region's bounded shape correction, solved from the
    summed at-the-mark measurement and emitted on the stereo program bus before
    the split mixer. Per-entry fields go through ``_validated_biquad_entry``;
    the policy this gate adds is stage-specific — Peaking only (a shelf across a
    two-octave blend would re-level the region, which is the trim's job), at
    most :data:`MAX_BLEND_CORRECTION_FILTERS` entries, and every ``gain`` at or
    below :data:`MAX_BLEND_CORRECTION_GAIN_DB`.

    An empty correction returns ``[]`` and emits no stage.
    """

    if blend_correction is None:
        return []
    if (
        not isinstance(blend_correction, Sequence)
        or isinstance(blend_correction, (str, bytes))
    ):
        raise ActiveSpeakerConfigError("blend correction must be a list")
    if len(blend_correction) > MAX_BLEND_CORRECTION_FILTERS:
        raise ActiveSpeakerConfigError(
            f"blend correction filter count exceeds "
            f"{MAX_BLEND_CORRECTION_FILTERS}"
        )
    return [
        _validated_biquad_entry(
            entry,
            label="blend correction",
            allowed_types=_BLEND_CORRECTION_BIQUAD_TYPES,
            max_gain_db=MAX_BLEND_CORRECTION_GAIN_DB,
        )
        for entry in blend_correction
    ]


def _validate_linearization_shelf_structure(
    role: str, role_filters: list[dict[str, Any]],
) -> None:
    """Fail-closed structural gate on shelf placement.

    The fit engine emits shelves in one of two shapes only: a single LEADING
    shelf at position 0, and — only after a Lowshelf lead — a single TRAILING
    Highshelf taper as the last entry. Any other placement means the persisted
    candidate was corrupted or produced by something else, so it raises rather
    than letting a duplicate filter name reach the graph. Peaking is always fine.
    """
    shelf_types = {"Highshelf", "Lowshelf"}
    n = len(role_filters)
    for i, entry in enumerate(role_filters):
        biquad_type = entry["biquad_type"]
        # A shelf-type entry is legal only where it occupies a shelf/taper slot;
        # one that classifies as a "peak" slot (a shelf mid-chain, a second
        # shelf, a taper without a Lowshelf lead, a taper not last) is invalid.
        if (
            biquad_type in shelf_types
            and linearization_slot(i, n, role_filters) == "peak"
        ):
            raise ActiveSpeakerConfigError(
                f"linearization shelf placement for {role} is invalid: a "
                f"{biquad_type} may only appear as the leading filter, or (a "
                f"Highshelf taper) as the trailing filter after a Lowshelf lead"
            )


def _driver_linearization_chain_names(
    linearization: dict[str, list[dict[str, Any]]],
    role: str,
) -> list[str]:
    """The filter-name list for ``role``'s linearization stage, IN INPUT ORDER.

    Names whatever order the input carries and never reorders it; the
    shelf-first / taper-last order is the fit engine's construction guarantee,
    re-validated at the emitter boundary by
    ``_validate_linearization_shelf_structure``."""

    filters = linearization.get(role) or []
    names: list[str] = []
    peak_index = 0
    count = len(filters)
    for i in range(count):
        slot = linearization_slot(i, count, filters)
        if slot == "shelf":
            names.append(driver_linearization_shelf_name(role))
        elif slot == "taper":
            names.append(driver_linearization_taper_name(role))
        else:
            peak_index += 1
            names.append(driver_linearization_peak_name(role, peak_index))
    return names


def _emit_driver_linearization_definitions(
    linearization: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Definitions for every role's linearization filters, via the shared
    ``emit_filter_spec`` leaf.

    Shelf-type entries carry NO steepness on their ``FilterSpec``:
    ``emit_filter_spec`` spells every shelf at the one Butterworth ``SHELF_Q``
    the fit engine designed it at, and the entry's own ``q`` is dropped for the
    same reason — honouring a stray value would emit a shelf no evaluator in the
    fit loop can see. Position-aware naming mirrors
    ``_driver_linearization_chain_names`` so definitions and pipeline names
    cannot disagree.
    """

    lines: list[str] = []
    for role, filters in linearization.items():
        peak_index = 0
        count = len(filters)
        for i, entry in enumerate(filters):
            slot = linearization_slot(i, count, filters)
            if slot == "shelf":
                spec = FilterSpec(
                    name=driver_linearization_shelf_name(role),
                    biquad_type=entry["biquad_type"],
                    freq=entry["freq"],
                    gain=entry["gain"],
                )
            elif slot == "taper":
                spec = FilterSpec(
                    name=driver_linearization_taper_name(role),
                    biquad_type="Highshelf",
                    freq=entry["freq"],
                    gain=entry["gain"],
                )
            else:
                peak_index += 1
                spec = FilterSpec(
                    name=driver_linearization_peak_name(role, peak_index),
                    biquad_type="Peaking",
                    freq=entry["freq"],
                    gain=entry["gain"],
                    q=entry["q"],
                )
            lines.extend(emit_filter_spec(spec))
    return lines


def _emit_baseline_driver_definitions(
    preset: ActiveSpeakerPreset,
    *,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    linearization: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """The driver-domain (Layer A) filter definitions shared by the solo/leader
    baseline and the follower's driver-domain-only graph.

    The per-region Linkwitz-Riley crossover pair, then each driver's [delay,
    non-positive baseline gain, soft-clip limiter] chain. The *intra-speaker*
    half only — no program-domain headroom, no preference EQ — so the follower's
    relocated Layer A is byte-for-byte the chain a solo speaker runs.
    ``linearization`` is threaded only by the solo/leader baseline caller.
    """
    lines: list[str] = []
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    # Layer-1a driver linearization: immediately after the crossover HP/LP
    # definitions, before bass-management/bass-extension. Empty emits nothing.
    lines.extend(_emit_driver_linearization_definitions(linearization or {}))
    # Bass-management high-pass on the lowest driver (the complementary upper half
    # of the single sub crossover). Emitted only when a local sub is present.
    sub = preset.local_subwoofer
    lines.extend(_emit_bass_management_hp_definition(preset))
    for role in required_driver_roles(preset.way_count):
        delay_ms = _correction_value(corrections, role, "delay_ms", 0.0)
        gain_db = _correction_value(corrections, role, "gain_db", 0.0)
        inverted = _correction_bool(corrections, role, "inverted")
        lines.extend(_emit_delay_filter(driver_delay_name(role), delay_ms=delay_ms))
        lines.extend(emit_gain_filter(
            driver_baseline_gain_name(role),
            gain_db,
            inverted=inverted,
        ))
        lines.extend(_emit_limiter_filter(
            driver_baseline_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    # The local-sub lane definitions: LR4 low-pass (band-limit) + non-positive
    # baseline gain + soft-clip limiter (excursion), same protection a main gets.
    if sub is not None:
        lines.extend(_emit_sub_baseline_definitions(
            sub.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    return lines


def _emit_bass_management_hp_definition(preset: ActiveSpeakerPreset) -> list[str]:
    """The LR4 bass-management high-pass filter def on the lowest driver, or [].

    The complementary upper half of the single sub crossover at the sub corner.
    Shared by every emitter (startup/commissioning/baseline) so the HP corner +
    order have ONE definition that cannot drift between them."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    return emit_linkwitz_riley(
        bass_management_hp_name(lowest_driver_role(preset.way_count)),
        highpass=True,
        freq_hz=sub.crossover_fc_hz,
        order=SUB_CROSSOVER_ORDER,
    )


def _emit_sub_startup_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub startup/commissioning lane definitions: LR4 low-pass +
    soft-clip limiter + hard mute.

    The sub starts muted for commissioning safety; the band-limit and excursion
    limiter are still present so an un-muting path arms a protected output."""
    return [
        *emit_linkwitz_riley(
            sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
        *emit_gain_filter(_sub_startup_mute_name(), STARTUP_MUTE_GAIN_DB, mute=True),
    ]


def _emit_sub_commissioning_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub commissioning lane definitions: LR4 low-pass + soft-clip
    limiter only.

    The lane's own startup mute is dropped (the per-output commission mute does
    the muting), so no orphan mute filter is emitted; the band-limit and
    excursion limiter stay so the output is protected when the mute is lifted."""
    return [
        *emit_linkwitz_riley(
            sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def _emit_sub_baseline_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub baseline filter definitions: LR4 low-pass + gain + limiter.

    The durable graph's sub protection is this ``gain <= 0`` + soft-clip
    limiter, band-limited by the LR4 low-pass at the bass-management corner. The
    commissioning-tone bounds (50 Hz floor / 300 ms) live in
    ``driver_protection.driver_protection_profile('subwoofer')`` instead.
    """
    return [
        *emit_linkwitz_riley(
            sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *emit_gain_filter(sub_baseline_gain_name(), 0.0),
        *_emit_limiter_filter(
            sub_baseline_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def _emit_baseline_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    baseline_headroom_db: float,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
    blend_correction: Sequence[Mapping[str, Any]] = (),
    rear_calibration: Mapping[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    room_peqs = tuple(room_peqs)
    for i, peq in enumerate(room_peqs, start=1):
        lines.extend(
            emit_peaking_biquad(
                _room_peq_name(i),
                freq=peq.freq,
                q=peq.q,
                gain=peq.gain,
            )
        )
    # Crossover blend correction — the same Peaking primitive the room PEQs use,
    # wired beside them pre-split.
    #
    # It charges NO headroom because it CANNOT BOOST
    # (``_validated_blend_correction`` refuses a positive gain), not because the
    # common attenuation covers it: a boost posture would have to ADD a term to
    # ``total_headroom_db``, since position above the gain is necessary for
    # absorption and not sufficient for it.
    for i, entry in enumerate(blend_correction, start=1):
        lines.extend(
            emit_peaking_biquad(
                _blend_correction_name(i),
                freq=float(entry["freq"]),
                q=float(entry["q"]),
                gain=float(entry["gain"]),
            )
        )
    # The active graph's single place for explicit common attenuation: baseline
    # headroom, room-correction boost headroom, the Layer-1a linearization
    # boost, plus the household's manual headroom / loudness-match
    # output_trim_db. Preference boosts themselves ride at unity, matching the
    # stereo /sound policy; room-correction and linearization boosts can raise a
    # band above unity, so their worst case is folded in here instead. It rides
    # the PRE-SPLIT gain because every branch sees the same program, so absorbing
    # the worst branch's total covers all of them — the mechanism that lets the
    # fit engine's boost stay uncapped while the 0 dB ceiling stays a hard rail.
    # The linearization term is the SAME quantity the fit discloses as
    # ``LinearizationFit.headroom_cost_db``.
    #
    # A NUMBER, never a gate: `active_baseline_headroom` is always emitted, so
    # folding the trim into its value keeps a flat-window crossing a parameter
    # write rather than stepping the gain by the whole trim, un-ducked, the
    # moment a band crosses ±0.05 dB. Matches the stereo path's `sound_preamp`.
    total_headroom_db = program_headroom_db(
        linearization, baseline_headroom_db=baseline_headroom_db,
        room_peqs=room_peqs, output_trim_db=output_trim_db,
        rear_calibration=rear_calibration,
        branch_context=_branch_context(preset, corrections) if linearization_has_boost(linearization) else {},
    )
    if total_headroom_db > MAX_PROGRAM_HEADROOM_DB:
        raise ActiveSpeakerConfigError(
            f"program headroom {total_headroom_db:g} dB exceeds {MAX_PROGRAM_HEADROOM_DB:g} dB"
        )
    headroom_gain_db = 0.0 if total_headroom_db == 0 else -total_headroom_db
    lines.extend(
        emit_gain_filter(
            "active_baseline_headroom",
            headroom_gain_db,
        )
    )
    lines.extend(_emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=corrections,
        linearization=linearization,
    ))
    # Program-domain preference EQ (Layer C) definitions, via the shared
    # emit_filter_spec leaf so the active and stereo paths spell a preference
    # band identically. Wired pre-split — see _emit_baseline_pipeline.
    for spec in preference_filters:
        lines.extend(emit_filter_spec(spec))
    return "\n".join(lines)


def _emit_commissioning_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
    audible_outputs: frozenset[int],
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delays_us: Mapping[str, float] | None = None,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    # The delay lane: definitions only for the roles the caller named.
    #
    # ONE `fmt` pass over the raw microsecond value and no intermediate
    # rounding — `_emit_delay_filter` formats through `jasper.camilla_emit.fmt`,
    # which IS `delay_graph.quantized_delay_ms`'s implementation, so a proof
    # recomputing from the same `delay_us` matches by construction.
    delays = dict(measurement_delays_us or {})
    if delays:
        if protection_sections_by_role is None:
            # The unprotected shape already defines a zero Delay filter per
            # role, so a named delay would emit a duplicate mapping key whose
            # later zero wins on parse — a capture that plays undelayed and
            # banks as a delayed take.
            raise ActiveSpeakerConfigError(
                "a measurement delay needs the protected-neutral program shape; "
                "the unprotected shape carries its own zeroed delay lane"
            )
        known = set(required_driver_roles(preset.way_count))
        unknown = sorted(set(delays) - known)
        if unknown:
            # An unreferenced Delay filter would leave the capture undelayed
            # while its graph fingerprint claimed otherwise.
            raise ActiveSpeakerConfigError(
                f"measurement delays name roles this preset has no branch for: "
                f"{unknown}"
            )
        for role, delay_us in sorted(delays.items()):
            value = float(delay_us)
            if not math.isfinite(value):
                # A non-finite value emits `delay: .nan`, which parses back as a
                # float and would read as a bound question rather than a
                # nonsense one. The RANGE is `_assert_measurement_delays_bound`'s.
                raise ActiveSpeakerConfigError(
                    f"measurement delay for {role!r} is not finite: {delay_us!r}"
                )
            lines.extend(_emit_delay_filter(
                driver_delay_name(role), delay_ms=value / 1000.0,
            ))
    for region in (() if protection_sections_by_role is not None else _ordered_regions(preset)):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    # The bass-management HP is referenced by the lowest driver's commissioning
    # chain (it preserves the running graph's protection), so its definition must
    # be present here too.
    if protection_sections_by_role is None:
        lines.extend(_emit_bass_management_hp_definition(preset))
    for role in required_driver_roles(preset.way_count):
        for index, section in enumerate((protection_sections_by_role or {}).get(role, ())):
            lines.extend(emit_linkwitz_riley(
                _program_protection_name(role, index),
                highpass=section.highpass,
                freq_hz=section.fc_hz,
                order=section.order,
            ))
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if filter_mode == COMMISSIONING_FILTER_MODE and protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        if protection_sections_by_role is None:
            lines.extend(_emit_delay_filter(driver_delay_name(role)))
        lines.extend(_emit_limiter_filter(
            driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    # The local-sub lane definitions (LR4 low-pass + soft-clip limiter): the sub
    # output is band-limited AND excursion-limited even in the commissioning
    # graph. Its muting is the per-output commission mask below.
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_commissioning_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    # Per-output commissioning mute: only audible outputs pass, so exactly one
    # physical driver is excited through the real graph; the empty default is
    # fully muted. An audible output carries ``audible_gain_db``, which defaults
    # to the silent mute floor, so an un-ramped commission load arms the target
    # at {gain: -120, mute: off}. Muted outputs stay at -120 dB regardless.
    for index in range(_output_count(preset)):
        is_audible = index in audible_outputs
        lines.extend(emit_gain_filter(
            output_commission_mute_name(index),
            audible_gain_db if is_audible else STARTUP_MUTE_GAIN_DB,
            mute=not is_audible,
        ))
    return "\n".join(lines)
