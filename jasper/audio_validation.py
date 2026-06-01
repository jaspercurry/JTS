"""Persistent audio validation artifacts and readiness snapshots.

Validation artifacts are small JSON files that record whether a
mic/DAC/profile combination has been validated. This module owns the
schema, parsing, freshness checks, and atomic writes. Bounded producers
may read already-exposed runtime status, but they do not play audio,
open capture loops, persist chip settings, or mutate audio services.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .audio_profile_state import (
    AecIntent,
    MicProbe,
    build_audio_profile_status,
    parse_env_bool,
    runtime_env_from_mapping,
)
from .env_load import parse_env_file


CURRENT_SCHEMA_VERSION = 1
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
DEFAULT_ARTIFACT_DIR = Path("/var/lib/jasper/audio-validation")
LATEST_POINTER_NAME = "latest.json"
DEFAULT_STALE_AFTER = timedelta(days=30)
DEFAULT_FUTURE_SKEW = timedelta(minutes=5)
ALLOWED_STATUSES = frozenset({"pass", "warn", "fail", "unknown"})
CHIP_AEC_PROFILE = "xvf_chip_aec"
READINESS_SNAPSHOT_KIND = "readiness_snapshot"
DEFAULT_AEC_MODE_PATH = Path("/var/lib/jasper/aec_mode.env")
DEFAULT_SYSTEM_ENV_PATH = Path("/etc/jasper/jasper.env")
DEFAULT_BUILD_MANIFEST_PATH = Path("/var/lib/jasper/build.txt")
DEFAULT_BRIDGE_STATS_PATH = Path("/run/jasper/aec_bridge_stats.json")
DEFAULT_OUTPUTD_STATUS_SOCKET = Path("/run/jasper-outputd/control.sock")
EXPECTED_CHIP_WAKE_LEGS = ("on", "chip_aec_150", "chip_aec_210")

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

logger = logging.getLogger("jasper.audio_validation")


class ValidationArtifactError(ValueError):
    """A validation artifact is missing required fields or malformed."""

    def __init__(self, issues: list[str] | tuple[str, ...]):
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True)
class ValidationArtifact:
    """Schema-v1 audio validation artifact."""

    validated_at: datetime
    mic_id: str
    dac_id: str
    profile: str
    status: str
    checks: Mapping[str, JsonValue]
    recommendation: str
    notes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        issues = _validate_artifact_fields(self)
        if issues:
            raise ValidationArtifactError(issues)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "validated_at": _format_timestamp(self.validated_at),
            "hardware": {
                "mic_id": self.mic_id,
                "dac_id": self.dac_id,
            },
            "profile": self.profile,
            "status": self.status,
            "checks": dict(self.checks),
            "recommendation": self.recommendation,
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload


@dataclass(frozen=True)
class ArtifactLoadResult:
    """Non-throwing read result for callers that surface status to users."""

    state: str
    artifact: ValidationArtifact | None = None
    path: Path | None = None
    errors: tuple[str, ...] = ()
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "loaded" and self.artifact is not None

    @property
    def has_artifact(self) -> bool:
        return self.artifact is not None


def make_artifact(
    *,
    mic_id: str,
    dac_id: str,
    profile: str,
    status: str,
    checks: Mapping[str, JsonValue],
    recommendation: str,
    validated_at: datetime | None = None,
    notes: list[str] | tuple[str, ...] = (),
    errors: list[str] | tuple[str, ...] = (),
) -> ValidationArtifact:
    """Construct a schema-v1 artifact, defaulting the timestamp to UTC now."""

    return ValidationArtifact(
        schema_version=CURRENT_SCHEMA_VERSION,
        validated_at=datetime.now(timezone.utc) if validated_at is None else validated_at,
        mic_id=mic_id,
        dac_id=dac_id,
        profile=profile,
        status=status,
        checks=checks,
        recommendation=recommendation,
        notes=tuple(notes),
        errors=tuple(errors),
    )


def parse_artifact_payload(payload: Any) -> ValidationArtifact:
    """Parse and validate a schema-v1 artifact from decoded JSON."""

    issues: list[str] = []
    if not isinstance(payload, dict):
        raise ValidationArtifactError(["artifact must be a JSON object"])

    schema_version = payload.get("schema_version")
    if schema_version != CURRENT_SCHEMA_VERSION:
        issues.append(
            f"schema_version must be {CURRENT_SCHEMA_VERSION}, got {schema_version!r}"
        )

    validated_at = _parse_timestamp_field(payload.get("validated_at"), issues)
    hardware = payload.get("hardware")
    if not isinstance(hardware, dict):
        issues.append("hardware must be an object")
        hardware = {}

    mic_id = _required_string(hardware.get("mic_id"), "hardware.mic_id", issues)
    dac_id = _required_string(hardware.get("dac_id"), "hardware.dac_id", issues)
    profile = _required_string(payload.get("profile"), "profile", issues)
    status = _required_string(payload.get("status"), "status", issues)
    if status and status not in ALLOWED_STATUSES:
        issues.append(
            f"status must be one of {sorted(ALLOWED_STATUSES)}, got {status!r}"
        )

    checks = payload.get("checks")
    if not isinstance(checks, dict):
        issues.append("checks must be an object")
        checks = {}
    else:
        check_issues = _validate_checks(checks)
        issues.extend(f"checks.{issue}" for issue in check_issues)

    recommendation = _required_string(
        payload.get("recommendation"),
        "recommendation",
        issues,
    )
    notes = _string_tuple(payload.get("notes"), "notes", issues)
    errors = _string_tuple(payload.get("errors"), "errors", issues)

    if issues:
        raise ValidationArtifactError(issues)

    return ValidationArtifact(
        schema_version=schema_version,
        validated_at=validated_at,
        mic_id=mic_id,
        dac_id=dac_id,
        profile=profile,
        status=status,
        checks=checks,
        recommendation=recommendation,
        notes=notes,
        errors=errors,
    )


def write_artifact(
    artifact: ValidationArtifact,
    *,
    directory: Path | str = DEFAULT_ARTIFACT_DIR,
    file_mode: int = 0o644,
) -> Path:
    """Write one timestamped JSON artifact atomically and return its path."""

    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(directory_path, artifact)
    body = (
        json.dumps(artifact.to_dict(), allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(directory_path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp_name, file_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def write_latest_pointer(
    artifact: ValidationArtifact,
    *,
    directory: Path | str = DEFAULT_ARTIFACT_DIR,
    file_mode: int = 0o644,
) -> Path:
    """Atomically update the convenience latest pointer for status surfaces."""

    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True)
    path = directory_path / LATEST_POINTER_NAME
    body = (
        json.dumps(artifact.to_dict(), allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(directory_path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp_name, file_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def load_artifact(
    path: Path | str,
    *,
    now: datetime | None = None,
    max_age: timedelta | None = DEFAULT_STALE_AFTER,
    future_skew: timedelta = DEFAULT_FUTURE_SKEW,
) -> ArtifactLoadResult:
    """Load one artifact without raising for missing/malformed/stale files."""

    artifact_path = Path(path)
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ArtifactLoadResult(
            state="missing",
            path=artifact_path,
            errors=("artifact file does not exist",),
        )
    except OSError as e:
        return ArtifactLoadResult(
            state="malformed",
            path=artifact_path,
            errors=(f"could not read artifact: {e}",),
        )
    except json.JSONDecodeError as e:
        return ArtifactLoadResult(
            state="malformed",
            path=artifact_path,
            errors=(f"invalid JSON: {e.msg}",),
        )

    try:
        artifact = parse_artifact_payload(payload)
    except ValidationArtifactError as e:
        return ArtifactLoadResult(
            state="malformed",
            path=artifact_path,
            errors=e.issues,
        )

    future = is_artifact_from_future(
        artifact,
        now=now,
        tolerance=future_skew,
    )
    if future:
        return ArtifactLoadResult(
            state="future",
            artifact=artifact,
            path=artifact_path,
            errors=("artifact timestamp is in the future",),
        )

    stale = is_artifact_stale(artifact, now=now, max_age=max_age)
    return ArtifactLoadResult(
        state="stale" if stale else "loaded",
        artifact=artifact,
        path=artifact_path,
        stale=stale,
    )


def load_latest_artifact(
    directory: Path | str = DEFAULT_ARTIFACT_DIR,
    *,
    mic_id: str | None = None,
    dac_id: str | None = None,
    profile: str | None = None,
    now: datetime | None = None,
    max_age: timedelta | None = DEFAULT_STALE_AFTER,
    future_skew: timedelta = DEFAULT_FUTURE_SKEW,
) -> ArtifactLoadResult:
    """Find the newest valid artifact, optionally filtered by identity."""

    directory_path = Path(directory)
    try:
        paths = sorted(
            p for p in directory_path.glob("*.json")
            if p.is_file() and p.name != LATEST_POINTER_NAME
        )
    except OSError as e:
        return ArtifactLoadResult(
            state="missing",
            path=directory_path,
            errors=(f"could not list artifact directory: {e}",),
        )

    if not paths:
        return ArtifactLoadResult(
            state="missing",
            path=directory_path,
            errors=("no validation artifacts found",),
        )

    malformed_errors: list[str] = []
    matches: list[tuple[datetime, Path, ValidationArtifact]] = []
    for path in paths:
        result = load_artifact(
            path,
            now=now,
            max_age=None,
            future_skew=future_skew,
        )
        if result.artifact is None:
            if result.errors:
                malformed_errors.append(f"{path.name}: {'; '.join(result.errors)}")
            continue
        artifact = result.artifact
        if mic_id is not None and artifact.mic_id != mic_id:
            continue
        if dac_id is not None and artifact.dac_id != dac_id:
            continue
        if profile is not None and artifact.profile != profile:
            continue
        matches.append((artifact.validated_at, path, artifact))

    if not matches:
        state = "malformed" if malformed_errors and len(malformed_errors) == len(paths) else "missing"
        return ArtifactLoadResult(
            state=state,
            path=directory_path,
            errors=tuple(malformed_errors) or ("no matching validation artifacts found",),
        )

    _validated_at, path, artifact = max(matches, key=lambda item: (item[0], item[1].name))
    future = is_artifact_from_future(
        artifact,
        now=now,
        tolerance=future_skew,
    )
    if future:
        return ArtifactLoadResult(
            state="future",
            artifact=artifact,
            path=path,
            errors=tuple(malformed_errors) + ("artifact timestamp is in the future",),
        )

    stale = is_artifact_stale(artifact, now=now, max_age=max_age)
    return ArtifactLoadResult(
        state="stale" if stale else "loaded",
        artifact=artifact,
        path=path,
        errors=tuple(malformed_errors),
        stale=stale,
    )


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def artifact_directory() -> Path:
    return _env_path("JASPER_AUDIO_VALIDATION_DIR", DEFAULT_ARTIFACT_DIR)


def _read_mode_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(str(path or _env_path("JASPER_AEC_MODE_FILE", DEFAULT_AEC_MODE_PATH)))


def _read_system_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(str(path or _env_path("JASPER_ENV_FILE", DEFAULT_SYSTEM_ENV_PATH)))


def _intent_from_env(env: Mapping[str, str]) -> AecIntent:
    mode = (env.get("JASPER_AEC_MODE") or "auto").strip().strip("'\"").lower()
    if mode in ("", "on", "true", "1"):
        mode = "auto"
    elif mode in ("off", "false", "0", "disabled", "disable", "no"):
        mode = "disabled"
    return AecIntent(
        mode=mode,
        raw_enabled=parse_env_bool(env.get("JASPER_WAKE_LEG_RAW", "1"), True),
        dtln_enabled=parse_env_bool(env.get("JASPER_WAKE_LEG_DTLN", "0"), False),
        chip_aec_enabled=parse_env_bool(
            env.get("JASPER_WAKE_LEG_CHIP_AEC", "0"), False,
        ),
    )


def _probe_xvf_mic() -> MicProbe:
    try:
        from .mics import xvf3800

        return MicProbe(
            xvf_present=xvf3800.is_present(),
            capture_channels=xvf3800.capture_channels(),
            recommended_channels=xvf3800.RECOMMENDED_FIRMWARE.capture_channels,
            display_name=xvf3800.DISPLAY_NAME,
        )
    except Exception as e:  # noqa: BLE001 - readiness must fail soft
        return MicProbe(
            xvf_present=False,
            capture_channels=None,
            probe_error=f"firmware probe failed: {e}",
        )


def _mic_details(mic: MicProbe) -> dict[str, JsonValue]:
    return {
        "id": "xvf3800" if mic.xvf_present else "unknown",
        "family": "xvf3800",
        "display_name": mic.display_name,
        "present": mic.xvf_present,
        "capture_channels": mic.capture_channels,
        "recommended_channels": mic.recommended_channels,
        "probe_error": mic.probe_error,
    }


def _outputd_socket_path(system_env: Mapping[str, str]) -> Path:
    raw = (
        system_env.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or os.environ.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or str(DEFAULT_OUTPUTD_STATUS_SOCKET)
    )
    return Path(raw)


def _query_outputd_status(socket_path: Path, timeout: float = 1.0) -> dict[str, Any] | None:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as e:
        logger.debug("event=audio_validation.outputd_status_unavailable error=%s", e)
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    try:
        data = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        logger.debug("event=audio_validation.outputd_status_invalid error=%s", e)
        return None
    return data if isinstance(data, dict) else None


def _service_state(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("event=audio_validation.service_probe_failed unit=%s error=%s", unit, e)
        return "unknown"
    return result.stdout.strip() or "unknown"


def _read_bridge_stats(path: Path | None = None) -> dict[str, Any] | None:
    stats_path = path or _env_path("JASPER_AEC_BRIDGE_STATS_PATH", DEFAULT_BRIDGE_STATS_PATH)
    try:
        data = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("event=audio_validation.bridge_stats_unavailable error=%s", e)
        return None
    return data if isinstance(data, dict) else None


def _read_voice_wake_legs(timeout: float = 1.0) -> set[str] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8780/state", timeout=timeout) as r:
            data = json.loads(r.read())
    except (
        urllib.error.URLError,
        OSError,
        ValueError,
        TimeoutError,
        json.JSONDecodeError,
    ) as e:
        logger.debug("event=audio_validation.voice_state_unavailable error=%s", e)
        return None
    voice = data.get("voice") if isinstance(data, dict) else None
    if not isinstance(voice, dict):
        return None
    legs = voice.get("wake_legs")
    if not isinstance(legs, list):
        return None
    return {str(leg) for leg in legs}


def _recent_bridge_journal(timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                "jasper-aec-bridge.service",
                "--since",
                "-2min",
                "-o",
                "cat",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("event=audio_validation.bridge_journal_unavailable error=%s", e)
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    return result.stdout


def _check(
    status: str,
    *,
    summary: str,
    required: bool = True,
    observed: JsonValue = None,
    expected: JsonValue = None,
) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {
        "status": status,
        "required": required,
        "summary": summary,
    }
    if observed is not None:
        out["observed"] = observed
    if expected is not None:
        out["expected"] = expected
    return out


def _rollup_status(checks: Mapping[str, Mapping[str, Any]]) -> str:
    required = [check for check in checks.values() if check.get("required", True)]
    if any(check.get("status") == "fail" for check in required):
        return "fail"
    if any(check.get("status") != "pass" for check in required):
        return "warn"
    return "pass"


def _readiness_recommendation(status: str, checks: Mapping[str, Mapping[str, Any]]) -> str:
    failed = [name for name, check in checks.items() if check.get("status") == "fail"]
    if "mic_detected" in failed:
        return "use_software_aec3_until_xvf_6ch_available"
    if "runtime_profile" in failed or "runtime_env" in failed:
        return "run_reconciler_or_select_chip_aec_before_validating"
    if "dac_reference" in failed:
        return "fix_outputd_chip_reference_before_chip_aec"
    if "service_state" in failed:
        return "fix_audio_services_before_chip_aec"
    runtime_unknown = [
        name
        for name, check in checks.items()
        if name != "measured_drift_delay"
        and check.get("required", True)
        and check.get("status") in {"unknown", "not_run"}
    ]
    if runtime_unknown:
        return "fix_runtime_observability_before_hardware_validation"
    measured = checks.get("measured_drift_delay", {})
    if measured.get("status") in {"not_run", "unknown"}:
        return "run_hardware_validation"
    if status == "pass":
        return "chip_aec_validated"
    return "review_audio_validation_warnings"


def _dac_details(
    system_env: Mapping[str, str],
    outputd_status: Mapping[str, Any] | None,
) -> dict[str, JsonValue]:
    outputd_dac = outputd_status.get("dac") if isinstance(outputd_status, dict) else None
    dac_pcm = ""
    sample_rate: JsonValue = None
    if isinstance(outputd_dac, dict):
        dac_pcm = str(outputd_dac.get("pcm") or "")
        raw_sample_rate = outputd_dac.get("sample_rate")
        if isinstance(raw_sample_rate, (str, int, float, bool)) or raw_sample_rate is None:
            sample_rate = raw_sample_rate
    if not dac_pcm:
        dac_pcm = (
            system_env.get("JASPER_OUTPUTD_DAC_PCM")
            or os.environ.get("JASPER_OUTPUTD_DAC_PCM")
            or "outputd_dac"
        )
    dac_id = (
        system_env.get("JASPER_AUDIO_DAC_ID")
        or os.environ.get("JASPER_AUDIO_DAC_ID")
        or dac_pcm
    )
    return {
        "id": dac_id,
        "pcm": dac_pcm,
        "backend": str(
            (outputd_status or {}).get("backend")
            or system_env.get("JASPER_OUTPUTD_BACKEND")
            or os.environ.get("JASPER_OUTPUTD_BACKEND")
            or "unknown"
        ),
        "sample_rate": sample_rate,
    }


def _runtime_identity_check(system_env: Mapping[str, str]) -> dict[str, JsonValue]:
    build_env = parse_env_file(
        str(_env_path("JASPER_BUILD_MANIFEST", DEFAULT_BUILD_MANIFEST_PATH)),
    )
    observed = {
        "system_hostname": socket.gethostname(),
        "jasper_hostname": (
            system_env.get("JASPER_HOSTNAME")
            or os.environ.get("JASPER_HOSTNAME")
            or ""
        ),
        "build_sha": build_env.get("JASPER_GIT_SHA", ""),
        "build_branch": build_env.get("JASPER_GIT_BRANCH", ""),
        "installed_at": build_env.get("JASPER_INSTALL_AT", ""),
    }
    return _check(
        "pass",
        required=False,
        summary="Pi/runtime identity captured for artifact attribution.",
        observed=observed,
    )


def _runtime_profile_check(profile_status: Mapping[str, Any], profile: str) -> dict[str, JsonValue]:
    audio_profile = profile_status.get("audio_profile") or {}
    observed = {
        "requested": audio_profile.get("requested"),
        "active": audio_profile.get("active"),
        "state": audio_profile.get("state"),
        "reason": audio_profile.get("reason"),
    }
    if audio_profile.get("requested") == profile and audio_profile.get("active") == profile:
        return _check("pass", summary="Requested chip-AEC profile is active.", observed=observed)
    return _check(
        "fail",
        summary="Chip-AEC profile is not the active runtime profile.",
        observed=observed,
        expected={"requested": profile, "active": profile},
    )


def _runtime_env_check(runtime: Any) -> dict[str, JsonValue]:
    observed = {
        "chip_enabled": getattr(runtime, "chip_enabled", False),
        "chip_aec_150_device": getattr(runtime, "chip_aec_150_device", ""),
        "chip_aec_210_device": getattr(runtime, "chip_aec_210_device", ""),
        "chip_primary_leg": getattr(runtime, "chip_primary_leg", ""),
    }
    if (
        observed["chip_enabled"]
        and observed["chip_aec_150_device"]
        and observed["chip_aec_210_device"]
    ):
        return _check("pass", summary="Reconciler-applied chip-AEC env is present.", observed=observed)
    return _check(
        "fail",
        summary="Reconciler-applied chip-AEC env is incomplete.",
        observed=observed,
    )


def _service_state_check(service_states: Mapping[str, str]) -> dict[str, JsonValue]:
    required_units = (
        "jasper-outputd.service",
        "jasper-aec-bridge.service",
        "jasper-aec-init.service",
        "jasper-voice.service",
    )
    missing = {
        unit: service_states.get(unit, "unknown")
        for unit in required_units
        if service_states.get(unit) != "active"
    }
    if not missing:
        return _check("pass", summary="Required chip-AEC services are active.", observed=dict(service_states))
    return _check(
        "fail",
        summary="One or more required chip-AEC services are not active.",
        observed=dict(service_states),
        expected={unit: "active" for unit in required_units},
    )


def _dac_reference_check(outputd_status: Mapping[str, Any] | None) -> dict[str, JsonValue]:
    if not isinstance(outputd_status, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS was unavailable; chip-reference state could not be read.",
        )
    refs = outputd_status.get("reference_outputs")
    if not isinstance(refs, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS does not expose reference_outputs.",
        )
    observed = {
        "chip_ref_pcm": refs.get("chip_ref_pcm"),
        "chip_ref_sample_rate": refs.get("chip_ref_sample_rate"),
        "chip_ref_period_frames": refs.get("chip_ref_period_frames"),
        "chip_ref_buffer_frames": refs.get("chip_ref_buffer_frames"),
        "udp_target": refs.get("udp_target"),
    }
    if observed["chip_ref_pcm"] and observed["udp_target"] and observed["chip_ref_sample_rate"] == 16000:
        return _check("pass", summary="outputd exposes chip PCM and UDP reference outputs.", observed=observed)
    return _check(
        "fail",
        summary="outputd chip-reference outputs are not fully configured.",
        observed=observed,
        expected={"chip_ref_pcm": "non-empty", "udp_target": "non-empty", "chip_ref_sample_rate": 16000},
    )


def _wake_legs_check(voice_wake_legs: set[str] | None) -> dict[str, JsonValue]:
    expected = set(EXPECTED_CHIP_WAKE_LEGS)
    if voice_wake_legs is None:
        return _check(
            "unknown",
            summary="jasper-voice wake-leg runtime state was unavailable.",
            expected=sorted(expected),
        )
    missing = expected - voice_wake_legs
    if not missing:
        return _check(
            "pass",
            summary="jasper-voice has armed the chip-AEC wake legs.",
            observed=sorted(voice_wake_legs),
            expected=sorted(expected),
        )
    return _check(
        "fail",
        summary="jasper-voice has not armed every chip-AEC wake leg.",
        observed=sorted(voice_wake_legs),
        expected=sorted(expected),
    )


def _bridge_stats_check(stats: Mapping[str, Any] | None, now: datetime) -> dict[str, JsonValue]:
    if not isinstance(stats, Mapping):
        return _check("unknown", summary="AEC bridge stats snapshot is unavailable.")
    counters = stats.get("counters")
    if not isinstance(counters, Mapping):
        return _check("unknown", summary="AEC bridge stats snapshot has no counters.")
    updated = stats.get("updated_epoch_sec")
    age_sec = None
    if isinstance(updated, (int, float)):
        age_sec = max(0.0, now.timestamp() - float(updated))
    queue_drops = counters.get("queue_drops")
    udp_drops = counters.get("udp_send_drops_by_leg")
    ref_starved = int(counters.get("ref_starved_frames", 0) or 0)
    observed = {
        "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        "frames_processed": counters.get("frames_processed"),
        "ref_starved_frames": ref_starved,
        "queue_drops": queue_drops if isinstance(queue_drops, dict) else None,
        "udp_send_drops_by_leg": udp_drops if isinstance(udp_drops, dict) else None,
        "packets_sent_by_leg": counters.get("packets_sent_by_leg"),
    }
    if age_sec is not None and age_sec > 10:
        return _check("warn", summary="AEC bridge stats are stale.", observed=observed)
    drop_total = ref_starved
    for group in (queue_drops, udp_drops):
        if isinstance(group, Mapping):
            drop_total += sum(int(v or 0) for v in group.values())
    if drop_total:
        return _check(
            "warn",
            summary="AEC bridge counters show drops or reference starvation since process start.",
            observed=observed,
        )
    return _check("pass", summary="AEC bridge counters are clean.", observed=observed)


def _measured_drift_delay_check(journal_text: str | None) -> dict[str, JsonValue]:
    drift_warnings = None
    if journal_text is not None:
        drift_warnings = len(
            re.findall(r"stale ref frames.*drift|drift.*stale ref frames", journal_text),
        )
    return _check(
        "not_run",
        summary=(
            "Drift and fixed delay were not measured. This readiness snapshot "
            "does not play calibration audio or open capture streams."
        ),
        observed=(
            {"recent_bridge_drift_warnings": drift_warnings}
            if drift_warnings is not None else None
        ),
        expected={"hardware_validation": "operator-controlled playback/capture run"},
    )


def build_chip_aec_readiness_artifact(
    *,
    now: datetime | None = None,
    profile: str = CHIP_AEC_PROFILE,
    system_env: Mapping[str, str] | None = None,
    mode_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    bridge_stats: Mapping[str, Any] | None = None,
    voice_wake_legs: set[str] | None = None,
    bridge_journal_text: str | None = None,
) -> ValidationArtifact:
    """Build a bounded schema-v1 chip-AEC readiness snapshot.

    This producer reads runtime state that is already exposed by JTS. It does
    not play audio, capture audio, mutate audio daemons, or persist XVF chip
    settings, so it can only produce readiness evidence. Full validation stays
    a separate operator-controlled hardware run.
    """

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else _read_mode_env()
    system_env = dict(system_env) if system_env is not None else _read_system_env()
    mic_probe = mic_probe or _probe_xvf_mic()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: _service_state(unit)
            for unit in (
                "jasper-outputd.service",
                "jasper-aec-bridge.service",
                "jasper-aec-init.service",
                "jasper-voice.service",
            )
        }
    )
    if outputd_status is None:
        outputd_status = _query_outputd_status(_outputd_socket_path(system_env))
    if bridge_stats is None:
        bridge_stats = _read_bridge_stats()
    if voice_wake_legs is None:
        voice_wake_legs = _read_voice_wake_legs()
    if bridge_journal_text is None:
        bridge_journal_text = _recent_bridge_journal()

    intent = _intent_from_env(mode_env)
    runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
    chip_available = (
        mic_probe.xvf_present
        and mic_probe.capture_channels == mic_probe.recommended_channels
    )
    profile_status = build_audio_profile_status(
        intent,
        runtime,
        mic_probe,
        bridge_active=service_states.get("jasper-aec-bridge.service") == "active",
        chip_available=chip_available,
    )
    mic = _mic_details(mic_probe)
    dac = _dac_details(system_env, outputd_status)
    checks = {
        "runtime_identity": _runtime_identity_check(system_env),
        "runtime_profile": _runtime_profile_check(profile_status, profile),
        "mic_detected": _check(
            "pass" if chip_available else "fail",
            summary=(
                "XVF3800 6-channel firmware is available."
                if chip_available else "Chip-AEC requires XVF3800 6-channel firmware."
            ),
            observed=mic,
            expected={"family": "xvf3800", "capture_channels": mic_probe.recommended_channels},
        ),
        "runtime_env": _runtime_env_check(runtime),
        "service_state": _service_state_check(service_states),
        "dac_reference": _dac_reference_check(outputd_status),
        "wake_legs": _wake_legs_check(voice_wake_legs),
        "bridge_counters": _bridge_stats_check(bridge_stats, now),
        "measured_drift_delay": _measured_drift_delay_check(bridge_journal_text),
    }
    status = _rollup_status(checks)
    return make_artifact(
        validated_at=now,
        mic_id=str(mic["id"] or "unknown"),
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_readiness_recommendation(status, checks),
        notes=(
            f"{READINESS_SNAPSHOT_KIND}: runtime readiness only",
            "No playback stimulus was generated.",
            "No capture loop was opened.",
            "No XVF chip settings were written or persisted.",
            "Long-window drift and fixed-delay stability require hardware validation.",
        ),
    )


def _load_latest_or_explicit(
    path: Path | None,
    *,
    requested_profile: str | None = None,
    mic_id: str | None = None,
    dac_id: str | None = None,
    now: datetime | None = None,
) -> ArtifactLoadResult:
    def with_errors(
        result: ArtifactLoadResult,
        errors: tuple[str, ...],
    ) -> ArtifactLoadResult:
        if not errors:
            return result
        return ArtifactLoadResult(
            state=result.state,
            artifact=result.artifact,
            path=result.path,
            errors=errors + result.errors,
            stale=result.stale,
        )

    def pointer_errors(result: ArtifactLoadResult) -> tuple[str, ...]:
        if result.state != "loaded" or result.artifact is None:
            detail = "; ".join(result.errors) if result.errors else result.state
            return (f"{LATEST_POINTER_NAME} ignored: {detail}",)
        artifact = result.artifact
        mismatches: list[str] = []
        if requested_profile is not None and artifact.profile != requested_profile:
            mismatches.append(
                f"profile {artifact.profile!r} != {requested_profile!r}",
            )
        if mic_id is not None and artifact.mic_id != mic_id:
            mismatches.append(f"mic_id {artifact.mic_id!r} != {mic_id!r}")
        if dac_id is not None and artifact.dac_id != dac_id:
            mismatches.append(f"dac_id {artifact.dac_id!r} != {dac_id!r}")
        if mismatches:
            return (f"{LATEST_POINTER_NAME} ignored: {', '.join(mismatches)}",)
        return ()

    def load_from_directory(directory: Path) -> ArtifactLoadResult:
        latest = directory / LATEST_POINTER_NAME
        errors: tuple[str, ...] = ()
        if latest.exists():
            pointer_result = load_artifact(latest, now=now)
            errors = pointer_errors(pointer_result)
            if not errors:
                return pointer_result
        return with_errors(
            load_latest_artifact(
                directory,
                mic_id=mic_id,
                dac_id=dac_id,
                profile=requested_profile,
                now=now,
            ),
            errors,
        )

    if path is not None:
        if path.is_dir():
            return load_from_directory(path)
        if path.name == LATEST_POINTER_NAME:
            return load_from_directory(path.parent)
        return load_artifact(path, now=now)
    explicit = os.environ.get("JASPER_AUDIO_VALIDATION_ARTIFACT", "").strip()
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.name == LATEST_POINTER_NAME:
            return load_from_directory(explicit_path.parent)
        return load_artifact(explicit_path, now=now)
    return load_from_directory(artifact_directory())


def summarize_load_result(
    result: ArtifactLoadResult,
    *,
    requested_profile: str | None = None,
    mic_id: str | None = None,
    dac_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = result.path or artifact_directory()
    state = "current" if result.state == "loaded" else result.state
    artifact = result.artifact
    base: dict[str, Any] = {
        "available": artifact is not None,
        "status": artifact.status if artifact is not None else "unknown",
        "state": state,
        "artifact_path": str(path),
    }
    if result.errors:
        base["reason"] = "; ".join(result.errors)
        base["errors"] = list(result.errors)
    if artifact is None:
        return base

    mismatches: list[str] = []
    if requested_profile and artifact.profile != requested_profile:
        mismatches.append(
            f"artifact profile {artifact.profile!r} does not match "
            f"requested {requested_profile!r}"
        )
    if mic_id and artifact.mic_id != mic_id:
        mismatches.append(
            f"artifact mic_id {artifact.mic_id!r} does not match "
            f"requested {mic_id!r}"
        )
    if dac_id and artifact.dac_id != dac_id:
        mismatches.append(
            f"artifact dac_id {artifact.dac_id!r} does not match "
            f"requested {dac_id!r}"
        )
    if mismatches:
        base["state"] = "mismatch"
        base["reason"] = "; ".join(mismatches)
    base.update({
        "schema_version": artifact.schema_version,
        "validated_at": _format_timestamp(artifact.validated_at),
        "profile": artifact.profile,
        "recommendation": artifact.recommendation,
        "hardware": {
            "mic_id": artifact.mic_id,
            "dac_id": artifact.dac_id,
        },
        "checks": dict(artifact.checks),
        "check_statuses": {
            key: value.get("status") if isinstance(value, dict) else None
            for key, value in artifact.checks.items()
        },
    })
    if artifact.notes:
        base["notes"] = list(artifact.notes)
    if artifact.errors:
        base["artifact_errors"] = list(artifact.errors)
    try:
        base["age_seconds"] = max(
            0,
            round(artifact_age(artifact, now=now).total_seconds(), 3),
        )
    except ValidationArtifactError:
        pass
    return base


def latest_artifact_summary(
    *,
    path: Path | None = None,
    requested_profile: str | None = None,
    mic_id: str | None = None,
    dac_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return summarize_load_result(
        _load_latest_or_explicit(
            path,
            requested_profile=requested_profile,
            mic_id=mic_id,
            dac_id=dac_id,
            now=now,
        ),
        requested_profile=requested_profile,
        mic_id=mic_id,
        dac_id=dac_id,
        now=now,
    )


def artifact_age(
    artifact: ValidationArtifact,
    *,
    now: datetime | None = None,
) -> timedelta:
    current = _normalize_datetime(datetime.now(timezone.utc) if now is None else now)
    return current - _normalize_datetime(artifact.validated_at)


def is_artifact_from_future(
    artifact: ValidationArtifact,
    *,
    now: datetime | None = None,
    tolerance: timedelta = DEFAULT_FUTURE_SKEW,
) -> bool:
    return artifact_age(artifact, now=now) < -tolerance


def is_artifact_stale(
    artifact: ValidationArtifact,
    *,
    now: datetime | None = None,
    max_age: timedelta | None = DEFAULT_STALE_AFTER,
) -> bool:
    if max_age is None:
        return False
    return artifact_age(artifact, now=now) > max_age


def _artifact_path(directory: Path, artifact: ValidationArtifact) -> Path:
    ts = _filename_timestamp(artifact.validated_at)
    parts = [
        ts,
        _slug(artifact.mic_id),
        _slug(artifact.dac_id),
        _slug(artifact.profile),
        _slug(artifact.status),
    ]
    return directory / ("__".join(parts) + ".json")


def _format_timestamp(dt: datetime) -> str:
    normalized = _normalize_datetime(dt)
    if normalized.microsecond:
        return normalized.isoformat().replace("+00:00", "Z")
    return normalized.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _filename_timestamp(dt: datetime) -> str:
    normalized = _normalize_datetime(dt)
    return normalized.strftime("%Y%m%dT%H%M%S.%fZ")


def _normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValidationArtifactError(["validated_at must be timezone-aware"])
    return dt.astimezone(timezone.utc)


def _parse_timestamp_field(value: Any, issues: list[str]) -> datetime:
    if not isinstance(value, str) or not value.strip():
        issues.append("validated_at must be a non-empty ISO-8601 string")
        return datetime.fromtimestamp(0, tz=timezone.utc)
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        issues.append("validated_at must be a valid ISO-8601 timestamp")
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        issues.append("validated_at must include a timezone")
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_string(value: Any, field: str, issues: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{field} must be a non-empty string")
        return ""
    return value.strip()


def _string_tuple(value: Any, field: str, issues: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        issues.append(f"{field} must be a string or list of strings")
        return ()
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(f"{field}[{idx}] must be a string")
            continue
        out.append(item)
    return tuple(out)


def _validate_artifact_fields(artifact: ValidationArtifact) -> list[str]:
    issues: list[str] = []
    if artifact.schema_version != CURRENT_SCHEMA_VERSION:
        issues.append(
            f"schema_version must be {CURRENT_SCHEMA_VERSION}, "
            f"got {artifact.schema_version!r}"
        )
    try:
        _normalize_datetime(artifact.validated_at)
    except ValidationArtifactError as e:
        issues.extend(e.issues)
    for field in ("mic_id", "dac_id", "profile", "status", "recommendation"):
        value = getattr(artifact, field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field} must be a non-empty string")
    if artifact.status and artifact.status not in ALLOWED_STATUSES:
        issues.append(
            f"status must be one of {sorted(ALLOWED_STATUSES)}, "
            f"got {artifact.status!r}"
        )
    if not isinstance(artifact.checks, Mapping):
        issues.append("checks must be a mapping")
    else:
        issues.extend(f"checks.{issue}" for issue in _validate_checks(artifact.checks))
    for field in ("notes", "errors"):
        value = getattr(artifact, field)
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            issues.append(f"{field} must be a tuple of strings")
    return issues


def _validate_checks(checks: Mapping[Any, Any]) -> list[str]:
    issues: list[str] = []
    for key, value in checks.items():
        if not isinstance(key, str) or not key.strip():
            issues.append("keys must be non-empty strings")
        if not _is_json_value(value):
            issues.append(f"{key!r} must be JSON-serializable")
    return issues


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_value(v) for k, v in value.items())
    return False


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return slug or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a bounded audio readiness snapshot artifact.",
    )
    parser.add_argument(
        "--profile",
        default=CHIP_AEC_PROFILE,
        choices=(CHIP_AEC_PROFILE,),
        help="Audio profile to snapshot.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the full artifact JSON to stdout.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    artifact = build_chip_aec_readiness_artifact(profile=args.profile)
    directory = args.directory or artifact_directory()
    try:
        path = write_artifact(artifact, directory=directory)
        latest_path = write_latest_pointer(artifact, directory=directory)
    except OSError as e:
        logger.error(
            "event=audio_validation.write_failed profile=%s status=%s error=%s",
            artifact.profile,
            artifact.status,
            e,
        )
        return 1
    logger.info(
        "event=audio_validation.snapshot profile=%s status=%s recommendation=%s path=%s latest=%s",
        artifact.profile,
        artifact.status,
        artifact.recommendation,
        path,
        latest_path,
    )
    if args.stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if artifact.status != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
