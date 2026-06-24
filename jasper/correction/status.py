# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Status and bundle payload serialization for correction sessions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import bundles, strategy

_CORRECTION_FILENAME_RE = re.compile(
    r"^correction_(?P<id>[A-Za-z0-9]+)_(?P<ts>\d+)\.yml$"
)
_MEASUREMENT_FILENAME_RE = re.compile(
    r"^correction_measurement_(?P<id>[A-Za-z0-9]+)_(?P<ts>\d+)\.yml$"
)
_SOUND_FILENAME_RE = re.compile(r"^sound_(?:current|audition)\.yml$")
_ACTIVE_SPEAKER_FILENAME_RE = re.compile(r"^active_speaker_.*\.yml$")
_PEQ_KEY_RE = re.compile(r"^\s+(?:peq|room_peq)_\d+:", re.MULTILINE)

_ACTIVE_SPEAKER_LABELS = {
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config": (
        "JTS active-speaker baseline"
    ),
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config": (
        "JTS active-speaker startup"
    ),
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_commissioning_config": (
        "JTS active-speaker commissioning"
    ),
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config": (
        "JTS active-speaker driver-domain graph"
    ),
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config": (
        "JTS active-leader program bake"
    ),
}


def parse_current_correction(
    path: str | None,
    *,
    config_dir: Path = Path("/var/lib/camilladsp/configs"),
) -> dict[str, Any] | None:
    """Return the active JTS room-correction descriptor, if any."""
    descriptor = describe_current_config(path, config_dir=config_dir)
    correction = descriptor.get("current_correction")
    return correction if isinstance(correction, dict) else None


def describe_current_config(
    path: str | None,
    *,
    config_dir: Path = Path("/var/lib/camilladsp/configs"),
    base_config_path: Path = Path("/etc/camilladsp/outputd-cutover.yml"),
) -> dict[str, Any]:
    """Describe the active CamillaDSP config without overclaiming."""
    if not path:
        return {
            "kind": "unknown",
            "managed": False,
            "path": None,
            "label": "Unknown active config",
            "message": "CamillaDSP did not report an active config path.",
            "current_correction": None,
        }
    p = Path(path)
    if p == base_config_path:
        return {
            "kind": "base",
            "managed": True,
            "path": str(p),
            "label": "JTS flat baseline",
            "message": "No JTS room correction is applied.",
            "current_correction": None,
        }
    if p.parent != Path(config_dir):
        return {
            "kind": "custom",
            "managed": False,
            "path": str(p),
            "label": "Advanced DSP config",
            "message": (
                "CamillaDSP is running a config outside the JTS generated "
                "config directory. JTS cannot safely preserve it."
            ),
            "current_correction": None,
        }

    text: str | None
    try:
        text = p.read_text()
    except OSError:
        text = None

    if text is not None:
        for source, label in _ACTIVE_SPEAKER_LABELS.items():
            if f"Source: {source}" not in text:
                continue
            peq_count = len(_PEQ_KEY_RE.findall(text))
            correction = None
            if peq_count:
                match = _CORRECTION_FILENAME_RE.match(p.name)
                try:
                    applied_at_epoch = (
                        int(match.group("ts")) if match else int(p.stat().st_mtime)
                    )
                except (OSError, ValueError):
                    applied_at_epoch = 0
                correction = {
                    "path": str(p),
                    "session_id": match.group("id") if match else "active_speaker",
                    "applied_at_epoch": applied_at_epoch,
                    "peq_count": peq_count,
                }
            return {
                "kind": (
                    "active_speaker_with_correction"
                    if correction
                    else "active_speaker"
                ),
                "managed": True,
                "path": str(p),
                "label": label,
                "message": (
                    "Active-speaker DSP is active and includes room correction "
                    "PEQs; correction state is managed by the active-speaker/"
                    "grouping graph."
                    if correction
                    else (
                        "Active-speaker DSP is active; correction state is "
                        "managed by the active-speaker/grouping graph."
                    )
                ),
                "current_correction": correction,
            }

    m = _CORRECTION_FILENAME_RE.match(p.name)
    if not m:
        if _MEASUREMENT_FILENAME_RE.match(p.name):
            return {
                "kind": "measurement_baseline",
                "managed": True,
                "path": str(p),
                "label": "JTS room-correction measurement baseline",
                "message": (
                    "Room-correction measurement is using a temporary baseline "
                    "with preference and room-correction filters removed."
                ),
                "current_correction": None,
            }
        if _ACTIVE_SPEAKER_FILENAME_RE.match(p.name) and text is None:
            return {
                "kind": "unknown",
                "managed": False,
                "path": str(p),
                "label": "Unreadable active-speaker config",
                "message": "JTS could not read the active-speaker config file.",
                "current_correction": None,
            }
        if not _SOUND_FILENAME_RE.match(p.name):
            return {
                "kind": "custom",
                "managed": False,
                "path": str(p),
                "label": "Advanced DSP config",
                "message": (
                    "CamillaDSP is running a config that JTS did not "
                    "generate. JTS cannot safely preserve it."
                ),
                "current_correction": None,
            }
        try:
            peq_count = len(_PEQ_KEY_RE.findall(text or ""))
            applied_at_epoch = int(p.stat().st_mtime)
        except OSError:
            return {
                "kind": "unknown",
                "managed": False,
                "path": str(p),
                "label": "Unreadable JTS sound config",
                "message": "JTS could not read the active sound config file.",
                "current_correction": None,
            }
        if peq_count == 0:
            return {
                "kind": "sound_preference",
                "managed": True,
                "path": str(p),
                "label": "JTS sound preference",
                "message": "Preference EQ is active; no room correction PEQs were found.",
                "current_correction": None,
            }
        correction = {
            "path": str(p),
            "session_id": "sound",
            "applied_at_epoch": applied_at_epoch,
            "peq_count": peq_count,
        }
        return {
            "kind": "sound_with_correction",
            "managed": True,
            "path": str(p),
            "label": "JTS sound preference with room correction",
            "message": "A JTS sound config is active and includes room correction PEQs.",
            "current_correction": correction,
        }
    try:
        ts = int(m.group("ts"))
    except ValueError:
        return {
            "kind": "custom",
            "managed": False,
            "path": str(p),
            "label": "Advanced DSP config",
            "message": "Correction-shaped config has an invalid timestamp.",
            "current_correction": None,
        }
    peq_count = 0
    if text:
        peq_count = len(_PEQ_KEY_RE.findall(text))
    correction = {
        "path": str(p),
        "session_id": m.group("id"),
        "applied_at_epoch": ts,
        "peq_count": peq_count,
    }
    return {
        "kind": "correction",
        "managed": True,
        "path": str(p),
        "label": "JTS room correction",
        "message": "A JTS room correction config is active.",
        "current_correction": correction,
    }


