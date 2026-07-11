# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and apply accepted active-speaker baseline profiles.

The baseline profile is the handoff from commissioning into normal playback:
it consumes saved crossover settings plus measurement evidence, writes a
durable CamillaDSP candidate YAML, and can explicitly load that YAML through
the shared DSP apply transaction. It does not play tones or capture audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from jasper.atomic_io import atomic_write_text
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
    FilterSpec,
    PeqFilter,
)
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    validate_camilla_config,
)
from jasper.output_topology import OutputTopology

from ._common import issue as _issue
from .camilla_yaml import (
    DRIVER_DOMAIN_PROGRAM_CHANNELS,
    emit_active_speaker_baseline_config,
    emit_active_speaker_driver_domain_config,
)
from .crossover_contract import (
    TUNING_OWNERS,
    automatic_candidate_readiness,
    crossover_snapshot_state,
    legacy_manual_preservation_state,
)
from .playback_route import (
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .revalidation import applied_profile_revalidation_satisfies_driver_target_proof
from .staging import (
    _passive_mains_with_sub_preset,
    compile_preset_from_crossover_preview,
    topology_is_passive_mains_with_sub,
)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_baseline_profile.json")
DEFAULT_CONFIG_PATH = Path("/var/lib/camilladsp/configs/active_speaker_baseline.yml")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE"
CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH"

# Sensitivity deltas below this magnitude (dB) are treated as level-matched and
# get no derived trim, so the least-sensitive (reference) driver and any ties
# stay at unity.
_SENSITIVITY_TRIM_EPS_DB = 0.05
# Floor for any single attenuation, mirroring the explicit-gain clamp below.
_MAX_ATTENUATION_DB = -60.0
_EXCITATION_MATCH_TOLERANCE_DB = 0.05


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def baseline_profile_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def baseline_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


def _safe_id(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in value)
    return out.strip("_")[:80] or "active_speaker"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def topology_config_fingerprint(topology: OutputTopology) -> str:
    """Fingerprint only topology fields that determine emitted DSP config."""
    return _fingerprint({
        key: value
        for key, value in topology.to_dict().items()
        if key != "pairing_intent"
    })


def _source_payload(
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
) -> dict[str, Any]:
    measurement_summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), Mapping)
        else {}
    )
    # The baseline config cache invalidates whenever this source fingerprint
    # changes, so the topology fingerprint must cover ONLY fields that determine
    # the emitted CamillaDSP config. `pairing_intent` is commission-time design
    # intent that, by contract, drives no config (the multiroom reconciler
    # resolves the runtime role from grouping.env, not from this field — see
    # output_topology.py and docs/HANDOFF-distributed-active.md gap 1), so it is
    # excluded: toggling it must not force a needless baseline recompile, and
    # excluding it keeps the fingerprint stable across the field's introduction.
    # The "pairing field never changes the cache" contract is pinned by
    # test_pairing_intent_change_does_not_invalidate_baseline_cache, which fails
    # if this key is ever renamed without updating the exclusion here.
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        "design_draft_updated_at": design_draft.get("updated_at"),
        "crossover_preview_updated_at": crossover_preview.get("updated_at"),
        "crossover_preview_fingerprint": (
            (crossover_preview.get("source") or {}).get("design_draft_fingerprint")
            if isinstance(crossover_preview.get("source"), Mapping)
            else None
        ),
        "measurements_updated_at": measurements.get("updated_at"),
        "measurement_summary_fingerprint": _fingerprint(measurement_summary),
    }
    return {**source, "fingerprint": _fingerprint(source)}


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _overlap_level_at(
    record: Any, fc: float, *, tol_hz: float = 1.0
) -> float | None:
    """A usable measured overlap-band level (dB) for ``fc``, or None (fail-closed).

    Requires the driver's acoustic verdict to be ``present`` (the driver actually
    produced in-band sound) and an overlap entry around ``fc`` flagged ``usable``
    (good SNR, not silent, not clipped, enough bins). Anything else returns None,
    so a missing / low-SNR / clipped capture cannot contribute a measured trim.
    """
    if not isinstance(record, Mapping):
        return None
    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping) or acoustic.get("verdict") != "present":
        return None
    for entry in acoustic.get("overlap_levels") or ():
        if not isinstance(entry, Mapping) or not entry.get("usable"):
            continue
        entry_fc = _finite_float(entry.get("fc_hz"))
        if entry_fc is None:
            continue
        if abs(entry_fc - fc) <= max(tol_hz, fc * 0.01):
            return _finite_float(entry.get("level_db"))
    return None


def _effective_excitation_dbfs(record: Any) -> float | None:
    """Return a verified analyzer excitation, or ``None`` (fail closed).

    The excitation artifact is a small gain ledger owned by the capture record:
    generated sweep peak + the role-varying commissioning gain = the effective
    digital drive (the remaining commissioning gains are common and cancel).
    We recompute the total instead of trusting a loose scalar, which makes the
    evidence independently auditable and lets captures made through different
    applied role trims be normalized onto one common 0 dB reference. The quiet
    by-ear identity-test level is not acoustic measurement evidence.
    """
    if not isinstance(record, Mapping):
        return None
    ledger = record.get("excitation")
    if (
        not isinstance(ledger, Mapping)
        or ledger.get("schema_version") != 1
        or ledger.get("scope") not in {
            "sweep_plus_role_varying_commission_gain",
            "sweep_plus_role_gain_and_driver_level_lock",
        }
    ):
        return None
    sweep_peak = _finite_float(ledger.get("sweep_peak_dbfs"))
    commissioning_gain = _finite_float(ledger.get("commissioning_gain_db"))
    locked_main_volume = (
        _finite_float(ledger.get("locked_main_volume_db"))
        if ledger.get("scope") == "sweep_plus_role_gain_and_driver_level_lock"
        else 0.0
    )
    declared_effective = _finite_float(ledger.get("effective_peak_dbfs"))
    if (
        sweep_peak is None
        or commissioning_gain is None
        or locked_main_volume is None
        or declared_effective is None
    ):
        return None
    computed = sweep_peak + commissioning_gain + locked_main_volume
    if abs(computed - declared_effective) > _EXCITATION_MATCH_TOLERANCE_DB:
        return None
    return computed


