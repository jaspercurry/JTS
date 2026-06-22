"""Gain-chain visibility for jasper-control's ``/state`` payload.

This module does not own audio policy. It builds a diagnostic ledger from
runtime state that other subsystems already own: the user volume carrier,
CamillaDSP's active config, sound-profile trims, fan-in ducking, and outputd
multiroom trims. The intent is one place to answer "why is this quieter?" while
keeping the real knobs in their existing homes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from pathlib import Path
from typing import Any, Mapping

from ..log_event import log_event

logger = logging.getLogger(__name__)

_LOG_LOCK = threading.Lock()
_LAST_LOG_FINGERPRINT: str | None = None


def _finite_db(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, 2)


def _truthy(value: Any) -> bool:
    return value is True or value == "true" or value == "True" or value == 1


def _stage(
    stage_id: str,
    label: str,
    category: str,
    *,
    gain_db: Any = None,
    active: bool | None = True,
    scope: str = "program",
    source: str,
    included_in_common_total: bool = False,
    dynamic: bool = False,
    nonlinear: bool = False,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": stage_id,
        "label": label,
        "category": category,
        "scope": scope,
        "active": active,
        "gain_db": _finite_db(gain_db),
        "included_in_common_total": included_in_common_total,
        "dynamic": dynamic,
        "nonlinear": nonlinear,
        "source": source,
    }
    if reason:
        out["reason"] = reason
    if details:
        out["details"] = dict(details)
    return out


def _read_camilla_config(path: Any, warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        warnings.append(f"camilla_config_yaml_unavailable:{e}")
        return None
    config_path = Path(path)
    try:
        if not config_path.is_file():
            warnings.append(f"camilla_config_missing:{path}")
            return None
        raw_config = config_path.read_text(encoding="utf-8")
    except OSError as e:
        warnings.append(f"camilla_config_unreadable:{path}:{e}")
        return None
    try:
        blob = yaml.safe_load(raw_config)
    except yaml.YAMLError as e:
        warnings.append(f"camilla_config_unreadable:{path}:{e}")
        return None
    if not isinstance(blob, dict):
        warnings.append(f"camilla_config_not_mapping:{path}")
        return None
    return blob


def _gain_filter_stages(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    filters = config.get("filters")
    if not isinstance(filters, Mapping):
        return []
    stages: list[dict[str, Any]] = []
    for raw_name, raw_filter in sorted(filters.items()):
        name = str(raw_name)
        if not isinstance(raw_filter, Mapping):
            continue
        if raw_filter.get("type") != "Gain":
            continue
        params = raw_filter.get("parameters")
        if not isinstance(params, Mapping):
            params = {}
        gain_db = _finite_db(params.get("gain"))
        muted = _truthy(params.get("mute"))
        inverted = _truthy(params.get("inverted"))
        common = False
        category = "dsp_gain"
        scope = "program"
        label = name.replace("_", " ")
        reason = None
        if name in {
            "active_baseline_headroom",
            "active_startup_headroom",
            "room_headroom",
            "sound_preamp",
        }:
            common = True
            category = "dsp_headroom"
            label = {
                "active_baseline_headroom": "Active baseline headroom",
                "active_startup_headroom": "Active startup headroom",
                "room_headroom": "Room-correction headroom",
                "sound_preamp": "Sound-profile preamp",
            }[name]
        elif name.startswith("as_") and name.endswith("_baseline_gain"):
            category = "driver_calibration"
            scope = "driver"
            label = name.removeprefix("as_").removesuffix("_baseline_gain")
            label = f"{label.replace('_', ' ').title()} calibration"
            reason = (
                "Per-driver calibration is not a common program gain; each "
                "output channel may differ."
            )
        elif name.startswith("as_") and name.endswith("_startup_mute"):
            category = "protection"
            scope = "driver"
            label = name.removeprefix("as_").removesuffix("_startup_mute")
            label = f"{label.replace('_', ' ').title()} startup mute"
            reason = "Startup protection mute."
        elif muted:
            category = "protection"
            reason = "CamillaDSP Gain filter is muted."

        if gain_db == 0.0 and not muted and name not in {"active_baseline_headroom"}:
            continue

        stages.append(
            _stage(
                name,
                label,
                category,
                gain_db=gain_db,
                active=True,
                scope=scope,
                source="camilla_config.filters",
                included_in_common_total=common,
                reason=reason,
                details={
                    "mute": muted,
                    "inverted": inverted,
                    "filter_type": "Gain",
                },
            )
        )
    return stages


def _limiter_stages(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    filters = config.get("filters")
    if not isinstance(filters, Mapping):
        return []
    stages: list[dict[str, Any]] = []
    for raw_name, raw_filter in sorted(filters.items()):
        name = str(raw_name)
        if not isinstance(raw_filter, Mapping):
            continue
        if raw_filter.get("type") != "Limiter":
            continue
        params = raw_filter.get("parameters")
        if not isinstance(params, Mapping):
            params = {}
        stages.append(
            _stage(
                name,
                name.replace("_", " "),
                "protection",
                active=True,
                scope="driver" if name.startswith("as_") else "program",
                source="camilla_config.filters",
                dynamic=True,
                nonlinear=True,
                reason="Limiter reduces gain only when the signal approaches its ceiling.",
                details={
                    "filter_type": "Limiter",
                    "clip_limit_db": _finite_db(params.get("clip_limit")),
                    "soft_clip": _truthy(params.get("soft_clip")),
                },
            )
        )
    return stages


def _mono_fold_stage(config: Mapping[str, Any]) -> dict[str, Any] | None:
    mixers = config.get("mixers")
    if not isinstance(mixers, Mapping):
        return None
    folded_destinations: list[int] = []
    source_gains: list[float] = []
    mixer_name: str | None = None
    for raw_name, raw_mixer in sorted(mixers.items()):
        name = str(raw_name)
        if not name.startswith("split_active_") or not isinstance(raw_mixer, Mapping):
            continue
        mapping = raw_mixer.get("mapping")
        if not isinstance(mapping, list):
            continue
        for entry in mapping:
            if not isinstance(entry, Mapping):
                continue
            sources = entry.get("sources")
            if not isinstance(sources, list) or len(sources) < 2:
                continue
            gains: list[float] = []
            channels: list[int] = []
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                gain = _finite_db(source.get("gain"))
                channel = source.get("channel")
                if gain is None or not isinstance(channel, int):
                    continue
                gains.append(gain)
                channels.append(channel)
            if len(gains) < 2 or set(channels[:2]) != {0, 1}:
                continue
            dest = entry.get("dest")
            if isinstance(dest, int):
                folded_destinations.append(dest)
            source_gains = gains
            mixer_name = name
    if not folded_destinations:
        return None
    return _stage(
        "active_mono_fold_down",
        "Active mono fold-down",
        "channel_transform",
        gain_db=0.0,
        active=True,
        scope="program",
        source="camilla_config.mixers",
        included_in_common_total=True,
        reason=(
            "Each destination receives L and R at -6 dB so correlated mono "
            "content sums to unity instead of clipping."
        ),
        details={
            "mixer": mixer_name,
            "destinations": sorted(folded_destinations),
            "source_gains_db": source_gains,
        },
    )


def _volume_limit_stage(config: Mapping[str, Any]) -> dict[str, Any] | None:
    devices = config.get("devices")
    if not isinstance(devices, Mapping):
        return None
    ceiling = _finite_db(devices.get("volume_limit"))
    if ceiling is None:
        return None
    return _stage(
        "camilla_volume_limit",
        "Camilla volume limit",
        "policy_cap",
        active=True,
        scope="program",
        source="camilla_config.devices",
        nonlinear=True,
        reason="Ceiling for CamillaDSP's main volume fader; it is not a gain stage by itself.",
        details={"ceiling_db": ceiling},
    )


def _sound_setting_stages(
    sound_profile: Mapping[str, Any] | None,
    *,
    camilla_stage_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(sound_profile, Mapping):
        return []
    runtime = sound_profile.get("runtime")
    runtime_active = None
    if isinstance(runtime, Mapping):
        runtime_active = runtime.get("active")
    if runtime_active is None:
        runtime_active = sound_profile.get("runtime_active")
    active = runtime_active is True
    stages: list[dict[str, Any]] = []
    output_trim_db = _finite_db(sound_profile.get("output_trim_db"))
    if (
        output_trim_db is not None
        and output_trim_db != 0.0
        and "sound_preamp" not in camilla_stage_ids
    ):
        stages.append(
            _stage(
                "sound_output_trim",
                "Sound output trim",
                "output_trim",
                gain_db=-output_trim_db,
                active=active,
                scope="program",
                source="sound_settings",
                included_in_common_total=active,
                reason=(
                    "Global sound-profile trim inferred from state because "
                    "the active CamillaDSP config did not expose sound_preamp."
                ),
                details={
                    "match_loudness": bool(sound_profile.get("match_loudness")),
                    "headroom_trim_db": _finite_db(sound_profile.get("headroom_trim_db")),
                    "output_trim_db": output_trim_db,
                },
            )
        )
    return stages


def _outputd_stages(outputd_status: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(outputd_status, Mapping):
        return []
    stages: list[dict[str, Any]] = []
    dac_content = outputd_status.get("dac_content")
    if isinstance(dac_content, Mapping):
        trim = _finite_db(dac_content.get("trim_db"))
        enabled = dac_content.get("enabled")
        if trim is not None and trim != 0.0:
            stages.append(
                _stage(
                    "outputd_dac_content_trim",
                    "Outputd DAC-content trim",
                    "output_trim",
                    gain_db=trim,
                    active=bool(enabled),
                    scope="multiroom_dac_content",
                    source="outputd.status",
                    included_in_common_total=bool(enabled),
                    reason="Multiroom/bonded-output content trim owned by outputd.",
                    details={"enabled": bool(enabled)},
                )
            )
    return stages


def _fanin_stages(fanin_status: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(fanin_status, Mapping):
        return []
    tts = fanin_status.get("tts")
    if not isinstance(tts, Mapping):
        return []
    stages: list[dict[str, Any]] = []
    if bool(tts.get("program_duck_active")):
        stages.append(
            _stage(
                "fanin_tts_program_duck",
                "TTS program duck",
                "dynamic_duck",
                active=True,
                scope="program",
                source="fanin.status",
                dynamic=True,
                reason=(
                    "Fan-in is ducking program audio for TTS; STATUS does not "
                    "currently expose the configured duck dB."
                ),
            )
        )
    assistant_loudness = tts.get("assistant_loudness")
    if isinstance(assistant_loudness, Mapping):
        final_gain = _finite_db(assistant_loudness.get("final_gain_db"))
        if final_gain is not None:
            stages.append(
                _stage(
                    "fanin_assistant_loudness",
                    "Assistant loudness normalization",
                    "assistant_loudness",
                    gain_db=final_gain,
                    active=bool(assistant_loudness.get("decision_seen")),
                    scope="assistant_tts",
                    source="fanin.status",
                    included_in_common_total=False,
                    dynamic=True,
                    reason="Applies to assistant speech, not music/program audio.",
                    details={
                        "requested_gain_db": _finite_db(
                            assistant_loudness.get("requested_gain_db")
                        ),
                        "peak_cap_gain_db": _finite_db(
                            assistant_loudness.get("peak_cap_gain_db")
                        ),
                    },
                )
            )
    return stages


def _fingerprint(payload: Mapping[str, Any]) -> str:
    stable = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def _maybe_log(snapshot: Mapping[str, Any]) -> None:
    global _LAST_LOG_FINGERPRINT
    fingerprint = snapshot.get("fingerprint")
    if not isinstance(fingerprint, str):
        return
    with _LOG_LOCK:
        if fingerprint == _LAST_LOG_FINGERPRINT:
            return
        _LAST_LOG_FINGERPRINT = fingerprint
    log_event(
        logger,
        "audio.gain_chain.snapshot",
        active_source=snapshot.get("active_source"),
        common_static_gain_db=snapshot.get("common_static_gain_db"),
        complete=snapshot.get("complete_common_static_gain"),
        stage_count=snapshot.get("stage_count"),
        warning_count=len(snapshot.get("warnings") or []),
        fingerprint=fingerprint,
    )


def build_gain_chain_snapshot(
    *,
    active_source: str | None,
    volume_policy: Mapping[str, Any] | None,
    camilla_status: Mapping[str, Any] | None,
    sound_profile: Mapping[str, Any] | None,
    fanin_status: Mapping[str, Any] | None,
    outputd_status: Mapping[str, Any] | None,
    log_changes: bool = False,
) -> dict[str, Any]:
    """Return a JSON-able ledger of volume-affecting stages.

    ``common_static_gain_db`` is intentionally narrow: it sums only scalar gain
    stages that are common to program audio right now. Per-driver calibration,
    dynamic duckers, limiters, and source-owned volume are still listed as
    stages, but excluded from that headline number so the single number stays
    honest.
    """

    volume_policy = volume_policy if isinstance(volume_policy, Mapping) else {}
    camilla_status = camilla_status if isinstance(camilla_status, Mapping) else {}
    warnings: list[str] = []
    stages: list[dict[str, Any]] = []
    complete_common = True

    carrier = volume_policy.get("carrier")
    mode = volume_policy.get("volume_mode")
    source = volume_policy.get("source")
    listening_level = volume_policy.get("listening_level_percent")
    main_volume_db = _finite_db(volume_policy.get("main_volume_db"))
    if main_volume_db is None and carrier == "camilla_guard":
        main_volume_db = _finite_db(volume_policy.get("guard_db"))
    if main_volume_db is None:
        main_volume_db = _finite_db(camilla_status.get("main_volume_db"))

    if carrier == "source":
        complete_common = False
        warnings.append("source_volume_db_unknown")
        stages.append(
            _stage(
                "source_owned_volume",
                "Source-owned volume",
                "source_volume",
                active=True,
                scope="program",
                source="volume_policy",
                reason=(
                    "This source owns volume in push mode; /state has percent "
                    "but not the source's dB scalar."
                ),
                details={
                    "source": source,
                    "listening_level_percent": listening_level,
                    "volume_mode": mode,
                },
            )
        )
    else:
        if main_volume_db is None:
            complete_common = False
            warnings.append("camilla_main_volume_db_unknown")
        stages.append(
            _stage(
                "user_volume",
                "User volume",
                "user_volume",
                gain_db=main_volume_db,
                active=main_volume_db is not None,
                scope="program",
                source=(
                    "camilla.push_guard"
                    if carrier == "camilla_guard"
                    else "camilla.main_volume"
                ),
                included_in_common_total=main_volume_db is not None,
                reason="The user-visible volume knob when CamillaDSP is the carrier.",
                details={
                    "carrier": carrier,
                    "listening_level_percent": listening_level,
                    "push_guard_active": bool(volume_policy.get("push_guard_active")),
                    "guard_reason": volume_policy.get("guard_reason"),
                },
            )
        )

    active_config_path = camilla_status.get("active_config_path")
    config = _read_camilla_config(active_config_path, warnings)
    if active_config_path and config is None:
        complete_common = False
    if isinstance(config, Mapping):
        limit_stage = _volume_limit_stage(config)
        if limit_stage is not None:
            stages.append(limit_stage)
        stages.extend(_gain_filter_stages(config))
        mono_stage = _mono_fold_stage(config)
        if mono_stage is not None:
            stages.append(mono_stage)
        stages.extend(_limiter_stages(config))

    camilla_stage_ids = {
        stage["id"]
        for stage in stages
        if stage.get("source", "").startswith("camilla_config.")
    }
    stages.extend(
        _sound_setting_stages(sound_profile, camilla_stage_ids=camilla_stage_ids)
    )
    stages.extend(_outputd_stages(outputd_status))
    stages.extend(_fanin_stages(fanin_status))

    common_gains = [
        stage["gain_db"]
        for stage in stages
        if stage.get("included_in_common_total") and stage.get("gain_db") is not None
    ]
    common_static_gain_db = round(sum(float(gain) for gain in common_gains), 2)
    unknown_stage_count = sum(
        1
        for stage in stages
        if stage.get("active") is True
        and stage.get("gain_db") is None
        and stage.get("category") not in {"policy_cap", "protection"}
    )
    dynamic_stage_count = sum(1 for stage in stages if stage.get("dynamic"))
    nonlinear_stage_count = sum(1 for stage in stages if stage.get("nonlinear"))

    fingerprint_payload = {
        "active_source": active_source,
        "common_static_gain_db": common_static_gain_db,
        "complete_common_static_gain": complete_common,
        "warnings": warnings,
        "stages": stages,
    }
    snapshot = {
        "schema_version": 1,
        "active_source": active_source,
        "common_static_gain_db": common_static_gain_db,
        "complete_common_static_gain": complete_common,
        "stage_count": len(stages),
        "unknown_stage_count": unknown_stage_count,
        "dynamic_stage_count": dynamic_stage_count,
        "nonlinear_stage_count": nonlinear_stage_count,
        "warnings": warnings,
        "stages": stages,
        "active_config_path": active_config_path,
        "fingerprint": _fingerprint(fingerprint_payload),
    }
    if log_changes:
        _maybe_log(snapshot)
    return snapshot
