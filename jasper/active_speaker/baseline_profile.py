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
from jasper.camilla_config_contract import FilterSpec
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    validate_camilla_config,
)
from jasper.output_topology import OutputTopology

from ._common import issue as _issue
from .camilla_yaml import emit_active_speaker_baseline_config
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
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": _fingerprint(topology.to_dict()),
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


def _derive_corrections(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
) -> tuple[dict[str, dict[str, float | bool]], list[dict[str, str]]]:
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

    # Fail-safe sensitivity trim. When research declares no explicit
    # gain_offset_db for a driver but the sensitivities are known, attenuate the
    # hotter drivers down to the least-sensitive (reference) driver so a
    # high-sensitivity compression/horn driver can never start at full level
    # relative to the woofer (the shrill / horn-dominant failure mode, and a
    # diaphragm hazard). An explicit gain_offset_db always wins; on-axis
    # measurement refines this interim trim later.
    derivable_roles = [
        role for role in sensitivities if role not in explicit_gain_roles
    ]
    if len(sensitivities) >= 2 and derivable_roles:
        reference_db = min(sensitivities.values())
        derived_notes: list[str] = []
        for role in derivable_roles:
            trim_db = reference_db - sensitivities[role]  # <= 0 by construction
            if trim_db >= -_SENSITIVITY_TRIM_EPS_DB:
                continue  # reference driver and ties stay at unity
            if trim_db < _MAX_ATTENUATION_DB:
                trim_db = _MAX_ATTENUATION_DB
            corrections[role]["gain_db"] = round(trim_db, 1)
            derived_notes.append(f"{role} {round(trim_db, 1):.1f} dB")
        if derived_notes:
            issues.append(_issue(
                "warning",
                "driver_gain_derived_from_sensitivity",
                (
                    "applied an interim level trim from the sensitivity gap ("
                    + ", ".join(derived_notes)
                    + "); confirm against measurement before final tuning"
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
    return corrections, issues


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
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build or write a baseline candidate from current accepted evidence."""

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
    preview_ready = (
        crossover_preview.get("kind") == "jts_active_speaker_crossover_preview"
        and crossover_preview.get("status") == "ready_for_protected_staging"
        and bool(
            (crossover_preview.get("permissions") or {}).get(
                "may_prepare_protected_startup_config"
            )
        )
    )
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

    corrections, correction_issues = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
    )
    issues.extend(correction_issues)
    validation = {"status": "skipped", "reason": "not_written"}
    if write:
        config_target.parent.mkdir(parents=True, exist_ok=True)
        yaml = emit_active_speaker_baseline_config(
            preset,
            playback_device=resolved_playback_device,
            corrections=corrections,
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
    playback_device: str | None = None,
    out_path: str | Path | None = None,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit the active-speaker baseline YAML for the current accepted
    evidence, with optional program-domain preference EQ folded in pre-split.

    This is the composition seam the graph carrier
    (:mod:`jasper.sound.graph_carrier`) uses to apply preference EQ on top of an
    applied active baseline (``docs/HANDOFF-dsp-graph-carrier.md`` PR-3). It
    rebuilds the SAME structural baseline from the saved evidence — reusing the
    exact derivation primitives :func:`build_baseline_profile_candidate` uses
    (``resolve_active_playback_device`` → ``compile_preset_from_crossover_preview``
    → ``_derive_corrections`` → ``emit_active_speaker_baseline_config``) — rather
    than parsing the running config (the explicit anti-pattern). Only the
    ``preference_filters`` differ from the durable baseline; the crossover,
    per-driver limiters, tweeter high-pass, and 0 dB ceiling are identical, so
    the emitted YAML re-proves as ``GRAPH_APPROVED_ACTIVE_RUNTIME``.

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
    preview_ready = (
        crossover_preview.get("kind") == "jts_active_speaker_crossover_preview"
        and crossover_preview.get("status") == "ready_for_protected_staging"
        and bool(
            (crossover_preview.get("permissions") or {}).get(
                "may_prepare_protected_startup_config"
            )
        )
    )
    if not preview_ready:
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
    corrections, _correction_issues = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
    )
    yaml = emit_active_speaker_baseline_config(
        preset,
        playback_device=resolved_device,
        corrections=corrections,
        preference_filters=preference_filters,
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
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Apply the saved baseline candidate through the shared DSP transaction."""

    state_target = baseline_profile_state_path(state_path)
    candidate = build_baseline_profile_candidate(
        topology,
        design_draft=design_draft,
        crossover_preview=crossover_preview,
        measurements=measurements,
        write=True,
        state_path=state_target,
        config_path=config_path,
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