def _measured_level_trims(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Per-role attenuation-only trim from the MEASURED overlap-band level deltas.

    For each adjacent-driver crossover, both drivers' near-field captures through
    the production graph give their level in a band centred on the shared Fc; the
    driver-to-driver delta is the relative sensitivity at the handoff (the −6 dB
    Linkwitz-Riley shoulder cancels). We chain those deltas into a per-driver
    attenuation (quietest driver = reference, 0 dB), so the acoustic sum is level
    across every crossover — the MEASURED refinement of the datasheet sensitivity
    trim ``_derive_corrections`` otherwise seeds.

    Returns ``(trims_by_role, meta)``. ``trims_by_role`` is empty (fail-closed)
    unless at least one speaker group has a usable overlap level for BOTH drivers
    of EVERY crossover — any silent / clipped / low-SNR / missing capture drops
    that group, and if no group qualifies the caller keeps the datasheet trim and
    marks the config provisional. Magnitude only: never a phase/delay decision.
    """
    from .capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    active_comparison_set = measurements.get("active_comparison_set")
    latest = measurements.get("latest_by_target")
    if not isinstance(latest, Mapping):
        summary = measurements.get("summary")
        latest = (
            summary.get("latest_driver_measurements")
            if isinstance(summary, Mapping)
            else None
        )
    records = [
        record
        for record in (latest.values() if isinstance(latest, Mapping) else [])
        if isinstance(record, Mapping)
    ]

    roles = required_driver_roles(preset.way_count)
    regions = sorted(preset.crossover_regions, key=lambda region: region.fc_hz)

    by_group: dict[str, dict[str, Mapping[str, Any]]] = {}
    for record in records:
        group_id = record.get("speaker_group_id")
        role = record.get("role")
        if (
            isinstance(group_id, str)
            and group_id
            and isinstance(role, str)
            and role in roles
        ):
            by_group.setdefault(group_id, {})[role] = record

    per_group_trims: list[dict[str, float]] = []
    deltas: list[dict[str, Any]] = []
    incomparable_groups: list[dict[str, Any]] = []
    for group_id, group_records in sorted(by_group.items()):
        if not any(
            isinstance(record.get("acoustic"), Mapping)
            for record in group_records.values()
        ):
            # Operator-only floor checks prove routing but are not attempted as
            # acoustic level evidence, so do not diagnose their intentionally
            # absent analyzer ledger as malformed.
            continue
        placement_invalid_roles = [
            role
            for role in roles
            if not capture_proof_valid(
                group_records.get(role),
                active_comparison_set,
                policy_id=DRIVER_PLACEMENT_POLICY_ID,
                role=role,
                speaker_group_id=group_id,
            )
        ]
        if placement_invalid_roles:
            incomparable_groups.append({
                "speaker_group_id": group_id,
                "reason": "placement_or_comparison_set_missing_or_invalid",
                "roles": placement_invalid_roles,
            })
            continue
        excitation_by_role = {
            role: _effective_excitation_dbfs(group_records.get(role))
            for role in roles
        }
        if any(value is None for value in excitation_by_role.values()):
            incomparable_groups.append({
                "speaker_group_id": group_id,
                "reason": "excitation_ledger_missing_or_invalid",
            })
            continue
        assert all(value is not None for value in excitation_by_role.values())
        raw: dict[str, float] = {roles[0]: 0.0}
        group_deltas: list[dict[str, Any]] = []
        usable = True
        for region in regions:
            lo_role = region.lower_driver
            up_role = region.upper_driver
            fc = float(region.fc_hz)
            measured_lo = _overlap_level_at(group_records.get(lo_role), fc)
            measured_up = _overlap_level_at(group_records.get(up_role), fc)
            level_lo = (
                measured_lo - float(excitation_by_role[lo_role])
                if measured_lo is not None
                else None
            )
            level_up = (
                measured_up - float(excitation_by_role[up_role])
                if measured_up is not None
                else None
            )
            if level_lo is None or level_up is None or lo_role not in raw:
                usable = False
                break
            # effective[U] == effective[L]  =>  trim[U] = trim[L] + L_lo - L_up
            raw[up_role] = raw[lo_role] + level_lo - level_up
            group_deltas.append({
                "speaker_group_id": group_id,
                "crossover_fc_hz": fc,
                "lower_role": lo_role,
                "upper_role": up_role,
                "delta_db": round(level_up - level_lo, 1),  # + => upper hotter
                "effective_peak_dbfs": {
                    lo_role: round(float(excitation_by_role[lo_role]), 2),
                    up_role: round(float(excitation_by_role[up_role]), 2),
                },
            })
        if not usable or set(raw) != set(roles):
            continue
        offset = max(raw.values())  # quietest driver becomes the 0 dB reference
        per_group_trims.append({
            role: max(round(raw[role] - offset, 1), _MAX_ATTENUATION_DB)
            for role in roles
        })
        deltas.extend(group_deltas)

    meta: dict[str, Any] = {
        "groups_total": len(by_group),
        "groups_measured": len(per_group_trims),
        "measured_group_ids": sorted({
            str(item["speaker_group_id"])
            for item in deltas
            if item.get("speaker_group_id")
        }),
        "deltas": deltas,
        "comparison": "placement_attested_gain_ledger_normalized",
        "placement_policy": DRIVER_PLACEMENT_POLICY_ID,
        "active_comparison_set_id": (
            active_comparison_set.get("comparison_set_id")
            if isinstance(active_comparison_set, Mapping)
            else None
        ),
        "incomparable_groups": incomparable_groups,
    }
    if not per_group_trims:
        return {}, meta

    averaged = {
        role: sum(group[role] for group in per_group_trims) / len(per_group_trims)
        for role in roles
    }
    offset = max(averaged.values())
    trims = {
        role: max(round(averaged[role] - offset, 1), _MAX_ATTENUATION_DB)
        for role in roles
    }
    meta["trims"] = dict(trims)
    return trims, meta


def _derive_corrections(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    tuning_owner: str = "manual",
) -> tuple[dict[str, dict[str, float | bool]], list[dict[str, str]], dict[str, Any]]:
    if tuning_owner not in TUNING_OWNERS:
        raise ValueError(f"unsupported crossover tuning owner: {tuning_owner!r}")
    issues: list[dict[str, str]] = []
    corrections: dict[str, dict[str, float | bool]] = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in required_driver_roles(preset.way_count)
    }
    drivers = crossover_preview.get("drivers")
    pinned_gain_roles: set[str] = set()
    estimated_gains: dict[str, float] = {}
    gain_provenance: dict[str, str] = {}
    sensitivities: dict[str, float] = {}
    if isinstance(drivers, Mapping):
        for role, driver in drivers.items():
            if role not in corrections or not isinstance(driver, Mapping):
                continue
            sensitivity = _finite_float(driver.get("sensitivity_db_2v83_1m"))
            if sensitivity is not None:
                sensitivities[str(role)] = sensitivity
            gain = _finite_float(driver.get("gain_offset_db"))
            if gain is None:
                continue
            if gain > 0:
                issues.append(_issue(
                    "warning",
                    "positive_driver_gain_ignored",
                    f"positive gain for {role} was ignored; baseline gains only attenuate",
                ))
                gain = 0.0
            if gain < -60:
                issues.append(_issue(
                    "warning",
                    "driver_gain_clamped",
                    f"gain for {role} was clamped to -60 dB",
                ))
                gain = -60.0
            provenance = str(driver.get("gain_offset_db_provenance") or "").strip()
            # Pre-provenance preview artifacts are conservatively treated as a
            # pin: an upgrade must not replace a deliberate safety attenuation.
            if provenance not in {"research_estimate", "sensitivity_estimate"}:
                provenance = "operator_pinned"
                corrections[str(role)]["gain_db"] = gain
                pinned_gain_roles.add(str(role))
            else:
                estimated_gains[str(role)] = gain
            gain_provenance[str(role)] = provenance

    # Interim datasheet trim. When research declares no explicit gain_offset_db
    # for a driver but the sensitivities are known, attenuate the hotter drivers
    # down to the least-sensitive (reference) driver so a high-sensitivity
    # compression/horn driver can never start at full level relative to the
    # woofer (the shrill / horn-dominant failure mode, and a diaphragm hazard).
    # These are computed but NOT yet committed: a usable MEASURED phone level
    # match overrides them below, falling back to this datasheet estimate (marked
    # provisional) when no measurement is available.
    datasheet_trims: dict[str, float] = {}
    derivable_roles = [
        role for role in sensitivities if role not in pinned_gain_roles
    ]
    if len(sensitivities) >= 2 and derivable_roles:
        reference_db = min(sensitivities.values())
        for role in derivable_roles:
            trim_db = reference_db - sensitivities[role]  # <= 0 by construction
            if trim_db >= -_SENSITIVITY_TRIM_EPS_DB:
                continue  # reference driver and ties stay at unity
            datasheet_trims[role] = max(round(trim_db, 1), _MAX_ATTENUATION_DB)

    # MEASURED refinement overrides research, UI-suggested, and sensitivity
    # estimates. Manual tuning keeps an operator pin authoritative. Automatic
    # tuning is an explicit replacement operation, so its measured result wins
    # over the old manual pins (measured > pin > estimate > sensitivity).
    measured_trims, level_match = _measured_level_trims(preset, measurements)
    if level_match.get("incomparable_groups"):
        issues.append(_issue(
            "warning",
            "driver_measurement_comparison_incomparable",
            (
                "saved driver captures do not share verified placement, "
                "microphone, level, and excitation evidence; JTS kept the safe "
                "existing or estimated trim and needs new guided captures"
            ),
        ))
    if tuning_owner == "manual" and pinned_gain_roles and measured_trims:
        level_match["applied"] = False
        level_match["skipped_reason"] = "operator_pinned_gain"
        measured_trims = {}

    sources: dict[str, str] = {}
    measured_notes: list[str] = []
    estimate_notes: list[str] = []
    datasheet_notes: list[str] = []
    for role in corrections:
        if tuning_owner == "automatic" and role in measured_trims:
            corrections[role]["gain_db"] = measured_trims[role]
            sources[role] = "measured"
            measured_notes.append(f"{role} {measured_trims[role]:.1f} dB")
        elif role in pinned_gain_roles:
            sources[role] = "operator_pinned"
        elif role in measured_trims:
            corrections[role]["gain_db"] = measured_trims[role]
            sources[role] = "measured"
            measured_notes.append(f"{role} {measured_trims[role]:.1f} dB")
        elif role in estimated_gains:
            corrections[role]["gain_db"] = estimated_gains[role]
            sources[role] = "estimate"
            estimate_notes.append(f"{role} {estimated_gains[role]:.1f} dB")
        elif role in datasheet_trims:
            corrections[role]["gain_db"] = datasheet_trims[role]
            sources[role] = "sensitivity"
            datasheet_notes.append(f"{role} {datasheet_trims[role]:.1f} dB")
        else:
            sources[role] = "none"
    level_match["applied"] = bool(measured_notes)

    if measured_notes:
        issues.append(_issue(
            "info",
            "driver_gain_derived_from_measurement",
            (
                "applied a measured phone level match ("
                + ", ".join(measured_notes)
                + ")"
            ),
        ))
    if datasheet_notes:
        issues.append(_issue(
            "warning",
            "driver_gain_derived_from_sensitivity",
            (
                "applied an interim level trim from the sensitivity gap ("
                + ", ".join(datasheet_notes)
                + "); confirm against measurement before final tuning"
            ),
        ))
    if estimate_notes:
        issues.append(_issue(
            "warning",
            "driver_gain_from_unmeasured_estimate",
            (
                "applied an interim suggested driver trim ("
                + ", ".join(estimate_notes)
                + "); confirm against measurement before final tuning"
            ),
        ))
    provisional = any(
        source in {"estimate", "sensitivity"} for source in sources.values()
    )
    if provisional:
        issues.append(_issue(
            "warning",
            "baseline_level_match_provisional",
            (
                "per-driver level match is an unmeasured estimate; run the "
                "guided phone level-match to measure it"
            ),
        ))

    latest_summed = measurements.get("latest_summed_by_group")
    summed_records = [
        item for item in (
            latest_summed.values() if isinstance(latest_summed, Mapping) else []
        )
        if isinstance(item, Mapping)
    ]
    if len(summed_records) > 1:
        issues.append(_issue(
            "warning",
            "group_specific_delay_not_applied",
            "stereo/group-specific delay and polarity evidence is saved but not emitted yet",
        ))
        summed_records = []
    for summed in summed_records:
        delay_ms = _finite_float(summed.get("delay_ms"))
        delay_target = str(summed.get("delay_target_role") or "").strip().lower()
        if delay_ms is not None and delay_target in corrections:
            corrections[delay_target]["delay_ms"] = max(0.0, min(delay_ms, 20.0))
        polarity = str(summed.get("polarity") or "normal").strip().lower()
        if polarity.startswith("invert_"):
            role = polarity.removeprefix("invert_")
            if role in corrections:
                corrections[role]["inverted"] = True
    meta = {
        "sources": sources,
        "gain_provenance": gain_provenance,
        "provisional": provisional,
        "level_match": level_match,
    }
    return corrections, issues, meta


def _blocked_payload(
    *,
    topology: OutputTopology,
    source: Mapping[str, Any],
    issues: list[dict[str, str]],
    status: str = "blocked",
    config_path: Path,
    playback_device: str | None,
    playback_device_source: str,
) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": status,
        "baseline_id": f"baseline-{_safe_id(topology.topology_id)}",
        "created_at": None,
        "updated_at": None,
        "source": dict(source),
        "config": {
            "path": str(config_path),
            "basename": config_path.name,
            "exists": config_path.exists(),
            "playback_device": playback_device,
            "playback_device_source": playback_device_source,
        },
        "verification": {},
        "corrections": {},
        "corrections_source": {},
        "gain_provenance": {},
        "level_match": {"groups_total": 0, "groups_measured": 0, "applied": False},
        "provisional": False,
        "validation": {"status": "skipped", "reason": status},
        "permissions": {
            "may_compile": False,
            "may_apply": False,
            "may_not_emit_audio": True,
            "loads_camilla_on_apply": True,
        },
        "safety": {
            "no_audio": True,
            "compile_loads_camilla": False,
            "apply_requires_explicit_action": True,
            "volume_limit_db_max": 0.0,
            "positive_gain_allowed": False,
        },
        "issues": issues,
    }


def _apply_handoff_issue(playback_device_source: str) -> dict[str, str] | None:
    if playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE:
        return None
    return _issue(
        "blocker",
        "baseline_output_handoff_not_supported",
        (
            "active profile YAML can be compiled, but applying it is disabled "
            "until outputd owns this DAC handoff"
        ),
    )


def _load_saved_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("artifact_schema_version") != SCHEMA_VERSION
        or raw.get("kind") != BASELINE_PROFILE_KIND
    ):
        return None
    return raw


def _applied_profile_anchor(
    saved: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Current or retained applied profile behind a mutable candidate state."""
    if not isinstance(saved, Mapping):
        return None
    if saved.get("status") == "applied":
        return saved
    prior = saved.get("applied_recomposition_profile")
    if isinstance(prior, Mapping) and prior.get("status") == "applied":
        return prior
    return None


def _frozen_applied_profile(
    saved: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Small immutable record sufficient to recompose the running Layer A."""
    applied = _applied_profile_anchor(saved)
    if applied is None:
        return None
    return {
        "artifact_schema_version": applied.get("artifact_schema_version"),
        "kind": applied.get("kind"),
        "status": "applied",
        "baseline_id": applied.get("baseline_id"),
        "applied_at": applied.get("applied_at"),
        "source": dict(applied.get("source") or {}),
        "config": dict(applied.get("config") or {}),
        "corrections": dict(applied.get("corrections") or {}),
        "corrections_source": dict(applied.get("corrections_source") or {}),
        "gain_provenance": dict(applied.get("gain_provenance") or {}),
        "level_match": dict(applied.get("level_match") or {}),
        "tuning_owner": str(applied.get("tuning_owner") or ""),
        # Quality state belongs to the immutable applied anchor too.  Dropping
        # it here lets an older sensitivity-only profile masquerade as a
        # measured profile on every consumer of the frozen view.
        "provisional": bool(applied.get("provisional")),
        "recomposition_snapshot": (
            dict(applied["recomposition_snapshot"])
            if isinstance(applied.get("recomposition_snapshot"), Mapping)
            else None
        ),
    }


def load_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read one validated baseline artifact without deriving fresh evidence."""
    return _load_saved_state(baseline_profile_state_path(path))


def load_applied_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the applied Layer-A SSOT, even while a new candidate is staged."""
    return _frozen_applied_profile(load_baseline_profile_state(path))


def _revalidation_payload(
    saved: Mapping[str, Any] | None,
    current_source: Mapping[str, Any],
    *,
    status: str,
    issues: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Describe whether a previously applied profile is stale.

    ``build_baseline_profile_candidate`` deliberately re-derives readiness from
    current evidence instead of trusting the saved JSON. When that re-derivation
    invalidates a profile that had already been applied, keep that fact visible:
    the household needs a "revalidate" path, not a mysterious blocked profile.
    """

    saved = _applied_profile_anchor(saved)
    if saved is None:
        return {"required": False, "status": "not_required"}
    saved_source = (
        saved.get("source") if isinstance(saved.get("source"), Mapping) else {}
    )
    saved_fingerprint = saved_source.get("fingerprint")
    current_fingerprint = current_source.get("fingerprint")
    if not saved_fingerprint or saved_fingerprint == current_fingerprint:
        return {"required": False, "status": "not_required"}

    issue_codes = {
        str(issue.get("code") or "")
        for issue in (issues or [])
        if isinstance(issue, Mapping)
    }
    if issue_codes == {"baseline_summed_validation_missing"}:
        next_step = "combined_check"
        message = (
            "active speaker setup changed after this profile was applied; "
            "re-run the combined crossover check, then save and apply a fresh profile"
        )
    elif status in {"ready_to_compile", "ready_to_apply", "compiled_apply_blocked"}:
        next_step = "save_profile" if status == "ready_to_compile" else "apply_profile"
        message = (
            "active speaker revalidation is saved; save and apply a fresh profile"
        )
    else:
        next_step = "setup_checks"
        message = (
            "active speaker setup changed after this profile was applied; "
            "finish the highlighted setup checks, then save and apply a fresh profile"
        )

    changed = [
        key
        for key in (
            "topology_fingerprint",
            "design_draft_updated_at",
            "crossover_preview_updated_at",
            "crossover_preview_fingerprint",
            "measurements_updated_at",
            "measurement_summary_fingerprint",
        )
        if saved_source.get(key) != current_source.get(key)
    ]
    saved_config = (
        saved.get("config") if isinstance(saved.get("config"), Mapping) else {}
    )
    saved_config_path = str(saved_config.get("path") or "")
    return {
        "required": True,
        "status": "required",
        "reason": "applied_profile_superseded",
        "next_step": next_step,
        "message": message,
        "changed": changed,
        "applied_at": saved.get("applied_at"),
        "applied_source_fingerprint": saved_fingerprint,
        "current_source_fingerprint": current_fingerprint,
        "superseded_profile": {
            "status": saved.get("status"),
            "updated_at": saved.get("updated_at"),
            "applied_at": saved.get("applied_at"),
            "config": {
                "path": saved_config_path or None,
                "basename": saved_config.get("basename"),
                "exists": bool(saved_config_path) and Path(saved_config_path).exists(),
            },
        },
    }


def _summed_validation_evidence_complete(summary: Mapping[str, Any]) -> bool:
    if summary.get("summed_validation_complete"):
        return True
    required = int(summary.get("required_summed_group_count") or 0)
    validated = int(summary.get("validated_summed_group_count") or 0)
    missing = summary.get("missing_summed_targets")
    return (
        required > 0
        and validated >= required
        and isinstance(missing, list)
        and not missing
    )


def _crossover_preview_ready(crossover_preview: Mapping[str, Any]) -> bool:
    """True when the saved crossover preview is a fresh, staging-ready artifact.

    The single source of the preview-readiness gate, shared by
    :func:`build_baseline_profile_candidate` and the mutable-evidence preview
    helper :func:`recompose_baseline_yaml` so those candidate paths cannot drift
    on what "ready" means. Production EQ recompose reads the applied snapshot.
    """
    return (
        crossover_preview.get("kind") == "jts_active_speaker_crossover_preview"
        and crossover_preview.get("status") == "ready_for_protected_staging"
        and bool(
            (crossover_preview.get("permissions") or {}).get(
                "may_prepare_protected_startup_config"
            )
        )
    )


def build_baseline_profile_candidate(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    write: bool = False,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    playback_device: str | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    driver_domain: bool = False,
    program_channel: str | None = None,
    driver_domain_pair_trim_db: float = 0.0,
    tuning_owner: str = "manual",
    preserved_applied_profile: Mapping[str, Any] | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build or write a baseline candidate from current accepted evidence.

    ``capture_device`` is the CamillaDSP capture source the emitted baseline
    reads from. The default (``DEFAULT_CAPTURE_DEVICE`` = ``plug:jasper_capture``,
    the solo fan-in tap) keeps the solo baseline byte-identical; the multiroom
    reconciler passes a round-trip loopback device for a wireless follower (gap 1
    of ``docs/HANDOFF-distributed-active.md``). The graph shape — crossover,
    per-driver limiters, tweeter high-pass, 0 dB ceiling — is unaffected; only
    the capture source line changes.

    ``driver_domain`` switches the emit to the **driver-domain-only** graph
    (``emit_active_speaker_driver_domain_config``, Slice 2): a wireless active
    follower's Layer A — ``channel_select (pick L/R/mono) -> split -> per-driver
    crossover/limiter`` — with **no** program-domain headroom and **no**
    preference EQ (the leader baked Layer B/C into the streamed program). It
    requires ``program_channel`` (one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``: the
    inter-speaker channel this box plays). ``driver_domain_pair_trim_db`` is the
    attenuate-only pair-balance trim for this member, applied after
    channel-select and before the driver split; default zero keeps the full solo
    baseline emit byte-identical (invariant 7). The reconciler's active-member
    branches pass ``driver_domain=True`` + ``program_channel`` + the loopback
    ``capture_device``, writing to role-specific ``config_path`` / ``state_path``
    so the solo baseline artifacts are never clobbered.
    """
    if tuning_owner not in TUNING_OWNERS:
        raise ValueError(f"unsupported crossover tuning owner: {tuning_owner!r}")
    if driver_domain and program_channel not in DRIVER_DOMAIN_PROGRAM_CHANNELS:
        raise ValueError(
            "driver_domain requires program_channel in "
            f"{DRIVER_DOMAIN_PROGRAM_CHANNELS}, not {program_channel!r}"
        )

    state_target = baseline_profile_state_path(state_path)
    config_target = baseline_config_path(config_path)
    now = created_at or _utc_now()
    source = _source_payload(topology, design_draft, crossover_preview, measurements)
    resolved_playback_device, playback_device_source = (
        resolve_active_playback_device(
            topology,
            playback_device=playback_device,
        )
    )
    route_capability = active_playback_route_capability(
        topology,
        playback_device=playback_device,
    )
    saved = _load_saved_state(state_target)
    applied_anchor = _applied_profile_anchor(saved)
    applied_config = (
        applied_anchor.get("config")
        if isinstance(applied_anchor, Mapping)
        and isinstance(applied_anchor.get("config"), Mapping)
        else {}
    )
    applied_config_path = str(applied_config.get("path") or "")
    if applied_anchor is not None and applied_config_path == str(config_target):
        # Never overwrite the file the running/statefile-applied graph may still
        # read. A candidate gets a content-addressed sibling and becomes durable
        # only when the explicit apply repoints CamillaDSP to it.
        config_target = config_target.with_name(
            f"{config_target.stem}_candidate_{source['fingerprint'][:12]}"
            f"{config_target.suffix}"
        )
    retained_applied = _frozen_applied_profile(applied_anchor)

    def finalize(payload: dict[str, Any]) -> dict[str, Any]:
        if retained_applied is not None:
            payload["applied_recomposition_profile"] = retained_applied
        payload["revalidation"] = _revalidation_payload(
            saved,
            source,
            status=str(payload.get("status") or ""),
            issues=[
                issue for issue in payload.get("issues", [])
                if isinstance(issue, Mapping)
            ],
        )
        return payload

    if (
        not write
        and saved
        and isinstance(saved.get("source"), Mapping)
        and saved["source"].get("fingerprint") == source["fingerprint"]
        and str(saved.get("tuning_owner") or "manual") == tuning_owner
        and Path(str((saved.get("config") or {}).get("path") or "")).exists()
    ):
        out = dict(saved)
        out["config"] = dict(out.get("config") or {})
        out["config"]["exists"] = True
        issues = [
            issue for issue in out.get("issues", [])
            if isinstance(issue, dict)
            and issue.get("code") != "baseline_output_handoff_not_supported"
        ]
        handoff_issue = _apply_handoff_issue(
            str(out["config"].get("playback_device_source") or playback_device_source)
        )
        if handoff_issue:
            issues.append(handoff_issue)
            if out.get("status") == "ready_to_apply":
                out["status"] = "compiled_apply_blocked"
        out["issues"] = issues
        out["permissions"] = dict(out.get("permissions") or {})
        out["permissions"]["may_apply"] = out.get("status") == "ready_to_apply"
        out["permissions"]["may_compile"] = out.get("status") in {
            "ready_to_compile",
            "ready_to_apply",
            "compiled_apply_blocked",
        }
        return finalize(out)

    issues: list[dict[str, str]] = []
    summary = measurements.get("summary") if isinstance(measurements.get("summary"), Mapping) else {}
    driver_target_proof_complete = bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    driver_target_proof_source = (
        "measurements" if driver_target_proof_complete else "missing"
    )
    summed_validation_complete = bool(summary.get("summed_validation_complete"))
    # A passive-mains + local-subwoofer topology has NO inter-driver crossover, so
    # it never produces an active crossover preview and has no per-driver / summed
    # active-crossover measurements to complete. It is still roleful (bass
    # management splits the program), so it rides the SAME multi-output emitter as
    # the active path — but via a degenerate 1-way preset built directly from the
    # topology, skipping the preview-readiness and active-measurement gates that
    # only apply to a real active crossover. A SUBLESS passive speaker never
    # reaches here (it takes the flat emit_sound_config lane).
    passive_sub = topology_is_passive_mains_with_sub(topology)

    preset: ActiveSpeakerPreset | None = None
    preset_gates: list[dict[str, Any]] = []
    if passive_sub:
        if not resolved_playback_device:
            issues.append(_issue(
                "blocker",
                "baseline_playback_device_missing",
                "active profile compiler needs an explicit active playback device",
            ))
        for issue in route_capability.issues:
            if issue.get("code") == "active_playback_route_too_narrow":
                issues.append(issue)
        preset, preset_issues, preset_gates = _passive_mains_with_sub_preset(topology)
        issues.extend(preset_issues)
    else:
        preview_ready = _crossover_preview_ready(crossover_preview)
        if not preview_ready:
            issues.append(_issue(
                "blocker",
                "baseline_crossover_preview_not_ready",
                "save a fresh crossover preview before compiling an active profile",
            ))
        if not resolved_playback_device:
            issues.append(_issue(
                "blocker",
                "baseline_playback_device_missing",
                "active profile compiler needs an explicit active playback device",
            ))
        for issue in route_capability.issues:
            if issue.get("code") == "active_playback_route_too_narrow":
                issues.append(issue)
        # A routed local subwoofer compiles through the SAME multi-output emitter as
        # the mains: the preset builder (compile_preset_from_crossover_preview below)
        # resolves the sub lane onto the preset fail-closed — when it cannot pin the
        # sub to a safe contiguous output it returns a blocker instead of emitting a
        # full-range sub feed. The emitted graph is then re-proven structurally by
        # classify_camilla_graph + CamillaDSP --check, so there is no separate
        # subwoofer-not-supported gate here.
        if preview_ready:
            preset, preset_issues, preset_gates = compile_preset_from_crossover_preview(
                topology,
                dict(crossover_preview),
            )
            issues.extend(preset_issues)
        if not driver_target_proof_complete:
            probe_status = "ready_to_compile" if not issues else "blocked"
            revalidation_for_driver_proof = _revalidation_payload(
                saved,
                source,
                status=probe_status,
                issues=issues,
            )
            if applied_profile_revalidation_satisfies_driver_target_proof(
                revalidation_for_driver_proof
            ):
                driver_target_proof_complete = True
                driver_target_proof_source = "applied_profile_revalidation"
        if not driver_target_proof_complete:
            issues.append(_issue(
                "blocker",
                "baseline_driver_measurements_missing",
                "confirm each driver with a quiet test before saving the active profile",
            ))
        summed_validation_complete = (
            bool(summary.get("summed_validation_complete"))
            or (
                driver_target_proof_complete
                and _summed_validation_evidence_complete(summary)
            )
        )
        if not summed_validation_complete:
            issues.append(_issue(
                "blocker",
                "baseline_summed_validation_missing",
                "validate the combined crossover before saving the active profile",
            ))
    if issues:
        return finalize(_blocked_payload(
            topology=topology,
            source=source,
            issues=issues,
            status="blocked",
            config_path=config_target,
            playback_device=resolved_playback_device,
            playback_device_source=playback_device_source,
        ))
    if preset is None or resolved_playback_device is None:
        return finalize(_blocked_payload(
            topology=topology,
            source=source,
            issues=[
                _issue(
                    "blocker",
                    "baseline_preset_unavailable",
                    "active profile compiler could not build speaker preset intent",
                )
            ],
            status="blocked",
            config_path=config_target,
            playback_device=resolved_playback_device,
            playback_device_source=playback_device_source,
        ))

    preservation = None
    if preserved_applied_profile is not None:
        preservation = legacy_manual_preservation_state(
            preserved_applied_profile,
            current_source_fingerprint=str(source.get("fingerprint") or ""),
        )
        if not preservation["ready"]:
            return finalize(_blocked_payload(
                topology=topology,
                source=source,
                issues=[_issue(
                    "blocker",
                    str(preservation["reason"]),
                    str(preservation["detail"]),
                )],
                status="blocked",
                config_path=config_target,
                playback_device=resolved_playback_device,
                playback_device_source=playback_device_source,
            ))
        if not isinstance(preserved_applied_profile.get("corrections"), Mapping):
            return finalize(_blocked_payload(
                topology=topology,
                source=source,
                issues=[_issue(
                    "blocker",
                    "preserved_manual_corrections_missing",
                    "the applied manual crossover has no corrections to preserve",
                )],
                status="blocked",
                config_path=config_target,
                playback_device=resolved_playback_device,
                playback_device_source=playback_device_source,
            ))

    corrections, correction_issues, correction_meta = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
        tuning_owner=tuning_owner,
    )
    issues.extend(correction_issues)
    if preserved_applied_profile is not None:
        preserved_corrections = (
            preserved_applied_profile.get("corrections")
            if isinstance(preserved_applied_profile.get("corrections"), Mapping)
            else None
        )
    else:
        preserved_corrections = None
    if preserved_corrections is not None:
        normalized: dict[str, dict[str, float | bool]] = {}
        for role in required_driver_roles(preset.way_count):
            raw = preserved_corrections.get(role)
            gain = _finite_float(raw.get("gain_db")) if isinstance(raw, Mapping) else None
            delay = _finite_float(raw.get("delay_ms")) if isinstance(raw, Mapping) else None
            inverted = raw.get("inverted") if isinstance(raw, Mapping) else None
            if (
                gain is None
                or gain > 0.0
                or gain < _MAX_ATTENUATION_DB
                or delay is None
                or not 0.0 <= delay <= 20.0
                or not isinstance(inverted, bool)
            ):
                issues.append(_issue(
                    "blocker",
                    "preserved_manual_correction_invalid",
                    f"the applied manual correction for {role} is incomplete or unsafe",
                ))
                continue
            normalized[role] = {
                "gain_db": gain,
                "delay_ms": delay,
                "inverted": inverted,
            }
        if len(normalized) == len(required_driver_roles(preset.way_count)):
            corrections = normalized
            correction_meta["sources"] = {
                role: "operator_pinned" for role in normalized
            }
            correction_meta["gain_provenance"] = {
                role: "operator_pinned" for role in normalized
            }
            correction_meta["provisional"] = False
            correction_meta["level_match"] = {
                "groups_total": 0,
                "groups_measured": 0,
                "deltas": [],
                "comparison": "preserved_applied_manual_profile",
                "incomparable_groups": [],
                "applied": False,
            }
            issues.append(_issue(
                "info",
                "manual_crossover_preserved",
                "preserved the currently applied manual crossover corrections",
            ))
    automatic_candidate = automatic_candidate_readiness(
        required_group_ids=(
            group.id
            for group in topology.speaker_groups
            if group.mode in {"active_2_way", "active_3_way"}
        ),
        level_match=correction_meta["level_match"],
        measurement_summary=summary,
        active_comparison_set=measurements.get("active_comparison_set"),
    )
    if tuning_owner == "automatic" and not automatic_candidate["ready"]:
        issues.append(_issue(
            "blocker",
            str(automatic_candidate["reason"]),
            str(automatic_candidate["detail"]),
        ))
    provisional = bool(correction_meta.get("provisional"))
    validation = {"status": "skipped", "reason": "not_written"}
    if write:
        config_target.parent.mkdir(parents=True, exist_ok=True)
        if driver_domain:
            assert program_channel is not None  # validated above
            yaml = emit_active_speaker_driver_domain_config(
                preset,
                playback_device=resolved_playback_device,
                program_channel=program_channel,
                pair_trim_db=driver_domain_pair_trim_db,
                corrections=corrections,
                capture_device=capture_device,
                capture_format=capture_format,
                out_path=config_target,
                baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
            )
        else:
            yaml = emit_active_speaker_baseline_config(
                preset,
                playback_device=resolved_playback_device,
                corrections=corrections,
                capture_device=capture_device,
                capture_format=capture_format,
                out_path=config_target,
                baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
            )
        validation = validate(config_target).to_dict()
        if not validation.get("ok_to_apply") and validation.get("status") not in {
            "valid",
            "missing",
        }:
            issues.append(_issue(
                "blocker",
                "baseline_config_validation_failed",
                "generated active profile did not pass CamillaDSP validation",
            ))
        config_sha256 = hashlib.sha256(yaml.encode("utf-8")).hexdigest()
        status = "ready_to_apply" if not any(
            issue["severity"] == "blocker" for issue in issues
        ) else "blocked"
        handoff_issue = _apply_handoff_issue(playback_device_source)
        if status == "ready_to_apply" and handoff_issue:
            issues.append(handoff_issue)
            status = "compiled_apply_blocked"
    else:
        config_sha256 = None
        status = "ready_to_compile"

    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": status,
        "baseline_id": f"baseline-{_safe_id(topology.topology_id)}",
        "created_at": (
            saved.get("created_at") if saved and saved.get("created_at") else now
        ),
        "updated_at": now if write else None,
        "source": source,
        "preset": {
            "preset_id": preset.preset_id,
            "name": preset.name,
            "way_count": preset.way_count,
            "channel_map": preset.channel_map.to_dict(),
            "gates": preset_gates,
        },
        "config": {
            "path": str(config_target),
            "basename": config_target.name,
            "exists": config_target.exists(),
            "sha256": config_sha256,
            "playback_device": resolved_playback_device,
            "playback_device_source": playback_device_source,
            # "driver" = a wireless follower's Layer-A-only graph (no B/C);
            # "full" = the solo baseline (B/C + A). Observability only.
            "domain": "driver" if driver_domain else "full",
            "program_channel": program_channel if driver_domain else None,
        },
        "verification": {
            "driver_measurements_complete": bool(
                summary.get("driver_measurements_complete")
            ),
            "driver_target_proof_complete": driver_target_proof_complete,
            "driver_target_proof_source": driver_target_proof_source,
            "summed_validation_complete": summed_validation_complete,
            "captured_driver_count": summary.get("captured_driver_count", 0),
            "validated_summed_group_count": summary.get(
                "validated_summed_group_count",
                0,
            ),
        },
        "corrections": corrections,
        "corrections_source": correction_meta["sources"],
        "gain_provenance": correction_meta["gain_provenance"],
        "level_match": correction_meta["level_match"],
        "automatic_candidate": automatic_candidate,
        "tuning_owner": tuning_owner,
        # An unmeasured per-driver trim is explicitly provisional. Surfaced in
        # /state + the wizard so a household knows to run the guided level-match;
        # the speaker is safe (attenuation-only) either way.
        "provisional": provisional,
        "validation": validation,
        "permissions": {
            "may_compile": status in {
                "ready_to_compile",
                "ready_to_apply",
                "compiled_apply_blocked",
            },
            "may_apply": status == "ready_to_apply",
            "may_not_emit_audio": True,
            "loads_camilla_on_apply": True,
        },
        "safety": {
            "no_audio": True,
            "compile_loads_camilla": False,
            "apply_requires_explicit_action": True,
            "volume_limit_db_max": 0.0,
            "positive_gain_allowed": False,
            "per_driver_limiters": True,
        },
        "issues": issues,
        # Immutable Layer-A inputs captured at Save. Once this candidate is
        # explicitly applied, every production recompose reads ONLY this
        # snapshot; later measurement/design edits remain candidates and cannot
        # alter playback as a side effect of applying room/preference EQ.
        "recomposition_snapshot": {
            "schema_version": 1,
            "topology_id": topology.topology_id,
            "topology_fingerprint": source["topology_fingerprint"],
            "preset": preset.to_dict(),
            "corrections": corrections,
            "corrections_source": correction_meta["sources"],
            "gain_provenance": correction_meta["gain_provenance"],
            "level_match": correction_meta["level_match"],
            "tuning_owner": tuning_owner,
            "playback_device": resolved_playback_device,
            "domain": "driver" if driver_domain else "full",
        },
    }
    payload = finalize(payload)
    if write:
        atomic_write_text(
            state_target,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
    return payload


def recompose_applied_baseline_yaml(
    topology: OutputTopology,
    *,
    applied_profile: Mapping[str, Any],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    out_path: str | Path | None = None,
    capture_pipe_path: str | None = None,
    resampler_type: str | None = None,
    resampler_profile: str = DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit Layer A strictly from the immutable applied-profile snapshot.

    This is the production graph-carrier seam. Mutable design drafts,
    crossover previews, and measurement stores are deliberately not parameters:
    captures remain candidates until :func:`apply_baseline_profile` snapshots
    them under an explicit Apply transaction.
    """
    if applied_profile.get("status") != "applied":
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_unavailable",
            "the saved active-speaker profile is not an applied profile",
        )]
    snapshot = applied_profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 1:
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_unavailable",
            (
                "the applied active-speaker profile predates immutable "
                "recomposition; apply it again before adding EQ"
            ),
        )]
    if snapshot.get("domain") != "full":
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_domain_invalid",
            "only a full solo active-speaker profile can host room or preference EQ",
        )]
    if (
        snapshot.get("topology_id") != topology.topology_id
        or snapshot.get("topology_fingerprint")
        != topology_config_fingerprint(topology)
    ):
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_topology_stale",
            (
                "the applied active-speaker profile belongs to a different "
                "output topology; reapply speaker setup first"
            ),
        )]
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    except (ActiveSpeakerConfigError, TypeError, ValueError) as exc:
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_invalid",
            f"the applied active-speaker snapshot is invalid: {exc}",
        )]
    corrections = snapshot.get("corrections")
    playback_device = snapshot.get("playback_device")
    expected_roles = set(required_driver_roles(preset.way_count))
    correction_roles = set(corrections) if isinstance(corrections, Mapping) else set()
    corrections_valid = (
        correction_roles == expected_roles
        and all(isinstance(value, Mapping) for value in corrections.values())
    ) if isinstance(corrections, Mapping) else False
    if (
        not corrections_valid
        or not isinstance(playback_device, str)
        or not playback_device
    ):
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_invalid",
            "the applied active-speaker snapshot is missing corrections or playback device",
        )]
    yaml = emit_active_speaker_baseline_config(
        preset,
        playback_device=playback_device,
        corrections={str(role): dict(value) for role, value in corrections.items()},
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
        baseline_id=str(
            applied_profile.get("baseline_id")
            or f"baseline-{_safe_id(topology.topology_id)}"
        ),
        capture_pipe_path=capture_pipe_path,
        resampler_type=resampler_type,
        resampler_profile=resampler_profile,
    )
    return yaml, []