def _target_profile_payload(session: Any) -> dict[str, Any]:
    return strategy.resolve_target_profile(session.target_choice).to_dict()


def _correction_strategy_payload(session: Any) -> dict[str, Any]:
    return strategy.resolve_correction_strategy(session.strategy_choice).to_dict()


def _mic_calibration_payload(session: Any) -> dict[str, Any] | None:
    return (
        session.mic_calibration.public_metadata()
        if session.mic_calibration
        else None
    )


def _curve_payload(curve: Any) -> dict[str, Any] | None:
    return curve.__dict__ if curve else None


def _peq_payload(session: Any) -> list[dict[str, Any]]:
    return [p.__dict__ for p in session.peqs]


def _acoustic_summary(session: Any) -> dict[str, Any] | None:
    return (
        (session.acoustic_quality or {}).get("summary")
        if session.acoustic_quality
        else None
    )


def session_config_payload(session: Any) -> dict[str, Any]:
    cfg = session.cfg
    return {
        "f1_hz": cfg.f1_hz,
        "f2_hz": cfg.f2_hz,
        "duration_s": cfg.duration_s,
        "sample_rate": cfg.sample_rate,
        "amplitude_dbfs": cfg.amplitude_dbfs,
        "peq_f_low": cfg.peq_f_low,
        "peq_f_high": cfg.peq_f_high,
        "peq_max_filters": cfg.peq_max_filters,
        "peq_max_cut_db": cfg.peq_max_cut_db,
        "peq_max_boost_db": cfg.peq_max_boost_db,
        "peq_cuts_only": cfg.peq_cuts_only,
        "peq_flatness_target_db": cfg.peq_flatness_target_db,
        "correction_strategy": cfg.correction_strategy,
    }


