# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wake-corpus session metadata persistence.

The JSON-sidecar file I/O extracted from ``RecordingBackend``: finding,
parsing, listing, and deleting the ``enroll_<member>_<session_id>.json``
files under a recorder's metadata directory. No threading, no in-memory
session state — ``RecordingBackend`` owns that and delegates here for the
disk side.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_USB,
    config_metadata,
    variant_metadata,
)

from .bridge_session import (
    AEC3_SWEEP_LEGS,
    CORPUS_PROFILES,
    DTLN_LEG,
    PROFILE_CHIP_AEC_COMPARISON,
    PROFILE_STANDARD,
    RAW0_LEG,
    USB_CORPUS_LEGS,
    USB_DTLN_LEG,
    XVF_RAW0_DTLN_LEG,
    _enabled_legs_from_metadata,
    _legacy_aec3_sweep_source,
    _metadata_flag,
    chip_aec_config_metadata,
)

logger = logging.getLogger("jasper-wake-corpus-web")


def session_metadata_path(
    metadata_dir: Path, member: str | None, session_id: str | None,
) -> Path:
    return metadata_dir / f"enroll_{member}_{session_id}.json"


def write_metadata_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """Atomic-rewrite the session JSON sidecar. Called after every clip
    write + delete so the file on disk always reflects the current state
    (resilient to a server crash mid-session)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def find_session_file(metadata_dir: Path, session_id: str) -> Path | None:
    for p in metadata_dir.glob("enroll_*.json"):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("session_id") == session_id:
            return p
    return None


def parse_session_data(
    data: dict[str, Any], ports: dict[str, int],
) -> dict[str, Any]:
    """Parse a session JSON sidecar's raw dict into typed session fields.

    Clips are returned as raw dicts; the caller builds its own clip-metadata
    objects from them. Raises KeyError/TypeError on a malformed sidecar —
    callers convert those to a ValueError with session context.
    """
    session_id = data["session_id"]
    member = data["member"]
    clips = list(data.get("clips", []))
    enabled_legs = _enabled_legs_from_metadata(data, ports)
    corpus_profile = str(data.get("corpus_profile") or PROFILE_STANDARD)
    if corpus_profile not in CORPUS_PROFILES:
        corpus_profile = PROFILE_STANDARD
    saved_config = data.get("aec3_sweep_config")
    saved_source = (
        saved_config.get("input_source")
        if isinstance(saved_config, dict) else None
    )
    aec3_sweep_source = _legacy_aec3_sweep_source(
        str(data.get("aec3_sweep_source") or saved_source or ""),
    )
    include_raw_mic_0 = RAW0_LEG in enabled_legs
    include_usb_mic = bool(
        data.get(
            "include_usb_mic",
            any(leg in USB_CORPUS_LEGS for leg in enabled_legs),
        )
        or (
            aec3_sweep_source == AEC3_SWEEP_SOURCE_USB
            and any(leg in AEC3_SWEEP_LEGS for leg in enabled_legs)
        )
    )
    include_aec3_sweep = (
        bool(data.get("include_aec3_sweep", False))
        or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
    )
    saved_variants = data.get("aec3_sweep_variants")
    if not isinstance(saved_variants, list):
        saved_variants = []
    if not isinstance(saved_config, dict):
        saved_config = None
    if include_aec3_sweep and not saved_variants:
        saved_variants = variant_metadata(input_source=aec3_sweep_source)
        saved_config = config_metadata(input_source=aec3_sweep_source)
    elif include_aec3_sweep and saved_config is not None:
        saved_config = dict(saved_config)
        saved_config.setdefault("input_source", aec3_sweep_source)
    include_dtln = _metadata_flag(data, "include_dtln", DTLN_LEG, enabled_legs)
    include_usb_dtln = _metadata_flag(
        data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
    )
    include_xvf_raw0_dtln = _metadata_flag(
        data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
    )
    chip_config = data.get("chip_aec_config")
    if not isinstance(chip_config, dict):
        chip_config = (
            chip_aec_config_metadata()
            if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
        )
    audio_context = data.get("audio_context")
    if not isinstance(audio_context, dict):
        audio_context = None
    capture_plan = data.get("capture_plan")
    if not isinstance(capture_plan, dict):
        capture_plan = None
    return {
        "session_id": session_id,
        "member": member,
        "clips": clips,
        "enabled_legs": enabled_legs,
        "include_raw_mic_0": include_raw_mic_0,
        "include_dtln": include_dtln,
        "include_usb_mic": include_usb_mic,
        "include_usb_dtln": include_usb_dtln,
        "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
        "include_aec3_sweep": include_aec3_sweep,
        "corpus_profile": corpus_profile,
        "chip_aec_config": chip_config,
        "aec3_sweep_source": aec3_sweep_source,
        "aec3_sweep_variants": saved_variants,
        "aec3_sweep_config": saved_config,
        "capture_plan": capture_plan,
        "audio_context": audio_context,
    }


def list_session_summaries(
    metadata_dir: Path,
    ports: dict[str, int],
    active_session_id: str | None,
) -> list[dict[str, Any]]:
    """Scan the metadata dir, return one summary per session.

    Each summary: {session_id, member, mtime, clip_count,
    deleted_count, enabled_legs, conditions: {<cond>: n, ...}}.
    Sorted newest-first by mtime.

    Failure-soft: corrupt JSON files are skipped + logged, not
    raised — one bad file shouldn't black out the whole list.
    """
    if not metadata_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(
        metadata_dir.glob("enroll_*.json"),
        key=lambda f: f.stat().st_mtime, reverse=True,
    ):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip corrupt session %s: %s", p.name, e)
            continue
        clips = data.get("clips", [])
        alive = [c for c in clips if not c.get("deleted")]
        conds: dict[str, int] = {}
        for c in alive:
            k = c.get("condition", "?")
            conds[k] = conds.get(k, 0) + 1
        enabled_legs = _enabled_legs_from_metadata(data, ports)
        saved_config = data.get("aec3_sweep_config")
        saved_source = (
            saved_config.get("input_source")
            if isinstance(saved_config, dict) else None
        )
        aec3_sweep_source = _legacy_aec3_sweep_source(
            str(data.get("aec3_sweep_source") or saved_source or ""),
        )
        audio_context = data.get("audio_context")
        if not isinstance(audio_context, dict):
            audio_context = {}
        capture_plan = data.get("capture_plan")
        if not isinstance(capture_plan, dict):
            capture_plan = {}
        resource = capture_plan.get("resource")
        if not isinstance(resource, dict):
            resource = {}
        audio_profile = audio_context.get("production_audio_profile")
        if not isinstance(audio_profile, dict):
            audio_profile = {}
        dac_reference = audio_context.get("dac_reference")
        if not isinstance(dac_reference, dict):
            dac_reference = {}
        validation = dac_reference.get("validation")
        if not isinstance(validation, dict):
            validation = {}
        out.append({
            "session_id": data.get("session_id", "?"),
            "member": data.get("member", "?"),
            "metadata_schema_version": data.get("metadata_schema_version"),
            "mtime": p.stat().st_mtime,
            "clip_count": len(alive),
            "deleted_count": len(clips) - len(alive),
            "include_raw_mic_0": bool(data.get("include_raw_mic_0", False)),
            "include_dtln": _metadata_flag(
                data, "include_dtln", DTLN_LEG, enabled_legs,
            ),
            "include_usb_mic": bool(data.get("include_usb_mic", False)),
            "include_usb_dtln": _metadata_flag(
                data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
            ),
            "include_xvf_raw0_dtln": _metadata_flag(
                data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
            ),
            "include_aec3_sweep": (
                bool(data.get("include_aec3_sweep", False))
                or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
            ),
            "corpus_profile": data.get("corpus_profile", PROFILE_STANDARD),
            "aec3_sweep_source": aec3_sweep_source,
            "enabled_legs": list(enabled_legs),
            "has_audio_context": bool(audio_context),
            "audio_profile_requested": audio_profile.get("requested"),
            "audio_profile_active": audio_profile.get("active"),
            "audio_profile_state": audio_profile.get("state"),
            "audio_validation_status": validation.get("status"),
            "capture_plan_recipe": capture_plan.get("recipe"),
            "capture_plan_resource_level": resource.get("level"),
            "conditions": conds,
            "is_active": (
                active_session_id is not None
                and data.get("session_id") == active_session_id
            ),
        })
    return out


def delete_session_files(target: Path, data: Mapping[str, Any]) -> tuple[int, int]:
    """Delete every non-deleted clip's WAV files plus the JSON sidecar
    itself. Returns (wavs_deleted, wavs_missing)."""
    wavs_deleted = 0
    wavs_missing = 0
    for c in data.get("clips", []):
        if c.get("deleted"):
            # Already-deleted clips have already had their WAVs
            # removed by delete_clip(); skip + don't count.
            continue
        for path_str in (c.get("files") or {}).values():
            p_wav = Path(path_str)
            try:
                p_wav.unlink()
                wavs_deleted += 1
            except FileNotFoundError:
                wavs_missing += 1
            except OSError as e:
                logger.warning("failed to delete %s: %s", p_wav, e)
                wavs_missing += 1
    target.unlink()
    return wavs_deleted, wavs_missing
