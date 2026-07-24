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
from typing import Any, Callable, Mapping

from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.log_event import log_event
from jasper.output_topology import load_output_topology
from jasper.active_speaker.test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

logger = logging.getLogger(__name__)

MAX_CAPTURE_WAV_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES
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
    """Validate and describe the relay's controlled pre-sweep interval.

    This is a protocol-intent stub only. The acoustic analyzer locates the
    sweep from the signal and persists the exact quiet-window sample offsets;
    the beginning of the phone WAV is deliberately never treated as ambient.
    """

    try:
        duration = float(ambient_duration_s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    from jasper.audio_measurement import sweep as sweep_mod

    samples, sample_rate = sweep_mod.read_wav_mono(wav_path)
    ambient_count = int(round(duration * sample_rate))
    reference_count = int(sweep_meta.get("n_samples") or 0)
    if (
        ambient_count <= 0
        or reference_count <= 0
        or len(samples) < ambient_count + reference_count
    ):
        raise ValueError("crossover capture is missing its controlled ambient interval")
    return {
        "schema_version": 2,
        "domain": "controlled_pre_sweep",
        "method": "paired_signal_window_deconvolution",
        "ambient_duration_s": round(duration, 3),
        "source": {
            "kind": "pending_signal_boundary",
            "protocol_paused_duration_s": round(duration, 3),
        },
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


def capture_preset(
    topology: Any,
    frozen_preset: ActiveSpeakerPreset | None = None,
) -> ActiveSpeakerPreset:
    """Resolve the active-speaker preset used to analyze browser captures."""

    if frozen_preset is not None:
        frozen_preset.validate()
        return frozen_preset

    from jasper.active_speaker.commission_wiring import resolve_capture_preset

    return resolve_capture_preset(topology)


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


def driver_analysis_input_evidence(
    *,
    sweep_meta: Mapping[str, Any],
    excitation: Mapping[str, Any] | None,
    calibration_curve: Any,
    calibration_id: str | None,
    capture_geometry: str,
    ambient_duration_s: float | None,
) -> dict[str, Any]:
    """Return the lossless replay contract stored beside a driver WAV.

    ``acoustic.fr_curve`` is intentionally peak-normalized for display. LF
    splice analysis must instead replay the immutable raw WAV with the exact
    generated sweep, calibrated amplitude, and played-level ledger captured
    here. The calibration snapshot contains no serial or vendor URL.
    """

    curve_to_dict = getattr(calibration_curve, "to_dict", None)
    curve = curve_to_dict() if callable(curve_to_dict) else None
    return {
        "schema_version": 1,
        "response_amplitude": "recompute_from_raw_wav",
        "display_fr_curve_peak_normalized": True,
        "sweep_meta": dict(sweep_meta),
        "excitation": dict(excitation or {}),
        "calibration": (
            {
                "calibration_id": str(calibration_id or ""),
                "curve": curve,
            }
            if curve is not None
            else None
        ),
        "capture_geometry": str(capture_geometry),
        "ambient_duration_s": (
            float(ambient_duration_s)
            if ambient_duration_s is not None
            else None
        ),
    }


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


def _driver_capture_geometry(
    placement_proof: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None = None,
    *,
    speaker_group_id: str = "",
    role: str = "",
    target_fingerprint: str = "",
) -> str:
    """Return the analyzer geometry bound by server-owned placement proof."""

    from jasper.active_speaker.capture_geometry import driver_capture_geometry

    return driver_capture_geometry(
        placement_proof,
        active_comparison_set,
        speaker_group_id=speaker_group_id,
        role=role,
        target_fingerprint=target_fingerprint,
    )


def _summed_capture_geometry(
    placement_proof: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None = None,
    *,
    speaker_group_id: str = "",
    target_fingerprint: str = "",
) -> str:
    """Return summed geometry bound by the server-owned placement proof."""

    from jasper.active_speaker.capture_geometry import summed_capture_geometry

    return summed_capture_geometry(
        placement_proof,
        active_comparison_set,
        speaker_group_id=speaker_group_id,
        target_fingerprint=target_fingerprint,
    )


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


def _summed_target_fingerprint(topology: Any, *, group_id: str) -> str:
    from jasper.active_speaker.measurement import active_summed_targets

    for target in active_summed_targets(topology):
        if target.get("speaker_group_id") == group_id:
            return str(target.get("group_fingerprint") or "")
    return ""


def _finalize_driver_repeat_set(
    *,
    topology: Any,
    comparison_set: Mapping[str, Any],
    speaker_group_id: str,
    role: str,
    topology_target_fingerprint: str,
    repeat_target_id: str,
    repeat_target_fingerprint: str,
    reservation: Mapping[str, Any],
    admission_result: Mapping[str, Any],
    repeats: list[dict[str, Any]],
    repeat_store: Any,
    terminal_failure_type: str | None = None,
) -> dict[str, Any]:
    """One ready → measurement → complete transaction for every final path."""

    from jasper.active_speaker import bundles as active_speaker_bundles
    from jasper.active_speaker import repeat_admission
    from jasper.active_speaker.commissioning_capture import (
        DEFAULT_REPEAT_TARGET,
        record_driver_acoustic_capture,
        record_driver_repeat_aggregate,
    )

    comparison_set_id = str(comparison_set.get("comparison_set_id") or "")
    reservation_attempt = int(reservation.get("attempt") or 0)
    key = repeat_store.repeat_session_key(
        comparison_set_id, repeat_target_fingerprint
    )
    aggregate = record_driver_repeat_aggregate(
        speaker_group_id=speaker_group_id,
        role=role,
        repeats=repeats,
        target=DEFAULT_REPEAT_TARGET,
        session_id=(
            Path(str(repeats[-1]["bundle_dir"])).name
            if repeats[-1].get("bundle_dir")
            else None
        ),
    )
    # Record the MEASUREMENT-attempt count (audio-emitting captures that went
    # into this set), not the raw reservation number: a set that survived
    # refunded transport failures can carry a reservation attempt above
    # MAX_ATTEMPTS, and `driver_acoustic_usable` gates `admission_attempts` on
    # [DEFAULT_REPEAT_TARGET, MAX_ATTEMPTS]. Every `per_repeat` entry is an
    # audio-emitting attempt (transport failures never reach the store), so its
    # length is exactly the measurement budget consumed.
    aggregate["admission_attempts"] = len(aggregate.get("per_repeat") or ())
    winner = aggregate.get("aggregate_repeat")
    if aggregate["accepted"] < 2 or not isinstance(winner, Mapping):
        raise RuntimeError("driver repeat set is not eligible for finalization")
    analysis_kwargs = winner.get("analysis_kwargs")
    if "preset" not in winner:
        raise RuntimeError("accepted crossover repeats lost their analysis preset")
    preset = winner["preset"]
    if not isinstance(analysis_kwargs, Mapping):
        raise RuntimeError("accepted crossover repeats lost finalization context")
    winner_proof = winner.get("placement_proof")
    winner_acoustic = _mapping_value(winner.get("acoustic"))
    if not isinstance(winner_proof, Mapping):
        raise RuntimeError("accepted crossover repeats lost their placement proof")
    try:
        final_geometry = _driver_capture_geometry(
            winner_proof,
            comparison_set,
            speaker_group_id=speaker_group_id,
            role=role,
            target_fingerprint=topology_target_fingerprint,
        )
    except ValueError as exc:
        raise RuntimeError(
            "accepted crossover repeats have an invalid or stale placement proof"
        ) from exc
    if winner_acoustic.get("capture_geometry") != final_geometry:
        raise RuntimeError(
            "accepted crossover repeats have conflicting placement geometry"
        )
    winner_path = Path(str(winner.get("wav_path") or ""))
    bundle_dir = (
        Path(str(winner["bundle_dir"])) if winner.get("bundle_dir") else None
    )
    final_bundle_ref = None
    if bundle_dir is not None and winner.get("artifact_path"):
        durable_winner = bundle_dir / str(winner["artifact_path"])
        if durable_winner.is_file():
            winner_path = durable_winner
            final_bundle_ref = {
                "session_id": bundle_dir.name,
                "artifact_path": str(winner["artifact_path"]),
            }
    final_kwargs = dict(analysis_kwargs)
    final_kwargs.update({
        "captured_wav": winner_path,
        "sweep_meta": winner.get("sweep_meta"),
        "playback_id": winner.get("playback_id"),
        "test_level_dbfs": winner.get("test_level_dbfs"),
        "excitation": winner.get("excitation"),
        "noise_band_report": None,
        "ambient_report": winner.get("ambient_report"),
        "ambient_duration_s": winner.get("ambient_duration_s"),
    })
    repeat_admission.finish(
        comparison_set,
        target_id=repeat_target_id,
        target_fingerprint=repeat_target_fingerprint,
        token=str(reservation.get("token") or ""),
        result=admission_result,
        status="ready",
    )
    try:
        payload = record_driver_acoustic_capture(
            topology,
            preset,
            placement_proof=winner.get("placement_proof"),
            bundle_ref=final_bundle_ref,
            repeats=aggregate,
            **final_kwargs,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # ``finish(status="ready")`` above already consumed this audible
        # attempt. Final persistence failure must stay terminal: publish an
        # actionable abort without reopening reservation, then propagate the
        # original exception to the request boundary. A new level/comparison
        # run is the only supported recovery and starts again at attempt one.
        try:
            repeat_admission.abort_ready(
                comparison_set,
                target_id=repeat_target_id,
                target_fingerprint=repeat_target_fingerprint,
                reason="measurement_persistence_failed",
            )
        except (OSError, RuntimeError, ValueError) as abort_exc:
            # If even the abort write fails, the durable state remains ``ready``
            # and therefore still blocks replay. Service-start ownership claim
            # retires an old-owner ready state after a crash/restart.
            log_event(
                logger,
                "correction.crossover_repeat_abort_failed",
                level=logging.ERROR,
                target=repeat_target_id,
                attempts=reservation_attempt,
                failure_type=type(abort_exc).__name__,
                origin_failure_type=type(exc).__name__,
                reason="measurement_persistence_failed",
            )
        raise
    # Keep completion outside the measurement-persistence recovery block so a
    # completed measurement is never mislabeled as a measurement-write
    # failure.  A failed admission completion is still actionable in this
    # process: retire ``ready`` with its own reason. Only a second failed abort
    # write leaves ``ready`` fail-closed for startup ownership recovery.
    try:
        repeat_admission.complete(
            comparison_set,
            target_id=repeat_target_id,
            target_fingerprint=repeat_target_fingerprint,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            repeat_admission.abort_ready(
                comparison_set,
                target_id=repeat_target_id,
                target_fingerprint=repeat_target_fingerprint,
                reason="repeat_completion_failed",
            )
        except (OSError, RuntimeError, ValueError) as abort_exc:
            log_event(
                logger,
                "correction.crossover_repeat_abort_failed",
                level=logging.ERROR,
                target=repeat_target_id,
                attempts=reservation_attempt,
                failure_type=type(abort_exc).__name__,
                origin_failure_type=type(exc).__name__,
                reason="repeat_completion_failed",
            )
        raise
    repeat_store.clear_driver_repeats(key)
    payload["measurement_mode"] = winner.get("measurement_mode")
    payload["calibration_id"] = winner.get("calibration_id")
    if bundle_dir is not None:
        active_speaker_bundles.record_repeat_progress(
            bundle_dir,
            comparison_set_id=comparison_set_id,
            target_fingerprint=repeat_target_fingerprint,
            target_id=repeat_target_id,
            attempts=reservation_attempt,
            accepted=aggregate["accepted"],
            target=DEFAULT_REPEAT_TARGET,
            per_repeat=aggregate["per_repeat"],
            status="completed",
            reason=(
                "terminal_transport_failure_used_existing_repeats"
                if terminal_failure_type
                else None
            ),
        )
        capture_relpath = winner.get("capture_relpath")
        if capture_relpath:
            active_speaker_bundles.append_capture(
                bundle_dir,
                kind="driver",
                wav_source_path=winner_path,
                relative_path=str(capture_relpath),
                payload={
                    **payload,
                    "speaker_group_id": speaker_group_id,
                    "role": role,
                    "analysis_input": driver_analysis_input_evidence(
                        sweep_meta=_mapping_value(winner.get("sweep_meta")),
                        excitation=_mapping_value(payload.get("excitation")),
                        calibration_curve=analysis_kwargs.get("calibration"),
                        calibration_id=(
                            str(winner.get("calibration_id") or "") or None
                        ),
                        capture_geometry=final_geometry,
                        ambient_duration_s=winner.get("ambient_duration_s"),
                    ),
                },
            )
    log_event(
        logger,
        "active_speaker.web_driver_capture",
        status="recorded" if payload.get("recorded") else "not_recorded",
        group_id=speaker_group_id,
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
        floor_evidence_source=winner.get("floor_evidence_source"),
        bundle_session=bundle_dir.name if bundle_dir is not None else None,
        repeats_accepted=aggregate["accepted"],
    )
    if terminal_failure_type:
        log_event(
            logger,
            "correction.crossover_repeats_finalized_after_transport_failure",
            group=speaker_group_id,
            role=role,
            attempt=reservation_attempt,
            accepted=aggregate["accepted"],
            failure_type=str(terminal_failure_type)[:80],
        )
    return payload


def finalize_driver_repeats_after_terminal_failure(
    *,
    comparison_set: Mapping[str, Any],
    speaker_group_id: str,
    role: str,
    target_fingerprint: str,
    capture_geometry: str,
    reservation: Mapping[str, Any],
    failure_type: str,
    repeat_store: Any,
) -> dict[str, Any] | None:
    """Finalize a near-field set from its existing accepted repeats once the
    audible measurement budget is spent (a transport failure hit the last
    admissible audio attempt)."""

    from jasper.active_speaker import repeat_admission
    from jasper.active_speaker.commissioning_capture import (
        DEFAULT_REPEAT_TARGET,
        aggregate_driver_repeats,
    )

    if capture_geometry == "reference_axis":
        # Fixed-axis admitted captures feed strict commissioning authority,
        # whose load-bearing contract is three fresh accepted repetitions.
        # The caller will terminally refuse this attempt rather than publishing
        # a legacy completion that strict status cannot promote.
        return None
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
        speaker_group_id=speaker_group_id,
        role=role,
        target_fingerprint=target_fingerprint,
        capture_geometry=capture_geometry,
    )
    key = repeat_store.repeat_session_key(
        str(comparison_set.get("comparison_set_id") or ""),
        repeat_target_fingerprint,
    )
    repeats = repeat_store.driver_repeats(key)
    # Salvage only once the audible measurement budget is genuinely spent: a
    # transport/infra failure that never played a tone is refunded and does not
    # exhaust it, so a set that could still earn its third accept is never
    # prematurely completed from two. Every stored repeat is an audio-emitting
    # capture (transport failures never reach the store), so
    # measurement_attempts == len(repeats).
    if repeat_admission.measurement_attempts(repeats) < repeat_admission.MAX_ATTEMPTS:
        return None
    preview = aggregate_driver_repeats(repeats, target=DEFAULT_REPEAT_TARGET)
    if preview["accepted"] < 2:
        return None
    topology = load_output_topology()
    if _driver_target_fingerprint(
        topology,
        group_id=speaker_group_id,
        role=role,
    ) != target_fingerprint:
        raise ValueError("the driver target changed before repeat finalization")
    from jasper.active_speaker.measurement import load_measurement_state

    current_comparison = load_measurement_state(topology).get(
        "active_comparison_set"
    )
    if not isinstance(current_comparison, Mapping) or any(
        current_comparison.get(key) != comparison_set.get(key)
        for key in ("comparison_set_id", "fingerprint")
    ):
        raise ValueError("the comparison set changed before repeat finalization")
    return _finalize_driver_repeat_set(
        topology=topology,
        comparison_set=comparison_set,
        speaker_group_id=speaker_group_id,
        role=role,
        topology_target_fingerprint=target_fingerprint,
        repeat_target_id=repeat_target_id,
        repeat_target_fingerprint=repeat_target_fingerprint,
        reservation=reservation,
        admission_result={
            "accepted": False,
            "reject_reason": "capture_failed",
            "failure_type": str(failure_type)[:80],
            "phase": "transport",
            # A transport/infra failure provably played no tone, so it is
            # refunded from the audible measurement budget (see
            # repeat_admission.measurement_attempts).
            "audio_emitted": False,
        },
        repeats=repeats,
        repeat_store=repeat_store,
        terminal_failure_type=failure_type,
    )


def _record_authoritative_driver_capture(
    *,
    recorder: Callable[..., Mapping[str, Any]] | None,
    capture_geometry: str,
    accepted: bool,
    admission_handoff: Mapping[str, Any] | None,
    inputs: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Invoke strict promotion only for fresh accepted fixed-axis evidence."""

    if (
        recorder is None
        or capture_geometry != "reference_axis"
        or accepted is not True
    ):
        return None
    if admission_handoff is None:
        raise ValueError(
            "fixed-axis commissioning capture lacks admitted playback proof"
        )
    return dict(
        recorder(
            **dict(inputs),
            admission_handoff=admission_handoff,
        )
    )


def record_driver_capture(
    raw: Mapping[str, Any],
    wav_bytes: bytes | None = None,
    *,
    placement_proof: Mapping[str, Any] | None = None,
    admission_handoff: Mapping[str, Any] | None = None,
    preset: Any = None,
    repeat_store: Any = None,
    authoritative_recorder: Callable[..., Mapping[str, Any]] | None = None,
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
    )
    from jasper.active_speaker.measurement import (
        current_driver_floor_evidence,
        load_measurement_state,
    )
    topology = load_output_topology()
    group_id = str(raw.get("speaker_group_id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    repeat_failure_reader = getattr(repeat_store, "repeat_failure", None)
    legacy_repeat_target_id = f"{group_id}:{role}"
    if callable(repeat_failure_reader) and isinstance(
        repeat_failure_reader(legacy_repeat_target_id), Mapping
    ):
        raise ValueError(
            "the bounded crossover repeat set was refused or interrupted; "
            "run the driver level check again before measuring"
        )
    measurements = load_measurement_state(topology)
    comparison_set = measurements.get("active_comparison_set")
    target_fingerprint = (
        _driver_target_fingerprint(topology, group_id=group_id, role=role)
        if placement_proof is not None
        else ""
    )
    validated_admission: dict[str, Any] | None = None
    if admission_handoff is not None:
        if not isinstance(comparison_set, Mapping):
            raise ValueError("capture admission has no current comparison set")
        from jasper.active_speaker.commissioning_admission import (
            validate_capture_admission_handoff,
        )

        validated_admission = validate_capture_admission_handoff(
            admission_handoff,
            topology=topology,
            comparison_set=comparison_set,
            speaker_group_id=group_id,
            role=role,
        )
    capture_geometry = _driver_capture_geometry(
        placement_proof,
        comparison_set if isinstance(comparison_set, Mapping) else None,
        speaker_group_id=group_id,
        role=role,
        target_fingerprint=target_fingerprint,
    )
    repeat_target_id = legacy_repeat_target_id
    repeat_target_fingerprint = target_fingerprint
    if repeat_store is not None and target_fingerprint:
        from jasper.active_speaker.capture_geometry import driver_repeat_binding

        repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
            speaker_group_id=group_id,
            role=role,
            target_fingerprint=target_fingerprint,
            capture_geometry=capture_geometry,
        )
    prior_repeat_failure = (
        repeat_failure_reader(repeat_target_id)
        if callable(repeat_failure_reader)
        and repeat_target_id != legacy_repeat_target_id
        else None
    )
    if isinstance(prior_repeat_failure, Mapping):
        raise ValueError(
            "the bounded crossover repeat set was refused or interrupted; "
            "run the driver level check again before measuring"
        )
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
            "crossover repeat capture requires a controlled pre-sweep quiet interval"
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
            _noise_band_report_value(raw.get("noise_band_report"))
            if ambient_report is None
            else None
        ),
        ambient_report=ambient_report,
        ambient_duration_s=(
            float(raw["ambient_duration_s"])
            if ambient_report is not None
            else None
        ),
        calibration_level=load_calibration_level_state(),
        safe_session=None,
        durable_floor_confirmation=floor_evidence.get("confirmation"),
        capture_geometry=capture_geometry,
        capture_admission=validated_admission,
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
        if not isinstance(comparison_set, Mapping):
            raise ValueError(
                "the crossover measurement context or repeat admission changed; "
                "run the level check again"
            )
        if not target_fingerprint:
            target_fingerprint = _driver_target_fingerprint(
                topology, group_id=group_id, role=role
            )
            from jasper.active_speaker.capture_geometry import driver_repeat_binding

            repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
                speaker_group_id=group_id,
                role=role,
                target_fingerprint=target_fingerprint,
                capture_geometry=capture_geometry,
            )
        from jasper.active_speaker import repeat_admission

        comparison_set_id = str(comparison_set.get("comparison_set_id") or "")
        reservation = raw.get("repeat_reservation")
        reservation = reservation if isinstance(reservation, Mapping) else {}
        reservation_token = str(reservation.get("token") or "")
        reservation_attempt = int(reservation.get("attempt") or 0)
        if (
            not comparison_set_id
            or not target_fingerprint
            or not reservation_token
            or not 1 <= reservation_attempt <= repeat_admission.MAX_RESERVATIONS
            or reservation.get("target_id") != repeat_target_id
            or reservation.get("target_fingerprint")
            != repeat_target_fingerprint
        ):
            raise ValueError(
                "the crossover measurement context or repeat admission changed; "
                "run the level check again"
            )
        key = repeat_store.repeat_session_key(
            comparison_set_id, repeat_target_fingerprint
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
        analysis_input = driver_analysis_input_evidence(
            sweep_meta=sweep_meta,
            excitation=_mapping_value(provisional.get("excitation")),
            calibration_curve=calibration_curve,
            calibration_id=calibration_id,
            capture_geometry=str(analysis_kwargs["capture_geometry"]),
            ambient_duration_s=analysis_kwargs.get("ambient_duration_s"),
        )
        authoritative_evidence: dict[str, Any] | None = None
        artifact_path = None
        if bundle_dir is not None:
            from jasper.active_speaker import bundles as active_speaker_bundles

            appended = active_speaker_bundles.append_repeat_capture(
                bundle_dir,
                index=reservation_attempt - 1,
                wav_source_path=wav_path,
                payload={
                    **provisional,
                    "speaker_group_id": group_id,
                    "role": role,
                    "analysis_input": analysis_input,
                },
            )
            if isinstance(appended, Mapping):
                artifact_path = appended.get("artifact_path")
        item = {
            "attempt": reservation_attempt,
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
            "capture_admission": validated_admission,
            "ambient_report": (
                _mapping_value(provisional.get("acoustic")).get("ambient")
                or ambient_report
            ),
            "ambient_duration_s": analysis_kwargs.get("ambient_duration_s"),
            # Process-local continuation data for the bounded-attempt state
            # machine. A terminal transport failure can still finalize two
            # already-accepted acoustic repeats without replaying audio.
            "analysis_kwargs": dict(analysis_kwargs),
            "preset": preset,
            "calibration_id": calibration_id,
            "measurement_mode": measurement_mode,
            "bundle_dir": str(bundle_dir) if bundle_dir is not None else None,
            "capture_relpath": capture_relpath,
            "floor_evidence_source": floor_evidence.get("source"),
        }
        repeats = repeat_store.append_driver_repeat(
            key,
            target_id=repeat_target_id,
            item=item,
            attempt=reservation_attempt,
        )
        aggregate = aggregate_driver_repeats(
            repeats, target=DEFAULT_REPEAT_TARGET
        )
        # Every stored repeat is an audio-emitting capture (transport failures
        # never reach the store), so this is the MEASUREMENT-attempt count —
        # the audible budget consumed. It drives the recapture-vs-finalize
        # decision instead of the raw reservation number so that refunded
        # transport failures do not prematurely exhaust the budget.
        measurement_attempt_count = len(aggregate["per_repeat"])
        latest_attempt = aggregate["per_repeat"][-1]
        def persist_repeat_progress(status: str, reason: str | None = None) -> None:
            if bundle_dir is None:
                return
            from jasper.active_speaker import bundles as active_speaker_bundles

            active_speaker_bundles.record_repeat_progress(
                bundle_dir,
                comparison_set_id=comparison_set_id,
                target_fingerprint=repeat_target_fingerprint,
                target_id=repeat_target_id,
                attempts=reservation_attempt,
                accepted=aggregate["accepted"],
                target=DEFAULT_REPEAT_TARGET,
                per_repeat=aggregate["per_repeat"],
                status=status,
                reason=reason,
            )

        acoustic_block = _mapping_value(provisional.get("acoustic"))
        snr_block = _mapping_value(acoustic_block.get("snr"))
        worst_snr = _mapping_value(snr_block.get("worst_relevant"))
        worst_band = next(
            (
                entry
                for entry in snr_block.get("bands") or ()
                if isinstance(entry, Mapping)
                and entry.get("band_id") == worst_snr.get("band_id")
            ),
            {},
        )
        gating_block = _mapping_value(acoustic_block.get("gating"))
        # peak_dbfs/effective_peak_dbfs: the just-analyzed capture's measured
        # mic peak and this attempt's OWN played level (sweep amplitude +
        # commissioning gain + main volume). Both are always populated
        # regardless of verdict -- peak_dbfs comes straight off the capture
        # quality report (DriverAcousticResult.peak_dbfs), and
        # effective_peak_dbfs is the excitation ledger `raw["excitation"]`
        # already carries from play_driver_capture_sweep's response, echoed
        # back by the phone -- so even an unusable_capture (clipped) rejection
        # carries the evidence the closed-loop level solver's clip-aware
        # correction needs (see
        # jasper.web.correction_crossover_backend.CrossoverLevelLease
        # .record_solve_correction / .record_measured_gain).
        raw_excitation = _mapping_value(raw.get("excitation"))
        admission_result = {
            "accepted": latest_attempt.get("accepted"),
            "reject_reason": latest_attempt.get("reject_reason"),
            "estimated_snr_db": latest_attempt.get("estimated_snr_db"),
            "snr_verdict": snr_block.get("verdict"),
            "worst_band_id": worst_snr.get("band_id"),
            "snr_shortfall_db": worst_band.get("shortfall_db"),
            "clipping": latest_attempt.get("clipping"),
            "above_validity_floor": latest_attempt.get("above_validity_floor"),
            "validity_floor_hz": gating_block.get("f_valid_floor_hz"),
            "peak_dbfs": acoustic_block.get("peak_dbfs"),
            "effective_peak_dbfs": raw_excitation.get("effective_peak_dbfs"),
            # This capture was analyzed from a real recorded WAV, so a tone was
            # emitted regardless of the acoustic verdict. Acoustic rejections
            # still consume the audible measurement budget and the one allowed
            # non-accept slot; only a proven no-audio transport failure is
            # refunded (see repeat_admission.result_emitted_audio).
            "audio_emitted": True,
        }
        strict_isolated_required = bool(
            authoritative_recorder is not None
            and capture_geometry == "reference_axis"
        )
        if strict_isolated_required and latest_attempt.get("accepted") is True:
            try:
                authoritative_evidence = _record_authoritative_driver_capture(
                    recorder=authoritative_recorder,
                    capture_geometry=capture_geometry,
                    accepted=True,
                    admission_handoff=validated_admission,
                    inputs={
                        "topology": topology,
                        "preset": preset,
                        "comparison_set": comparison_set,
                        "calibration_id": calibration_id,
                        "calibration": calibration_curve,
                        "speaker_group_id": group_id,
                        "role": role,
                        "capture_geometry": capture_geometry,
                        "wav_bytes": (
                            wav_bytes
                            if wav_bytes is not None
                            else wav_path.read_bytes()
                        ),
                        "sweep_meta": sweep_meta,
                        "provisional": provisional,
                    },
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                repeat_admission.finish(
                    comparison_set,
                    target_id=repeat_target_id,
                    target_fingerprint=repeat_target_fingerprint,
                    token=reservation_token,
                    result={
                        **admission_result,
                        "accepted": False,
                        "reject_reason": "authoritative_promotion_failed",
                    },
                    status="refused",
                )
                repeat_store.clear_driver_repeats(key)
                repeat_store.record_repeat_failure(
                    repeat_target_id,
                    {
                        "reason": "authoritative_promotion_failed",
                        "attempts": reservation_attempt,
                        "accepted": aggregate["accepted"],
                        "target": DEFAULT_REPEAT_TARGET,
                    },
                )
                raise

        attempt_budget_exhausted = (
            measurement_attempt_count >= repeat_admission.MAX_ATTEMPTS
        )
        if aggregate["needed_recapture"] and not attempt_budget_exhausted:
            repeat_admission.finish(
                comparison_set,
                target_id=repeat_target_id,
                target_fingerprint=repeat_target_fingerprint,
                token=reservation_token,
                result=admission_result,
                status="active",
            )
            persist_repeat_progress("active")
            return {
                "recorded": False,
                "verdict": provisional.get("verdict"),
                "acoustic": provisional.get("acoustic"),
                "repeat_progress": {
                    "attempts": reservation_attempt,
                    "accepted": aggregate["accepted"],
                    "target": DEFAULT_REPEAT_TARGET,
                    "bounded_recapture": (
                        measurement_attempt_count >= DEFAULT_REPEAT_TARGET
                    ),
                    "latest_rejection": (
                        admission_result
                        if latest_attempt.get("accepted") is not True
                        else None
                    ),
                },
                "authoritative_evidence": authoritative_evidence,
            }
        minimum_accepted = DEFAULT_REPEAT_TARGET if strict_isolated_required else 2
        if aggregate["accepted"] < minimum_accepted or not isinstance(
            aggregate.get("aggregate_repeat"), Mapping
        ):
            failure_reason = (
                "strict_isolated_repeats_incomplete"
                if strict_isolated_required
                else "insufficient_accepted_repeats"
            )
            failure = {
                "reason": failure_reason,
                "attempts": reservation_attempt,
                "accepted": aggregate["accepted"],
                "target": DEFAULT_REPEAT_TARGET,
                "per_repeat": aggregate["per_repeat"],
            }
            repeat_store.clear_driver_repeats(key)
            repeat_store.record_repeat_failure(repeat_target_id, failure)
            repeat_admission.finish(
                comparison_set,
                target_id=repeat_target_id,
                target_fingerprint=repeat_target_fingerprint,
                token=reservation_token,
                result=admission_result,
                status="refused",
            )
            persist_repeat_progress(
                "refused", reason=failure_reason
            )
            return {
                "recorded": False,
                "status": "refused",
                "verdict": "insufficient_repeats",
                "repeat_failure": failure,
                "authoritative_evidence": authoritative_evidence,
            }
        result = _finalize_driver_repeat_set(
            topology=topology,
            comparison_set=comparison_set,
            speaker_group_id=group_id,
            role=role,
            topology_target_fingerprint=target_fingerprint,
            repeat_target_id=repeat_target_id,
            repeat_target_fingerprint=repeat_target_fingerprint,
            reservation=reservation,
            admission_result=admission_result,
            repeats=repeats,
            repeat_store=repeat_store,
        )
        result["authoritative_evidence"] = authoritative_evidence
        return result
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
