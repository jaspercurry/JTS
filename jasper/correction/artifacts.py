# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-session artifact and bundle persistence for room correction.

`MeasurementSession` owns the state machine and DSP decisions. This module
owns the file-system evidence bundle that lets a run be replayed or audited
later.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import (
    acoustic_quality,
    bundles,
    confidence,
    deconv,
    replay_artifacts,
    runtime_integrity,
    spatial,
    status,
)
from ..log_event import log_event

logger = logging.getLogger(__name__)

ANALYSIS_NORMALIZE_BAND_HZ = (200.0, 1000.0)


class SessionArtifacts:
    """Write and index the evidence bundle for a measurement session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def ensure_bundle_dir(self) -> Path | None:
        s = self._session
        if not s.save_bundles:
            return None
        try:
            s.bundle_dir.mkdir(parents=True, exist_ok=True)
            (s.bundle_dir / "captures").mkdir(exist_ok=True)
        except OSError as e:
            logger.warning(
                "bundle dir create failed for session %s: %s",
                s.session_id, e,
            )
            return None
        return s.bundle_dir

    def bundle_relative_path(self, path: Path) -> str | None:
        s = self._session
        try:
            return path.resolve().relative_to(s.bundle_dir.resolve()).as_posix()
        except ValueError:
            return None

    def existing_bundle_dependencies(self, *paths: str) -> list[str]:
        s = self._session
        return [
            path
            for path in sorted(set(paths))
            if path and (s.bundle_dir / path).exists()
        ]

    def _capture_artifact_dependencies(self) -> list[str]:
        s = self._session
        dependencies: list[str] = []
        reports = list(s.capture_quality)
        reports.extend(s.noise_reports)
        if s.repeat_quality:
            reports.append(s.repeat_quality)
        if s.verify_quality:
            reports.append(s.verify_quality)
        for report in reports:
            artifact_path = report.get("artifact_path")
            if not isinstance(artifact_path, str):
                continue
            if Path(artifact_path).is_absolute():
                continue
            if (s.bundle_dir / artifact_path).exists():
                dependencies.append(artifact_path)
        return sorted(set(dependencies))

    def _runtime_capture_artifact_dependencies(self) -> list[str]:
        s = self._session
        dependencies: list[str] = []
        for capture in s.runtime_integrity.captures:
            artifact_path = capture.get("artifact_path")
            if not isinstance(artifact_path, str):
                continue
            if Path(artifact_path).is_absolute():
                continue
            if (s.bundle_dir / artifact_path).exists():
                dependencies.append(artifact_path)
        return sorted(set(dependencies))

    def _replay_artifact_dependencies(self) -> list[str]:
        s = self._session
        dependencies: list[str] = []
        reports = list(s.capture_quality)
        if s.repeat_quality:
            reports.append(s.repeat_quality)
        if s.verify_quality:
            reports.append(s.verify_quality)
        for report in reports:
            artifacts = report.get("replay_artifacts")
            if not isinstance(artifacts, dict):
                continue
            for key in ("impulse_response_path", "response_path"):
                artifact_path = artifacts.get(key)
                if not isinstance(artifact_path, str):
                    continue
                if Path(artifact_path).is_absolute():
                    continue
                if (s.bundle_dir / artifact_path).exists():
                    dependencies.append(artifact_path)
        return sorted(set(dependencies))

    def write_capture_replay_artifacts(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
        ir: np.ndarray,
        raw_freqs_hz: np.ndarray,
        raw_magnitude_db: np.ndarray,
        smoothed_magnitude_db: np.ndarray,
        log_freqs_hz: np.ndarray,
        log_magnitude_db: np.ndarray,
        direct_arrival: dict[str, Any],
    ) -> dict[str, Any] | None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None:
            return None
        source_rel = self.bundle_relative_path(captured_wav_path)
        if source_rel is None:
            log_event(
                logger,
                "correction_replay_artifacts_skipped",
                session=s.session_id,
                capture_kind=capture_kind,
                position_index=position_index,
                reason="source_capture_outside_bundle",
            )
            return None
        try:
            artifacts = replay_artifacts.write_capture_replay_artifacts(
                bundle,
                bundle_schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
                session_id=s.session_id,
                capture_kind=capture_kind,
                position_index=position_index,
                source_capture_path=source_rel,
                ir=ir,
                sample_rate=s.cfg.sample_rate,
                raw_freqs_hz=raw_freqs_hz,
                raw_magnitude_db=raw_magnitude_db,
                smoothed_magnitude_db=smoothed_magnitude_db,
                log_freqs_hz=log_freqs_hz,
                log_magnitude_db=log_magnitude_db,
                direct_arrival=direct_arrival,
                deconvolution={
                    "method": "fft_tikhonov_regularized_inverse",
                    "pre_arrival_ms": deconv.DEFAULT_PRE_ARRIVAL_MS,
                    "post_arrival_ms": deconv.DEFAULT_POST_ARRIVAL_MS,
                    "epsilon_relative": deconv.DEFAULT_EPSILON_RELATIVE,
                },
                calibration_applied=s.mic_calibration is not None,
                normalized_band_hz=ANALYSIS_NORMALIZE_BAND_HZ,
            )
            raw_dependencies = self.existing_bundle_dependencies(
                "info.json",
                source_rel,
            )
            calibration_dependencies = (
                self.existing_bundle_dependencies("mic_calibration.json")
                if s.mic_calibration is not None
                else []
            )
            common_metadata = {
                "capture_kind": capture_kind,
                "position_index": position_index,
            }
            bundles.record_artifact(
                bundle,
                artifacts.impulse_response_path,
                kind="derived_impulse_response",
                sensitivity="private_metadata",
                recomputable=True,
                generated_by=(
                    "jasper.correction.artifacts."
                    "SessionArtifacts.write_capture_replay_artifacts"
                ),
                dependencies=raw_dependencies,
                metadata=common_metadata,
                schema_version=replay_artifacts.SCHEMA_VERSION,
            )
            bundles.record_artifact(
                bundle,
                artifacts.response_path,
                kind="derived_frequency_response",
                sensitivity="private_metadata",
                recomputable=True,
                generated_by=(
                    "jasper.correction.artifacts."
                    "SessionArtifacts.write_capture_replay_artifacts"
                ),
                dependencies=self.existing_bundle_dependencies(
                    *raw_dependencies,
                    *calibration_dependencies,
                    artifacts.impulse_response_path,
                ),
                metadata=common_metadata,
                schema_version=replay_artifacts.SCHEMA_VERSION,
            )
            log_event(
                logger,
                "correction_replay_artifacts_written",
                session=s.session_id,
                capture_kind=capture_kind,
                position_index=position_index,
                ir=artifacts.impulse_response_path,
                response=artifacts.response_path,
            )
            return artifacts.to_dict()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle replay artifact write failed session=%s "
                "capture_kind=%s position_index=%s",
                s.session_id,
                capture_kind,
                position_index,
            )
            return None

    def record_raw_capture_artifact(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None = None,
    ) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None:
            return
        rel_path = self.bundle_relative_path(captured_wav_path)
        if rel_path is None:
            return
        metadata: dict[str, Any] = {"capture_kind": capture_kind}
        if position_index is not None:
            metadata["position_index"] = position_index
        artifact_kind = {
            "noise": "noise_capture",
            "repeat": "repeat_capture",
        }.get(capture_kind, "raw_capture")
        try:
            bundles.record_artifact(
                bundle,
                rel_path,
                kind=artifact_kind,
                sensitivity="private_raw_audio",
                recomputable=False,
                generated_by=(
                    "jasper.correction.artifacts."
                    "SessionArtifacts.record_raw_capture_artifact"
                ),
                dependencies=self.existing_bundle_dependencies("info.json"),
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle raw capture manifest record failed session=%s path=%s",
                s.session_id,
                rel_path,
            )

    def write_acoustic_quality_json(self) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None or s.acoustic_quality is None:
            return
        bundles.write_json_artifact(
            bundle,
            "acoustic_quality.json",
            s.acoustic_quality,
            kind="acoustic_quality",
            sensitivity="private_metadata",
            recomputable=True,
            generated_by=(
                "jasper.correction.artifacts."
                "SessionArtifacts.write_acoustic_quality_json"
            ),
            dependencies=self.existing_bundle_dependencies(
                "info.json",
                *self._capture_artifact_dependencies(),
            ),
            schema_version=acoustic_quality.SCHEMA_VERSION,
        )

    def write_runtime_integrity_json(
        self,
        *,
        extra_dependencies: tuple[str, ...] = (),
    ) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None:
            return
        dependencies = self.existing_bundle_dependencies(
            "info.json",
            *self._capture_artifact_dependencies(),
            *self._runtime_capture_artifact_dependencies(),
            *extra_dependencies,
        )
        bundles.write_json_artifact(
            bundle,
            "runtime_integrity.json",
            {
                "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
                **s.runtime_integrity.to_dict(),
            },
            kind="runtime_integrity",
            sensitivity="private_metadata",
            recomputable=False,
            generated_by=(
                "jasper.correction.artifacts."
                "SessionArtifacts.write_runtime_integrity_json"
            ),
            dependencies=dependencies,
            schema_version=runtime_integrity.SCHEMA_VERSION,
        )

    def capture_path_for_position(self, idx: int) -> Path:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is not None:
            return bundle / "captures" / f"p{idx}.wav"
        s.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return s.cfg.capture_dir / (
            f"capture_{s.session_id}_p{idx}_{int(time.time())}.wav"
        )

    def noise_capture_path_for_position(self, idx: int) -> Path:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is not None:
            path = bundle / "noise" / f"p{idx}_pre.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        s.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return s.cfg.capture_dir / (
            f"noise_{s.session_id}_p{idx}_{int(time.time())}.wav"
        )

    def repeat_capture_path_for_position(
        self,
        idx: int = 0,
        *,
        repeat_index: int = 1,
    ) -> Path:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is not None:
            path = bundle / "repeat_captures" / f"p{idx}_r{repeat_index}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        s.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return s.cfg.capture_dir / (
            f"repeat_{s.session_id}_p{idx}_r{repeat_index}_{int(time.time())}.wav"
        )

    def verify_capture_path(self) -> Path:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is not None:
            return bundle / "verify.wav"
        s.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        return s.cfg.capture_dir / (
            f"verify_{s.session_id}_{int(time.time())}.wav"
        )

    def write_info_json(self) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None:
            return
        info = status.info_json_payload(s)
        bundles.write_json_artifact(
            bundle,
            "info.json",
            info,
            kind="session_metadata",
            sensitivity="private_metadata",
            recomputable=False,
            generated_by="jasper.correction.artifacts.SessionArtifacts.write_info_json",
            schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        )
        self.write_mic_calibration_bundle(bundle)

    def write_result_json(self) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None:
            return
        result = status.result_json_payload(s)
        bundles.write_json_artifact(
            bundle,
            "result.json",
            result,
            kind="analysis_result",
            sensitivity="private_metadata",
            recomputable=True,
            generated_by="jasper.correction.artifacts.SessionArtifacts.write_result_json",
            dependencies=self.existing_bundle_dependencies(
                "info.json",
                "position_analysis.json",
                "runtime_integrity.json",
                "acoustic_quality.json",
                "mic_calibration.json",
                *self._replay_artifact_dependencies(),
                *self._capture_artifact_dependencies(),
            ),
            schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        )

    def write_mic_calibration_bundle(self, bundle: Path) -> None:
        s = self._session
        if s.mic_calibration is None:
            return
        record = s.mic_calibration
        payload = {
            **record.public_metadata(),
            "raw_filename": "mic_calibration.txt",
            "curve": record.curve.to_dict(),
        }
        dependencies: list[str] = []
        raw_path = Path(record.raw_path)
        if raw_path.exists():
            try:
                bundle_raw = bundle / "mic_calibration.txt"
                shutil.copy2(raw_path, bundle_raw)
                bundle_raw.chmod(0o600)
                bundles.record_artifact(
                    bundle,
                    "mic_calibration.txt",
                    kind="mic_calibration_raw",
                    sensitivity="private_metadata",
                    recomputable=False,
                    generated_by=(
                        "jasper.correction.artifacts."
                        "SessionArtifacts.write_mic_calibration_bundle"
                    ),
                    dependencies=self.existing_bundle_dependencies("info.json"),
                )
                dependencies.append("mic_calibration.txt")
            except OSError as e:
                logger.warning(
                    "mic_calibration.txt copy failed for session %s: %s",
                    s.session_id, e,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mic_calibration.txt manifest record failed session=%s",
                    s.session_id,
                )
        bundles.write_json_artifact(
            bundle,
            "mic_calibration.json",
            payload,
            kind="mic_calibration_metadata",
            sensitivity="private_metadata",
            recomputable=True,
            generated_by=(
                "jasper.correction.artifacts."
                "SessionArtifacts.write_mic_calibration_bundle"
            ),
            dependencies=self.existing_bundle_dependencies(
                "info.json",
                *dependencies,
            ),
            schema_version=1,
            file_mode=0o600,
        )

    def write_position_analysis_json(self) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if (
            bundle is None
            or s.position_freqs is None
            or not s.position_magnitudes
            or s.measured_curve is None
        ):
            s.position_analysis = None
            return

        freqs = np.asarray(s.position_freqs, dtype=float)
        spatial_matrix, spatial_error = spatial.build_spatial_matrix(
            s.position_magnitudes,
            freqs,
        )
        if spatial_matrix is None:
            logger.warning(
                "position_analysis unavailable for session %s: %s",
                s.session_id, spatial_error,
            )
            s.position_analysis = None
            return
        std_db = spatial_matrix.std_db
        range_db = spatial_matrix.range_db

        def round_list(values: np.ndarray) -> list[float]:
            return [round(float(v), 3) for v in values]

        variance_summary = (
            (s.confidence_report or {})
            .get("position_variance")
        )
        target_db = (
            np.asarray(s.target_curve.magnitude_db, dtype=float)
            if s.target_curve is not None
            else None
        )
        position_report = confidence.build_position_report(
            position_magnitudes=s.position_magnitudes,
            freqs_hz=freqs,
            measured_db=np.asarray(s.measured_curve.magnitude_db, dtype=float),
            target_db=target_db,
            correction_band_hz=(s.cfg.peq_f_low, s.cfg.peq_f_high),
        )
        payload = {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "artifact_schema_version": 1,
            "session_id": s.session_id,
            "correction_band_hz": [s.cfg.peq_f_low, s.cfg.peq_f_high],
            "freqs_hz": round_list(freqs),
            "positions": [
                {
                    "position_index": idx,
                    "magnitude_db": round_list(np.asarray(mag, dtype=float)),
                }
                for idx, mag in enumerate(s.position_magnitudes)
            ],
            "spatial_average_db": [
                round(float(v), 3) for v in s.measured_curve.magnitude_db
            ],
            "variance": {
                "std_db": round_list(std_db),
                "range_db": round_list(range_db),
                "summary": variance_summary,
            },
            "bands": position_report["bands"],
            "feature_flags": position_report["feature_flags"],
        }
        bundles.write_json_artifact(
            bundle,
            "position_analysis.json",
            payload,
            kind="position_analysis",
            sensitivity="private_metadata",
            recomputable=True,
            generated_by=(
                "jasper.correction.artifacts."
                "SessionArtifacts.write_position_analysis_json"
            ),
            dependencies=self.existing_bundle_dependencies(
                "info.json",
                *self._capture_artifact_dependencies(),
            ),
            schema_version=1,
        )

        s.position_analysis = {
            "artifact_path": "position_analysis.json",
            "artifact_schema_version": 1,
            "position_count": len(s.position_magnitudes),
            "freq_count": int(freqs.shape[0]),
            "variance": variance_summary,
            "chart": {
                "freqs_hz": round_list(freqs),
                "min_db": round_list(np.min(spatial_matrix.magnitudes_db, axis=0)),
                "max_db": round_list(np.max(spatial_matrix.magnitudes_db, axis=0)),
                "std_db": round_list(std_db),
                "range_db": round_list(range_db),
            },
            "bands": position_report["bands"],
            "feature_flags": position_report["feature_flags"],
        }
        if s.design_report is not None:
            s.design_report["position_report"] = {
                "artifact_path": "position_analysis.json",
                "artifact_schema_version": 1,
                "position_count": len(s.position_magnitudes),
                "bands": position_report["bands"],
                "feature_flags": position_report["feature_flags"],
            }

    def copy_applied_yaml(self) -> None:
        s = self._session
        bundle = self.ensure_bundle_dir()
        if bundle is None or s.config_path is None:
            return
        try:
            shutil.copy2(s.config_path, bundle / "applied.yml")
            bundles.record_artifact(
                bundle,
                "applied.yml",
                kind="camilladsp_config",
                sensitivity="debug_safe",
                recomputable=True,
                generated_by="jasper.correction.artifacts.SessionArtifacts.copy_applied_yaml",
                dependencies=self.existing_bundle_dependencies(
                    "info.json",
                    "result.json",
                ),
            )
        except OSError as e:
            logger.warning(
                "applied.yml copy failed for session %s: %s",
                s.session_id, e,
            )