def session_snapshot(session: Any) -> dict[str, Any]:
    """Build the live `/status` session snapshot payload."""
    return {
        "session_id": session.session_id,
        "state": session.state.value,
        "started_at": session.started_at,
        "updated_at": session.updated_at,
        "error": session.error,
        "total_positions": session.total_positions,
        "current_position": session.current_position,
        "repeat_main_position": session.repeat_main_position,
        "target_choice": session.target_choice,
        "target_profile": _target_profile_payload(session),
        "strategy_choice": session.strategy_choice,
        "correction_strategy": _correction_strategy_payload(session),
        "input_device": session.input_device,
        "mic_calibration": _mic_calibration_payload(session),
        "browser_audio_report": session.browser_audio_report,
        # Point-in-time copies: these containers can mutate while a
        # handler thread serializes `/status`.
        "capture_quality": list(session.capture_quality),
        "noise_reports": list(session.noise_reports),
        "repeat_quality": session.repeat_quality,
        "repeatability_report": session.repeatability_report,
        "verify_quality": session.verify_quality,
        "confidence_report": session.confidence_report,
        "acoustic_quality": _acoustic_summary(session),
        "runtime_integrity": session.runtime_integrity.summary(),
        "position_analysis": session.position_analysis,
        "sweep": (
            session.sweep_meta.to_dict() if session.sweep_meta else None
        ),
        "peqs": _peq_payload(session),
        "design_report": (
            dict(session.design_report)
            if session.design_report is not None
            else None
        ),
        "config_path": (
            str(session.config_path) if session.config_path else None
        ),
        "measurement_config_path": (
            str(session.measurement_config_path)
            if getattr(session, "measurement_config_path", None)
            else None
        ),
        "pre_measurement_config_path": (
            str(session.pre_measurement_config_path)
            if getattr(session, "pre_measurement_config_path", None)
            else None
        ),
        "verify_metrics": session.verify_metrics,
        "autolevel": session.autolevel.snapshot(),
    }


def info_json_payload(session: Any) -> dict[str, Any]:
    """Build the per-session `info.json` metadata payload."""
    return {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session.session_id,
        "state": session.state.value,
        "started_at": session.started_at,
        "updated_at": session.updated_at,
        "error": session.error,
        "total_positions": session.total_positions,
        "current_position": session.current_position,
        "repeat_main_position": session.repeat_main_position,
        "target_choice": session.target_choice,
        "target_profile": _target_profile_payload(session),
        "strategy_choice": session.strategy_choice,
        "correction_strategy": _correction_strategy_payload(session),
        "noise_floor_db": session.noise_floor_db,
        "input_device": session.input_device,
        "mic_calibration": _mic_calibration_payload(session),
        "browser_audio_report": session.browser_audio_report,
        "capture_quality": session.capture_quality,
        "noise_reports": session.noise_reports,
        "repeat_quality": session.repeat_quality,
        "repeatability_report": session.repeatability_report,
        "verify_quality": session.verify_quality,
        "confidence_report": session.confidence_report,
        "acoustic_quality": _acoustic_summary(session),
        "runtime_integrity": session.runtime_integrity.summary(),
        "position_analysis": session.position_analysis,
        "current_correction_at_start": session.current_correction_at_start,
        "autolevel": session.autolevel.snapshot(),
        "sweep_meta": (
            session.sweep_meta.to_dict() if session.sweep_meta else None
        ),
        "peqs": _peq_payload(session),
        "design_report": session.design_report,
        "config_path": (
            str(session.config_path) if session.config_path else None
        ),
        "measurement_config_path": (
            str(session.measurement_config_path)
            if getattr(session, "measurement_config_path", None)
            else None
        ),
        "pre_measurement_config_path": (
            str(session.pre_measurement_config_path)
            if getattr(session, "pre_measurement_config_path", None)
            else None
        ),
        "verify_metrics": session.verify_metrics,
        "config": session_config_payload(session),
    }


def result_json_payload(session: Any) -> dict[str, Any]:
    """Build the per-session `result.json` analysis payload."""
    return {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session.session_id,
        "input_device": session.input_device,
        "mic_calibration": _mic_calibration_payload(session),
        "browser_audio_report": session.browser_audio_report,
        "measured": _curve_payload(session.measured_curve),
        "target": _curve_payload(session.target_curve),
        "predicted": _curve_payload(session.predicted_curve),
        "verify": _curve_payload(session.verify_curve),
        "verify_metrics": session.verify_metrics,
        "capture_quality": session.capture_quality,
        "noise_reports": session.noise_reports,
        "repeat": _curve_payload(session.repeat_curve),
        "repeat_quality": session.repeat_quality,
        "repeatability_report": session.repeatability_report,
        "verify_quality": session.verify_quality,
        "confidence_report": session.confidence_report,
        "acoustic_quality": _acoustic_summary(session),
        "runtime_integrity": session.runtime_integrity.summary(),
        "position_analysis": session.position_analysis,
        "peqs": _peq_payload(session),
        "design_report": session.design_report,
    }