def recompose_baseline_yaml(
    topology: OutputTopology,
    *,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    playback_device: str | None = None,
    out_path: str | Path | None = None,
    capture_pipe_path: str | None = None,
    resampler_type: str | None = None,
    resampler_profile: str = DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit the active-speaker baseline YAML for the current accepted
    evidence, with optional program-domain room PEQ / preference EQ inserted
    pre-split.

    This is the candidate/debug composition seam retained for callers that are
    deliberately previewing current mutable evidence. Production graph
    recomposition uses :func:`recompose_applied_baseline_yaml` instead. This
    helper rebuilds the SAME structural baseline from the supplied evidence — reusing the
    exact derivation primitives :func:`build_baseline_profile_candidate` uses
    (``resolve_active_playback_device`` → ``compile_preset_from_crossover_preview``
    → ``_derive_corrections`` → ``emit_active_speaker_baseline_config``) — rather
    than parsing the running config (the explicit anti-pattern). Only the
    ``room_peqs``, ``preference_filters`` (and the explicit ``output_trim_db``
    attenuation) differ from the durable baseline; the crossover, per-driver
    limiters, tweeter high-pass, and 0 dB ceiling are identical, so the emitted
    YAML re-proves as ``GRAPH_APPROVED_ACTIVE_RUNTIME``.

    ``room_peqs`` are preserved room-correction filters. They run pre-split on
    channels [0, 1], and their positive-boost headroom is folded into
    ``active_baseline_headroom`` rather than emitted as a second program-domain
    gain.

    ``output_trim_db`` is the household's manual headroom + loudness-match
    attenuation; the emitter folds it into ``active_baseline_headroom`` so the
    active EQ apply honours it exactly like the stereo path.

    Unlike :func:`build_baseline_profile_candidate` /
    :func:`apply_baseline_profile`, this re-emit takes no ``capture_device``: it
    inserts program-domain (Layer C) preference EQ, which only ever runs on the
    fan-in-fed program domain — a solo speaker's single graph and a pair
    leader's bake instance (``camilla#1``). A wireless follower (and a leader's
    own-driver instance, ``camilla#2``) is Layer-A-only and never recomposes
    preference EQ, so this seam always captures from the fan-in program tap. The
    role-varying capture (the round-trip loopback) belongs to the driver-domain
    emit on build/apply, where ``capture_device`` lives.

    The fan-in program tap is either the default ALSA snd-aloop capture
    (``capture_pipe_path`` unset — byte-identical to today) OR a legacy
    File-capture lane (``capture_pipe_path`` + ``resampler_type`` set, threaded
    from the graph carrier). Either way Layer A is rebuilt from the canonical
    evidence and unchanged; only the program-domain capture block differs.
    ``enable_rate_adjust`` is intentionally NOT a parameter — the active graph
    hardcodes it true, so a File capture additionally needs the async resampler.

    **Gate scope (intentionally a subset of the candidate builder).** This only
    re-checks what it needs to EMIT a structurally-valid baseline — playback
    device, preview-readiness (the shared :func:`_crossover_preview_ready`
    predicate), and a compilable preset. It deliberately does NOT re-run the
    candidate builder's *readiness/quality* gates (driver-measurement /
    summed-validation completeness, route width, subwoofer-block). It is not a
    production apply authorization; callers must validate the emitted graph.

    Returns ``(yaml, [])`` on success (writing to ``out_path`` when given, at the
    same group-readable mode the emitter uses), or ``(None, issues)`` when the
    evidence can no longer produce a baseline (preview not ready, preset
    uncompilable, no playback device) so the carrier can refuse with a typed
    reason instead of emitting a partial graph. It does NOT validate against
    CamillaDSP or touch the durable baseline state — the caller (the DSP-apply
    transaction) owns validation.
    """
    resolved_device, _device_source = resolve_active_playback_device(
        topology,
        playback_device=playback_device,
    )
    if not resolved_device:
        return None, [_issue(
            "blocker",
            "baseline_playback_device_missing",
            "active profile compiler needs an explicit active playback device",
        )]
    if not _crossover_preview_ready(crossover_preview):
        return None, [_issue(
            "blocker",
            "baseline_crossover_preview_not_ready",
            "save a fresh crossover preview before re-emitting the active baseline",
        )]
    preset, preset_issues, _gates = compile_preset_from_crossover_preview(
        topology,
        dict(crossover_preview),
    )
    blockers = [i for i in preset_issues if i.get("severity") == "blocker"]
    if preset is None or blockers:
        return None, (blockers or [_issue(
            "blocker",
            "baseline_preset_unavailable",
            "active profile compiler could not build speaker preset intent",
        )])
    corrections, _correction_issues, _correction_meta = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
    )
    yaml = emit_active_speaker_baseline_config(
        preset,
        playback_device=resolved_device,
        corrections=corrections,
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
        baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
        capture_pipe_path=capture_pipe_path,
        resampler_type=resampler_type,
        resampler_profile=resampler_profile,
    )
    return yaml, []


async def apply_baseline_profile(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]] | None = None,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    driver_domain: bool = False,
    program_channel: str | None = None,
    driver_domain_pair_trim_db: float = 0.0,
    tuning_owner: str = "manual",
    preserved_applied_profile: Mapping[str, Any] | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Apply the saved baseline candidate through the shared DSP transaction.

    ``capture_device`` is threaded to :func:`build_baseline_profile_candidate`
    so the reconciler can apply a follower's round-trip-loopback baseline; the
    default keeps the solo apply byte-identical.

    ``driver_domain`` + ``program_channel`` switch the emit to a wireless active
    follower's driver-domain-only Layer-A graph (Slice 2 emitter). The optional
    ``driver_domain_pair_trim_db`` follows the same parameter on
    :func:`build_baseline_profile_candidate` so direct apply callers cannot drift
    from the candidate builder. The follower branch of the multiroom reconciler
    passes follower-specific ``state_path`` / ``config_path`` alongside these so
    the solo baseline state is not overwritten.
    """

    state_target = baseline_profile_state_path(state_path)
    candidate = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=crossover_preview,
        measurements=measurements,
        write=True,
        state_path=state_target,
        config_path=config_path,
        capture_device=capture_device,
        capture_format=capture_format,
        driver_domain=driver_domain,
        program_channel=program_channel,
        driver_domain_pair_trim_db=driver_domain_pair_trim_db,
        tuning_owner=tuning_owner,
        preserved_applied_profile=preserved_applied_profile,
        validate=validate,
    )
    snapshot_state = crossover_snapshot_state(
        candidate,
        expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=str(
            (candidate.get("source") or {}).get("topology_fingerprint") or ""
        ),
        expected_domain="driver" if driver_domain else "full",
        require_applied=False,
    )
    if candidate.get("permissions", {}).get("may_apply") and not snapshot_state["valid"]:
        candidate["status"] = "compiled_apply_blocked"
        candidate["permissions"]["may_apply"] = False
        candidate["issues"] = [
            *candidate.get("issues", []),
            _issue(
                "blocker",
                str(snapshot_state["reason"]),
                str(snapshot_state["detail"]),
            ),
        ]
        atomic_write_text(
            state_target,
            json.dumps(candidate, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
    if not candidate.get("permissions", {}).get("may_apply"):
        return {
            "status": "blocked",
            "profile": candidate,
            "apply": None,
            "issues": [
                *candidate.get("issues", []),
                _issue(
                    "blocker",
                    "baseline_profile_not_ready_to_apply",
                    "save a ready active profile before applying it",
                ),
            ],
        }

    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_baseline_apply",
            candidate_path=str((candidate.get("config") or {}).get("path")),
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            validate=validate,
        )
    except DspApplyError as exc:
        failed = {
            **candidate,
            "status": "apply_failed",
            "apply": exc.state.to_dict(),
            "updated_at": _utc_now(),
            "issues": [
                *candidate.get("issues", []),
                _issue(
                    "blocker",
                    "baseline_profile_apply_failed",
                    str(exc),
                ),
            ],
        }
        atomic_write_text(
            state_target,
            json.dumps(failed, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
        return {
            "status": "apply_failed",
            "profile": failed,
            "apply": exc.state.to_dict(),
            "issues": failed["issues"],
        }

    applied = {
        **candidate,
        "status": "applied",
        "applied_at": _utc_now(),
        "updated_at": _utc_now(),
        "apply": apply_state.to_dict(),
        "revalidation": {"required": False, "status": "not_required"},
    }
    # The newly applied profile is now the one SSOT; retaining the predecessor
    # would create two plausible Layer-A owners.
    applied.pop("applied_recomposition_profile", None)
    applied["permissions"] = dict(applied.get("permissions") or {})
    applied["permissions"]["may_apply"] = False
    atomic_write_text(
        state_target,
        json.dumps(applied, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
    )
    return {
        "status": "applied",
        "profile": applied,
        "apply": apply_state.to_dict(),
        "issues": applied.get("issues", []),
    }
