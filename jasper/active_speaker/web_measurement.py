# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable browser-mic evidence bridge for active-speaker measurements.

Browser measurement surfaces should not each own their own copy of "where do WAV
uploads live?" or "how does a browser WAV become active-speaker measurement
evidence?". This module is the narrow bridge: it stores bounded browser WAV
evidence, resolves the active-speaker preset and calibration mode, then calls
the domain analyzers that persist measurement state.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from jasper.log_event import log_event
from jasper.output_topology import load_output_topology

logger = logging.getLogger(__name__)

MAX_CAPTURE_WAV_BYTES = 3 * 1024 * 1024
MAX_CAPTURE_STORED_FILES = 24
MAX_CAPTURE_STORAGE_BYTES = 32 * 1024 * 1024
CAPTURE_FILE_MODE = 0o640
DEFAULT_ACTIVE_SPEAKER_CAPTURE_DIR = Path("/var/lib/jasper/active_speaker_captures")
ACTIVE_SPEAKER_CAPTURE_DIR_ENV = "JASPER_ACTIVE_SPEAKER_CAPTURE_DIR"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_capture_slug(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    out = "_".join(part for part in out.split("_") if part)
    return out[:64] or fallback


def capture_root() -> Path:
    """Return the active-speaker browser-capture storage root."""

    return Path(
        os.environ.get(ACTIVE_SPEAKER_CAPTURE_DIR_ENV)
        or DEFAULT_ACTIVE_SPEAKER_CAPTURE_DIR
    )


def _capture_mapping(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    capture = raw.get("capture")
    return capture if isinstance(capture, Mapping) else {}


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _noise_band_report_value(value: Any) -> list[dict[str, Any]] | None:
    """Validate a browser-supplied ``noise_band_report``.

    The correction-shape band list (see
    ``jasper.audio_measurement.snr_policy.band_levels_dbfs``) — a non-empty
    list of ``{band_id, band_hz: [lo, hi], level_dbfs}`` mappings. Anything
    else (missing, wrong shape, a malformed entry) resolves to ``None`` so a
    bad upload degrades the SC-1 SNR block to "unknown" evidence rather than
    computing from garbage — the same fail-closed posture as every other
    browser-supplied evidence field on this bridge.
    """
    if not isinstance(value, list) or not value:
        return None
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            return None
        band_id = entry.get("band_id")
        band_hz = entry.get("band_hz")
        level_dbfs = entry.get("level_dbfs")
        if (
            not isinstance(band_id, str)
            or not band_id
            or not isinstance(band_hz, (list, tuple))
            or len(band_hz) != 2
            or level_dbfs is None
        ):
            return None
        try:
            lo, hi = float(band_hz[0]), float(band_hz[1])
            level = float(level_dbfs)
        except (TypeError, ValueError):
            return None
        out.append({"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": level})
    return out


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _capture_store_files(root: Path) -> list[Path]:
    try:
        children = list(root.iterdir())
    except FileNotFoundError:
        return []
    return [
        child
        for child in children
        if child.is_file() and child.suffix.lower() == ".wav"
    ]


def _capture_sort_key(path: Path) -> tuple[float, str]:
    try:
        stat = path.stat()
    except OSError:
        return (0.0, path.name)
    return (stat.st_mtime, path.name)


def enforce_capture_retention(root: Path, *, keep: Path | None = None) -> None:
    """Keep browser capture storage bounded by count and bytes."""

    protected = keep.resolve() if keep is not None else None
    ordered: list[Path] = []
    protected_path: Path | None = None
    for path in sorted(
        _capture_store_files(root),
        key=_capture_sort_key,
        reverse=True,
    ):
        try:
            resolved = path.resolve()
        except OSError:
            ordered.append(path)
            continue
        if protected is not None and resolved == protected:
            protected_path = path
        else:
            ordered.append(path)
    if protected_path is not None:
        ordered.insert(0, protected_path)

    kept_count = 0
    kept_bytes = 0
    for path in ordered:
        try:
            resolved = path.resolve()
            size = path.stat().st_size
        except OSError:
            continue
        if protected is not None and resolved == protected:
            kept_count += 1
            kept_bytes += size
            continue
        if (
            kept_count < MAX_CAPTURE_STORED_FILES
            and kept_bytes + size <= MAX_CAPTURE_STORAGE_BYTES
        ):
            kept_count += 1
            kept_bytes += size
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _captured_path_from_request(raw: Mapping[str, Any]) -> Path | None:
    capture = _capture_mapping(raw)
    path_value = (
        raw.get("captured_wav_path")
        or raw.get("capture_wav_path")
        or capture.get("captured_wav_path")
        or capture.get("wav_path")
        or capture.get("path")
    )
    if not path_value:
        return None
    root = capture_root().resolve()
    candidate = Path(str(path_value)).expanduser().resolve()
    if not _is_relative_to(candidate, root):
        raise ValueError("capture WAV path must be inside active-speaker capture storage")
    if not candidate.is_file():
        raise ValueError("capture WAV file does not exist")
    if candidate.stat().st_size > MAX_CAPTURE_WAV_BYTES:
        raise ValueError("capture WAV file is too large")
    enforce_capture_retention(root, keep=candidate)
    return candidate


def _wav_bytes_from_request(
    raw: Mapping[str, Any],
    wav_bytes: bytes | None,
) -> bytes:
    if wav_bytes is not None:
        if not wav_bytes:
            raise ValueError("capture WAV evidence is empty")
        if len(wav_bytes) > MAX_CAPTURE_WAV_BYTES:
            raise ValueError("capture WAV upload is too large")
        return wav_bytes

    capture = _capture_mapping(raw)
    encoded = (
        raw.get("captured_wav_base64")
        or raw.get("capture_wav_base64")
        or capture.get("wav_base64")
        or capture.get("data")
    )
    if not encoded:
        raise ValueError("capture WAV evidence is missing")
    encoded_text = str(encoded)
    if encoded_text.startswith("data:"):
        _prefix, _sep, encoded_text = encoded_text.partition(",")
    try:
        decoded = base64.b64decode(encoded_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("capture WAV base64 is invalid") from exc
    if not decoded:
        raise ValueError("capture WAV evidence is empty")
    if len(decoded) > MAX_CAPTURE_WAV_BYTES:
        raise ValueError("capture WAV upload is too large")
    return decoded


def capture_wav_path(
    raw: Mapping[str, Any],
    *,
    kind: str,
    wav_bytes: bytes | None = None,
) -> Path:
    """Persist or validate browser WAV evidence and return its local path."""

    existing = _captured_path_from_request(raw)
    if existing is not None:
        return existing

    body = _wav_bytes_from_request(raw, wav_bytes)
    root = capture_root()
    root.mkdir(parents=True, exist_ok=True)
    group = _safe_capture_slug(raw.get("speaker_group_id"), fallback="group")
    role = _safe_capture_slug(raw.get("role"), fallback="target")
    target = root / f"{kind}_{group}_{role}_{uuid.uuid4().hex}.wav"
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(body)
        os.chmod(tmp, CAPTURE_FILE_MODE)
        os.replace(tmp, target)
        os.chmod(target, CAPTURE_FILE_MODE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    enforce_capture_retention(root, keep=target)
    return target


def capture_sweep_meta(raw: Mapping[str, Any]) -> dict[str, Any]:
    capture = _capture_mapping(raw)
    sweep_meta = raw.get("sweep_meta") or capture.get("sweep_meta")
    if not isinstance(sweep_meta, Mapping):
        from jasper.active_speaker import driver_acoustics as acoustic
        from jasper.audio_measurement import sweep as sweep_mod

        _signal, meta = sweep_mod.synchronized_swept_sine(
            f1=acoustic.DEFAULT_F1_HZ,
            f2=acoustic.DEFAULT_F2_HZ,
            duration_approx_s=acoustic.DEFAULT_DURATION_S,
            sample_rate=acoustic.DEFAULT_SAMPLE_RATE,
            amplitude_dbfs=acoustic.DEFAULT_AMPLITUDE_DBFS,
        )
        return meta.to_dict()
    required = {"sample_rate", "n_samples", "f1", "f2", "duration_s", "amplitude_dbfs"}
    missing = sorted(key for key in required if key not in sweep_meta)
    if missing:
        raise ValueError("capture sweep metadata is incomplete")
    return dict(sweep_meta)


def capture_preset(topology: Any, frozen_preset: Any = None) -> Any:
    """Resolve the active-speaker preset used to analyze browser captures."""

    if frozen_preset is not None:
        frozen_preset.validate()
        return frozen_preset

    from jasper.active_speaker.commission_wiring import resolve_commission_inputs
    from jasper.active_speaker.tone_plan import load_active_speaker_preset

    preset, crossover_preview = resolve_commission_inputs()
    if preset is not None:
        return preset
    if crossover_preview is not None:
        from jasper.active_speaker.staging import compile_preset_from_crossover_preview

        compiled, issues, _gates = compile_preset_from_crossover_preview(
            topology,
            crossover_preview,
        )
        if compiled is not None:
            return compiled
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in issues
            if isinstance(issue, Mapping)
        ]
        raise ValueError(
            "active speaker preset is not ready for capture analysis"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return load_active_speaker_preset(
        os.environ.get("JASPER_ACTIVE_SPEAKER_PRESET") or None
    )


def capture_calibration(
    raw: Mapping[str, Any],
) -> tuple[Any, str | None, dict[str, Any]]:
    """Resolve calibration curve/id plus the phase-aware/magnitude-only mode."""

    from jasper.active_speaker.crossover_alignment import resolve_measurement_mode

    calibration_id = str(raw.get("calibration_id") or "").strip()
    curve = None
    resolved_id: str | None = None
    if calibration_id:
        from jasper.audio_measurement.calibration import load_calibration_record

        try:
            record = load_calibration_record(calibration_id)
        except (FileNotFoundError, ValueError, OSError):
            record = None
        if record is not None:
            curve = record.curve
            resolved_id = record.calibration_id
    mode = resolve_measurement_mode(
        raw.get("measurement_mode"), has_calibrated_mic=curve is not None
    )
    return curve, resolved_id, mode.to_dict()


def _playback_id(raw: Mapping[str, Any]) -> str | None:
    playback = _mapping_value(raw.get("playback"))
    value = raw.get("playback_id") or playback.get("playback_id")
    return str(value).strip() if value else None


def _summed_test_id(raw: Mapping[str, Any]) -> str | None:
    playback = _mapping_value(raw.get("playback"))
    value = (
        raw.get("summed_test_id")
        or raw.get("playback_id")
        or playback.get("summed_test_id")
        or playback.get("playback_id")
    )
    return str(value).strip() if value else None


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets plus saved measurement evidence."""

    from jasper.active_speaker.measurement import (
        active_driver_targets,
        active_summed_targets,
        load_measurement_state,
    )

    topology = load_output_topology()
    measurements = load_measurement_state(topology)
    return {
        "ok": True,
        "generated_at": _utc_now(),
        "topology": {
            "topology_id": topology.topology_id,
            "status": topology.status,
        },
        "targets": {
            "drivers": active_driver_targets(topology),
            "summed": active_summed_targets(topology),
        },
        "measurements": measurements,
    }


def record_driver_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes | None = None,
    *,
    placement_proof: Mapping[str, Any] | None = None,
    preset: Any = None,
) -> dict[str, Any]:
    """Analyze one browser WAV and record per-driver acoustic evidence."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_capture import (
        record_driver_acoustic_capture,
    )
    from jasper.active_speaker.measurement import (
        current_driver_floor_evidence,
        load_measurement_state,
    )
    topology = load_output_topology()
    group_id = str(raw.get("speaker_group_id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    floor_evidence = current_driver_floor_evidence(
        topology,
        load_measurement_state(topology),
        speaker_group_id=group_id,
        role=role,
    )
    if floor_evidence.get("valid") is not True:
        log_event(
            logger,
            "active_speaker.web_driver_capture",
            level=logging.WARNING,
            status="refused",
            group_id=group_id,
            role=role,
            floor_evidence_source=floor_evidence.get("source"),
            reason=floor_evidence.get("reason"),
        )
        raise ValueError(
            str(
                floor_evidence.get("detail")
                or "confirm this driver again before recording mic evidence"
            )
        )
    preset = capture_preset(topology, preset)
    wav_path = capture_wav_path(raw, kind="driver", wav_bytes=wav_bytes)
    calibration_curve, calibration_id, measurement_mode = capture_calibration(raw)
    payload = record_driver_acoustic_capture(
        topology,
        preset,
        speaker_group_id=group_id,
        role=role,
        captured_wav=wav_path,
        sweep_meta=capture_sweep_meta(raw),
        playback_id=_playback_id(raw),
        test_level_dbfs=raw.get("test_level_dbfs"),
        excitation=_mapping_value(raw.get("excitation")),
        placement_proof=placement_proof,
        has_mic_calibration=(
            bool(raw.get("has_mic_calibration")) or calibration_curve is not None
        ),
        calibration=calibration_curve,
        notes=raw.get("notes"),
        noise_floor_dbfs=raw.get("noise_floor_dbfs"),
        noise_band_report=_noise_band_report_value(raw.get("noise_band_report")),
        calibration_level=load_calibration_level_state(),
        safe_session=None,
        durable_floor_confirmation=floor_evidence.get("confirmation"),
    )
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    log_event(
        logger,
        "active_speaker.web_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=group_id,
        role=role,
        verdict=payload.get("verdict"),
        excitation_source=(payload.get("excitation") or {}).get("gain_source"),
        effective_peak_dbfs=(payload.get("excitation") or {}).get(
            "effective_peak_dbfs"
        ),
        placement_schema=(payload.get("placement_proof") or {}).get(
            "schema_version"
        ),
        placement_policy=(payload.get("placement_proof") or {}).get("policy_id"),
        floor_evidence_source=floor_evidence.get("source"),
    )
    return payload


def record_summed_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes | None = None,
    *,
    placement_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze one browser WAV and record summed-crossover evidence."""

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_capture import (
        record_summed_acoustic_capture,
    )

    topology = load_output_topology()
    preset = capture_preset(topology)
    wav_path = capture_wav_path(raw, kind="summed", wav_bytes=wav_bytes)
    calibration_curve, calibration_id, measurement_mode = capture_calibration(raw)
    group_id = str(raw.get("speaker_group_id") or "").strip()
    payload = record_summed_acoustic_capture(
        topology,
        preset,
        speaker_group_id=group_id,
        captured_wav=wav_path,
        sweep_meta=capture_sweep_meta(raw),
        crossover_fc_hz=raw.get("crossover_fc_hz"),
        summed_test_id=_summed_test_id(raw),
        playback_id=_playback_id(raw),
        excitation=_mapping_value(raw.get("excitation")),
        placement_proof=placement_proof,
        polarity=raw.get("polarity"),
        delay_ms=raw.get("delay_ms"),
        delay_target_role=raw.get("delay_target_role"),
        expect_null=bool(raw.get("expect_null")),
        has_mic_calibration=(
            bool(raw.get("has_mic_calibration")) or calibration_curve is not None
        ),
        calibration=calibration_curve,
        notes=raw.get("notes"),
        noise_floor_dbfs=raw.get("noise_floor_dbfs"),
        noise_band_report=_noise_band_report_value(raw.get("noise_band_report")),
        calibration_level=load_calibration_level_state(),
    )
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    log_event(
        logger,
        "active_speaker.web_summed_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=group_id,
        verdict=payload.get("verdict"),
        excitation_source=(payload.get("excitation") or {}).get("gain_source"),
        excitation_scope=(payload.get("excitation") or {}).get("scope"),
        placement_schema=(payload.get("placement_proof") or {}).get(
            "schema_version"
        ),
        placement_policy=(payload.get("placement_proof") or {}).get("policy_id"),
        noise_floor_dbfs=(payload.get("acoustic") or {}).get("noise_floor_dbfs"),
    )
    return payload
