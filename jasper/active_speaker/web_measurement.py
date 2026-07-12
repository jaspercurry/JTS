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
import math
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


def _stored_ambient_report(
    wav_path: Path,
    sweep_meta: Mapping[str, Any],
    *,
    calibration: Any,
    ambient_duration_s: Any,
) -> dict[str, Any] | None:
    """Build post-deconvolution band noise from the relay's silent prefix.

    The relay orchestration pauses household audio and records a fixed silent
    window before playing the sweep.  The full uploaded WAV is already copied
    into every repeat/final bundle artifact, so the report identifies the
    exact source window rather than duplicating private raw audio.
    """

    try:
        duration = float(ambient_duration_s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    from jasper.audio_measurement import snr_policy
    from jasper.audio_measurement import sweep as sweep_mod

    samples, sample_rate = sweep_mod.read_wav_mono(wav_path)
    ambient_count = int(round(duration * sample_rate))
    if ambient_count <= 0 or len(samples) < ambient_count:
        raise ValueError("crossover capture is missing its stored ambient window")
    ambient = samples[:ambient_count]
    report = snr_policy.deconvolved_ambient_report(
        ambient,
        sample_rate,
        sweep_meta,
        calibration=calibration,
        capture_length_samples=len(samples),
    )
    raw_robust = snr_policy.ambient_band_report(ambient, sample_rate)
    return {
        **report,
        "source": {
            "kind": "capture_prefix",
            "start_s": 0.0,
            "end_s": round(duration, 3),
        },
        "raw_robust": raw_robust,
    }


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


def _capture_geometry(raw: Mapping[str, Any]) -> str:
    """Validate the SC-2 capture-geometry vocabulary from a browser request.

    Defaults to ``"near_field"`` (today's shipped driver capture) for any
    missing or unrecognized value, never propagating an arbitrary client
    string into the analysis layer.
    """
    geo = raw.get("capture_geometry")
    return geo if geo in ("near_field", "reference_axis") else "near_field"


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


def _resolve_bundle_for_capture(
    topology: Any,
    *,
    kind: str,
    group: str,
    role: str | None,
    calibration_id: str | None,
) -> tuple[Path | None, str | None]:
    """Resolve (or lazily open) the commissioning bundle for one capture.

    Prefers the ``bundle_session_id`` stamped on the active comparison set by
    ``measurement.start_active_comparison_set``; falls back to the newest
    still-``open`` bundle; lazily opens a fresh one if neither exists (this
    path does NOT re-stamp the comparison set — the capture record's
    ``bundle_ref`` is the durable link either way). Mints the deterministic
    capture path here so the SAME relative path is embedded in the
    measurement record's ``bundle_ref`` and later handed to
    ``bundles.append_capture`` — the on-disk WAV ends up at exactly the path
    the durable record points at.

    Fail-soft: returns ``(None, None)`` on any filesystem error or malformed
    ``topology`` (an ``AttributeError``/``TypeError`` from dereferencing an
    unexpected shape). Bundle resolution must never block a capture from
    being recorded — mirrors ``bundles.py``'s own fail-soft contract, which
    this helper is not itself part of but must uphold on its behalf.
    """

    from jasper.active_speaker import bundles as active_speaker_bundles
    from jasper.active_speaker.measurement import load_measurement_state

    try:
        root = active_speaker_bundles.sessions_dir()
        measurements = load_measurement_state(topology)
        comparison_set = measurements.get("active_comparison_set")
        session_id = (
            comparison_set.get("bundle_session_id")
            if isinstance(comparison_set, Mapping)
            else None
        )
        bundle_dir: Path | None = None
        if session_id:
            candidate = root / str(session_id)
            if (candidate / "info.json").is_file():
                bundle_dir = candidate
        if bundle_dir is None:
            latest = active_speaker_bundles.latest_bundle(root)
            if isinstance(latest, Mapping) and latest.get("state") == "open":
                bundle_dir = Path(str(latest["bundle_dir"]))
        if bundle_dir is None:
            opened = active_speaker_bundles.open_bundle(
                topology, calibration_id=calibration_id or "",
            )
            if opened is not None:
                bundle_dir = Path(str(opened["bundle_dir"]))
        if bundle_dir is None:
            return None, None
        relpath = active_speaker_bundles.capture_artifact_relpath(kind, group, role)
        return bundle_dir, relpath
    except (OSError, AttributeError, TypeError) as exc:
        log_event(
            logger,
            "active_speaker.bundle_write_failed",
            level=logging.WARNING,
            session=None,
            op="resolve_bundle_for_capture",
            error=str(exc),
        )
        return None, None


def _analyze_only_record(*_args: Any, **_kwargs: Any) -> None:
    """Injection used while a repeat is still provisional."""

    return None


def _driver_target_fingerprint(
    topology: Any, *, group_id: str, role: str
) -> str:
    from jasper.active_speaker.measurement import active_driver_targets

    target_id = f"{group_id}:{role}"
    for target in active_driver_targets(topology):
        if target.get("target_id") == target_id:
            return str(target.get("target_fingerprint") or "")
    return ""


def record_driver_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes | None = None,
    *,
    placement_proof: Mapping[str, Any] | None = None,
    preset: Any = None,
    repeat_store: Any = None,
) -> dict[str, Any]:
    """Analyze one browser WAV and record per-driver acoustic evidence.

    When ``repeat_store`` is supplied, provisional attempts are analyzed and
    bundled without touching durable measurement state.  Three accepted
    stationary repeats finalize the median representative; one bounded fourth
    attempt is automatic after a rejection, and fewer than two accepted
    captures refuse the run.  The key is ``comparison_set_id`` plus immutable
    target fingerprint, so attempts can never cross a level/profile context.
    """

    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.commissioning_capture import (
        DEFAULT_REPEAT_TARGET,
        aggregate_driver_repeats,
        record_driver_acoustic_capture,
        record_driver_repeat_aggregate,
    )
    from jasper.active_speaker.measurement import (
        current_driver_floor_evidence,
        load_measurement_state,
    )
    topology = load_output_topology()
    group_id = str(raw.get("speaker_group_id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    measurements = load_measurement_state(topology)
    floor_evidence = current_driver_floor_evidence(
        topology,
        measurements,
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
    sweep_meta = capture_sweep_meta(raw)
    ambient_report = _stored_ambient_report(
        wav_path,
        sweep_meta,
        calibration=calibration_curve,
        ambient_duration_s=raw.get("ambient_duration_s"),
    )
    if repeat_store is not None and ambient_report is None:
        raise ValueError(
            "crossover repeat capture requires a stored ambient window"
        )
    bundle_dir, capture_relpath = _resolve_bundle_for_capture(
        topology,
        kind="driver",
        group=group_id,
        role=role,
        calibration_id=calibration_id,
    )
    bundle_ref = (
        {"session_id": bundle_dir.name, "artifact_path": capture_relpath}
        if bundle_dir is not None
        else None
    )
    analysis_kwargs: dict[str, Any] = dict(
        speaker_group_id=group_id,
        role=role,
        captured_wav=wav_path,
        sweep_meta=sweep_meta,
        playback_id=_playback_id(raw),
        test_level_dbfs=raw.get("test_level_dbfs"),
        excitation=_mapping_value(raw.get("excitation")),
        has_mic_calibration=(
            bool(raw.get("has_mic_calibration")) or calibration_curve is not None
        ),
        calibration=calibration_curve,
        notes=raw.get("notes"),
        noise_floor_dbfs=raw.get("noise_floor_dbfs"),
        noise_band_report=(
            ambient_report
            if ambient_report is not None
            else _noise_band_report_value(raw.get("noise_band_report"))
        ),
        ambient_report=ambient_report,
        calibration_level=load_calibration_level_state(),
        safe_session=None,
        durable_floor_confirmation=floor_evidence.get("confirmation"),
        capture_geometry=_capture_geometry(raw),
    )
    if repeat_store is None:
        payload = record_driver_acoustic_capture(
            topology,
            preset,
            placement_proof=placement_proof,
            bundle_ref=bundle_ref,
            **analysis_kwargs,
        )
    else:
        comparison_set = measurements.get("active_comparison_set")
        comparison_set_id = (
            str(comparison_set.get("comparison_set_id") or "")
            if isinstance(comparison_set, Mapping)
            else ""
        )
        target_fingerprint = _driver_target_fingerprint(
            topology, group_id=group_id, role=role
        )
        if not comparison_set_id or not target_fingerprint:
            raise ValueError(
                "the crossover measurement context changed; run the level check again"
            )
        key = repeat_store.repeat_session_key(
            comparison_set_id, target_fingerprint
        )
        provisional = record_driver_acoustic_capture(
            topology,
            preset,
            placement_proof=placement_proof,
            bundle_ref=None,
            record=_analyze_only_record,
            emit_lifecycle_event=False,
            **analysis_kwargs,
        )
        artifact_path = None
        if bundle_dir is not None:
            from jasper.active_speaker import bundles as active_speaker_bundles

            appended = active_speaker_bundles.append_repeat_capture(
                bundle_dir,
                index=len(repeat_store.driver_repeats(key)),
                wav_source_path=wav_path,
                payload={
                    **provisional,
                    "speaker_group_id": group_id,
                    "role": role,
                },
            )
            if isinstance(appended, Mapping):
                artifact_path = appended.get("artifact_path")
        item = {
            "verdict": provisional.get("verdict"),
            "acoustic": provisional.get("acoustic"),
            "artifact_path": artifact_path,
            "wav_path": str(wav_path),
            "sweep_meta": dict(sweep_meta),
            "playback_id": _playback_id(raw),
            "test_level_dbfs": raw.get("test_level_dbfs"),
            "excitation": dict(_mapping_value(raw.get("excitation"))),
            "placement_proof": (
                dict(placement_proof) if isinstance(placement_proof, Mapping) else None
            ),
            "ambient_report": ambient_report,
        }
        repeats = repeat_store.append_driver_repeat(
            key,
            target_id=f"{group_id}:{role}",
            item=item,
        )
        aggregate = aggregate_driver_repeats(
            repeats, target=DEFAULT_REPEAT_TARGET
        )
        if aggregate["needed_recapture"]:
            return {
                "recorded": False,
                "verdict": provisional.get("verdict"),
                "acoustic": provisional.get("acoustic"),
                "repeat_progress": {
                    "attempts": len(repeats),
                    "accepted": aggregate["accepted"],
                    "target": DEFAULT_REPEAT_TARGET,
                    "bounded_recapture": len(repeats) >= DEFAULT_REPEAT_TARGET,
                },
            }
        aggregate = record_driver_repeat_aggregate(
            speaker_group_id=group_id,
            role=role,
            repeats=repeats,
            target=DEFAULT_REPEAT_TARGET,
            session_id=bundle_dir.name if bundle_dir is not None else None,
        )
        winner = aggregate.get("aggregate_repeat")
        if aggregate["accepted"] < 2 or not isinstance(winner, Mapping):
            failure = {
                "reason": "insufficient_accepted_repeats",
                "attempts": len(repeats),
                "accepted": aggregate["accepted"],
                "target": DEFAULT_REPEAT_TARGET,
                "per_repeat": aggregate["per_repeat"],
            }
            repeat_store.clear_driver_repeats(key)
            repeat_store.record_repeat_failure(f"{group_id}:{role}", failure)
            return {
                "recorded": False,
                "status": "refused",
                "verdict": "insufficient_repeats",
                "repeat_failure": failure,
            }
        winner_path = Path(str(winner.get("wav_path") or wav_path))
        if bundle_dir is not None and winner.get("artifact_path"):
            durable_winner = bundle_dir / str(winner["artifact_path"])
            if durable_winner.is_file():
                winner_path = durable_winner
        final_kwargs = dict(analysis_kwargs)
        final_kwargs.update({
            "captured_wav": winner_path,
            "sweep_meta": winner.get("sweep_meta") or sweep_meta,
            "playback_id": winner.get("playback_id"),
            "test_level_dbfs": winner.get("test_level_dbfs"),
            "excitation": winner.get("excitation"),
            "noise_band_report": winner.get("ambient_report"),
            "ambient_report": winner.get("ambient_report"),
        })
        payload = record_driver_acoustic_capture(
            topology,
            preset,
            placement_proof=winner.get("placement_proof"),
            bundle_ref=bundle_ref,
            repeats=aggregate,
            **final_kwargs,
        )
        wav_path = winner_path
        repeat_store.clear_driver_repeats(key)
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    if bundle_dir is not None:
        from jasper.active_speaker import bundles as active_speaker_bundles

        active_speaker_bundles.append_capture(
            bundle_dir,
            kind="driver",
            wav_source_path=wav_path,
            relative_path=capture_relpath,
            payload={**payload, "speaker_group_id": group_id, "role": role},
        )
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
        bundle_session=bundle_dir.name if bundle_dir is not None else None,
        repeats_accepted=_mapping_value(
            (payload.get("measurement") or {}).get("repeats")
        ).get("accepted"),
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
    sweep_meta = capture_sweep_meta(raw)
    ambient_report = _stored_ambient_report(
        wav_path,
        sweep_meta,
        calibration=calibration_curve,
        ambient_duration_s=raw.get("ambient_duration_s"),
    )
    group_id = str(raw.get("speaker_group_id") or "").strip()
    bundle_dir, capture_relpath = _resolve_bundle_for_capture(
        topology,
        kind="summed",
        group=group_id,
        role=None,
        calibration_id=calibration_id,
    )
    bundle_ref = (
        {"session_id": bundle_dir.name, "artifact_path": capture_relpath}
        if bundle_dir is not None
        else None
    )
    payload = record_summed_acoustic_capture(
        topology,
        preset,
        speaker_group_id=group_id,
        captured_wav=wav_path,
        sweep_meta=sweep_meta,
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
        noise_band_report=(
            ambient_report
            if ambient_report is not None
            else _noise_band_report_value(raw.get("noise_band_report"))
        ),
        ambient_report=ambient_report,
        calibration_level=load_calibration_level_state(),
        capture_geometry=_capture_geometry(raw),
        bundle_ref=bundle_ref,
    )
    payload["measurement_mode"] = measurement_mode
    payload["calibration_id"] = calibration_id
    if bundle_dir is not None:
        from jasper.active_speaker import bundles as active_speaker_bundles

        active_speaker_bundles.append_capture(
            bundle_dir,
            kind="summed",
            wav_source_path=wav_path,
            relative_path=capture_relpath,
            payload={**payload, "speaker_group_id": group_id},
        )
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
        bundle_session=bundle_dir.name if bundle_dir is not None else None,
    )
    return payload
