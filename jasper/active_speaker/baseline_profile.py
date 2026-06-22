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
    FilterSpec,
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
from .playback_route import (
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from .profile import ActiveSpeakerPreset, required_driver_roles
from .staging import compile_preset_from_crossover_preview

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
    topology_config_view = {
        key: value
        for key, value in topology.to_dict().items()
        if key != "pairing_intent"
    }
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": _fingerprint(topology_config_view),
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
    for group_id, group_records in sorted(by_group.items()):
        raw: dict[str, float] = {roles[0]: 0.0}
        group_deltas: list[dict[str, Any]] = []
        usable = True
        for region in regions:
            lo_role = region.lower_driver
            up_role = region.upper_driver
            fc = float(region.fc_hz)
            level_lo = _overlap_level_at(group_records.get(lo_role), fc)
            level_up = _overlap_level_at(group_records.get(up_role), fc)
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
        "deltas": deltas,
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
) -> tuple[dict[str, dict[str, float | bool]], list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    corrections: dict[str, dict[str, float | bool]] = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in required_driver_roles(preset.way_count)
    }
    drivers = crossover_preview.get("drivers")
    explicit_gain_roles: set[str] = set()
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
            corrections[str(role)]["gain_db"] = gain
            explicit_gain_roles.add(str(role))

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
        role for role in sensitivities if role not in explicit_gain_roles
    ]
    if len(sensitivities) >= 2 and derivable_roles:
        reference_db = min(sensitivities.values())
        for role in derivable_roles:
            trim_db = reference_db - sensitivities[role]  # <= 0 by construction
            if trim_db >= -_SENSITIVITY_TRIM_EPS_DB:
                continue  # reference driver and ties stay at unity
            datasheet_trims[role] = max(round(trim_db, 1), _MAX_ATTENUATION_DB)

    # MEASURED refinement: a usable phone near-field level match OVERRIDES the
    # datasheet trim. Only when there are no explicit operator gains — an explicit
    # gain_offset_db is the operator's deliberate choice and shifts the chain's
    # reference, so we never mix it with a measured chain (explicit > measured >
    # datasheet).
    measured_trims, level_match = _measured_level_trims(preset, measurements)
    if explicit_gain_roles and measured_trims:
        level_match["applied"] = False
        level_match["skipped_reason"] = "explicit_gain"
        measured_trims = {}

    sources: dict[str, str] = {}
    measured_notes: list[str] = []
    datasheet_notes: list[str] = []
    for role in corrections:
        if role in explicit_gain_roles:
            sources[role] = "explicit"
        elif role in measured_trims:
            corrections[role]["gain_db"] = measured_trims[role]
            sources[role] = "measured"
            measured_notes.append(f"{role} {measured_trims[role]:.1f} dB")
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
    provisional = any(source == "sensitivity" for source in sources.values())
    if provisional:
        issues.append(_issue(
            "warning",
            "baseline_level_match_provisional",
            (
                "per-driver level match is a datasheet estimate; run the guided "
                "phone level-match to measure it"
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


def _crossover_preview_ready(crossover_preview: Mapping[str, Any]) -> bool:
    """True when the saved crossover preview is a fresh, staging-ready artifact.

    The single source of the preview-readiness gate, shared by
    :func:`build_baseline_profile_candidate` (compile/apply) and
    :func:`recompose_baseline_yaml` (the carrier's EQ re-emit) so the two cannot
    drift on what "ready" means.
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
    inter-speaker channel this box plays). Default ``False`` keeps the full solo
    baseline emit byte-identical (invariant 7); the reconciler's follower branch
    passes ``driver_domain=True`` + ``program_channel`` + the loopback
    ``capture_device``, writing to a follower-specific ``config_path`` /
    ``state_path`` so the solo baseline artifacts are never clobbered.
    """
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
    if (
        not write
        and saved
        and isinstance(saved.get("source"), Mapping)
        and saved["source"].get("fingerprint") == source["fingerprint"]
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
        return out

    issues: list[dict[str, str]] = []
    preview_ready = _crossover_preview_ready(crossover_preview)
    if not preview_ready:
        issues.append(_issue(
            "blocker",
            "baseline_crossover_preview_not_ready",
            "save a fresh crossover preview before compiling an active profile",
        ))
    summary = measurements.get("summary") if isinstance(measurements.get("summary"), Mapping) else {}
    if not summary.get("driver_measurements_complete"):
        issues.append(_issue(
            "blocker",
            "baseline_driver_measurements_missing",
            "confirm each driver with a quiet test before saving the active profile",
        ))
    if not summary.get("summed_validation_complete"):
        issues.append(_issue(
            "blocker",
            "baseline_summed_validation_missing",
            "validate the combined crossover before saving the active profile",
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
    subwoofer_groups = [
        group.label for group in topology.speaker_groups
        if group.kind == "subwoofer" or group.mode == "subwoofer"
    ]
    if subwoofer_groups:
        issues.append(_issue(
            "blocker",
            "baseline_subwoofer_not_supported",
            "active profile compiler does not yet include subwoofer groups",
        ))

    preset: ActiveSpeakerPreset | None = None
    preset_gates: list[dict[str, Any]] = []
    if preview_ready:
        preset, preset_issues, preset_gates = compile_preset_from_crossover_preview(
            topology,
            dict(crossover_preview),
        )
        issues.extend(preset_issues)
    if issues:
        return _blocked_payload(
            topology=topology,
            source=source,
            issues=issues,
            status="blocked",
            config_path=config_target,
            playback_device=resolved_playback_device,
            playback_device_source=playback_device_source,
        )
    if preset is None or resolved_playback_device is None:
        return _blocked_payload(
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
        )

    corrections, correction_issues, correction_meta = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
    )
    issues.extend(correction_issues)
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
            "summed_validation_complete": bool(
                summary.get("summed_validation_complete")
            ),
            "captured_driver_count": summary.get("captured_driver_count", 0),
            "validated_summed_group_count": summary.get(
                "validated_summed_group_count",
                0,
            ),
        },
        "corrections": corrections,
        "corrections_source": correction_meta["sources"],
        "level_match": correction_meta["level_match"],
        # The per-driver level trim is a datasheet ESTIMATE, not a measured one.
        # Surfaced in /state + the wizard so a household knows to run the guided
        # phone level-match; the speaker is safe (attenuation-only) either way.
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
    }
    if write:
        atomic_write_text(
            state_target,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
        )
    return payload


def recompose_baseline_yaml(
    topology: OutputTopology,
    *,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    playback_device: str | None = None,
    out_path: str | Path | None = None,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit the active-speaker baseline YAML for the current accepted
    evidence, with optional program-domain preference EQ inserted pre-split.

    This is the composition seam the graph carrier
    (:mod:`jasper.sound.graph_carrier`) uses to apply preference EQ on top of an
    applied active baseline (``docs/HANDOFF-dsp-graph-carrier.md`` PR-3). It
    rebuilds the SAME structural baseline from the saved evidence — reusing the
    exact derivation primitives :func:`build_baseline_profile_candidate` uses
    (``resolve_active_playback_device`` → ``compile_preset_from_crossover_preview``
    → ``_derive_corrections`` → ``emit_active_speaker_baseline_config``) — rather
    than parsing the running config (the explicit anti-pattern). Only the
    ``preference_filters`` (and the explicit ``output_trim_db`` attenuation)
    differ from the durable baseline; the crossover, per-driver limiters,
    tweeter high-pass, and 0 dB ceiling are identical, so the emitted YAML
    re-proves as ``GRAPH_APPROVED_ACTIVE_RUNTIME``.

    ``output_trim_db`` is the household's manual headroom + loudness-match
    attenuation; the emitter folds it into ``active_baseline_headroom`` so the
    active EQ apply honours it exactly like the stereo path.

    Unlike :func:`build_baseline_profile_candidate` /
    :func:`apply_baseline_profile`, this re-emit takes no ``capture_device``: it
    inserts program-domain (Layer C) preference EQ, which only ever runs on the
    fan-in-fed program domain — a solo speaker's single graph and a pair
    leader's bake instance (``camilla#1``). A wireless follower (and a leader's
    own-driver instance, ``camilla#2``) is Layer-A-only and never recomposes
    preference EQ, so this seam always captures from the default fan-in tap. The
    role-varying capture (the round-trip loopback) belongs to the driver-domain
    emit on build/apply, where ``capture_device`` lives.

    **Gate scope (intentionally a subset of the candidate builder).** This only
    re-checks what it needs to EMIT a structurally-valid baseline — playback
    device, preview-readiness (the shared :func:`_crossover_preview_ready`
    predicate), and a compilable preset. It deliberately does NOT re-run the
    candidate builder's *readiness/quality* gates (driver-measurement /
    summed-validation completeness, route width, subwoofer-block): the carrier
    only reaches this for an already-APPLIED baseline that passed all of those
    at apply time, and the protective invariants are re-proven structurally by
    ``classify_camilla_graph`` + CamillaDSP ``--check`` downstream — not by these
    gates. So a quality-degraded (but still structurally-safe) re-emit is
    preferred over refusing a household's EQ change.

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
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
        baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
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
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Apply the saved baseline candidate through the shared DSP transaction.

    ``capture_device`` is threaded to :func:`build_baseline_profile_candidate`
    so the reconciler can apply a follower's round-trip-loopback baseline; the
    default keeps the solo apply byte-identical.

    ``driver_domain`` + ``program_channel`` switch the emit to a wireless active
    follower's driver-domain-only Layer-A graph (Slice 2 emitter). The follower
    branch of the multiroom reconciler passes follower-specific ``state_path`` /
    ``config_path`` alongside these so the solo baseline state is not overwritten.
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
        validate=validate,
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
    }
    applied["permissions"] = dict(applied.get("permissions") or {})
    applied["permissions"]["may_apply"] = False
    atomic_write_text(
        state_target,
        json.dumps(applied, indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )
    return {
        "status": "applied",
        "profile": applied,
        "apply": apply_state.to_dict(),
        "issues": applied.get("issues", []),
    }
