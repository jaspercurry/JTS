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
import time
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
DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"
DAC8X_DAC_ID = "hifiberry_dac8x"
HARDWARE_VALIDATION_PROFILES = (
    CHIP_AEC_PROFILE,
    DAC8X_OUTPUTD_STABILITY_PROFILE,
)
READINESS_SNAPSHOT_KIND = "readiness_snapshot"
HARDWARE_VALIDATION_KIND = "hardware_validation_passive"
DEFAULT_HARDWARE_OBSERVE_SECONDS = 10.0
MAX_SHORT_HARDWARE_OBSERVE_SECONDS = 120.0
LONG_HARDWARE_OBSERVE_SECONDS = 1800.0
MAX_LONG_HARDWARE_OBSERVE_SECONDS = 1800.0
DEFAULT_CHIP_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_AEC_MODE_PATH = Path("/var/lib/jasper/aec_mode.env")
DEFAULT_SYSTEM_ENV_PATH = Path("/etc/jasper/jasper.env")
DEFAULT_BUILD_MANIFEST_PATH = Path("/var/lib/jasper/build.txt")
DEFAULT_BRIDGE_STATS_PATH = Path("/run/jasper/aec_bridge_stats.json")
DEFAULT_OUTPUTD_STATUS_SOCKET = Path("/run/jasper-outputd/control.sock")
EXPECTED_CHIP_WAKE_LEGS = ("on", "chip_aec_150", "chip_aec_210")
CHIP_AEC_PROFILE_READBACK_COMMANDS = (
    "SHF_BYPASS",
    "AUDIO_MGR_SYS_DELAY",
    "AEC_ASROUTONOFF",
    "AEC_FIXEDBEAMSONOFF",
    "AEC_FIXEDBEAMSGATING",
)
CHIP_AEC_CONVERGENCE_COMMAND = "AEC_AECCONVERGED"

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


@dataclass(frozen=True)
class HardwareValidationRun:
    """Result from the operator-controlled hardware validation runner."""

    artifact: ValidationArtifact | None
    refused: bool = False
    refusal_reason: str = ""
    path: Path | None = None
    latest_path: Path | None = None


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
        profile_selection=env.get("JASPER_AUDIO_INPUT_PROFILE", ""),
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
    dac_card = ""
    sample_rate: JsonValue = None
    if isinstance(outputd_dac, dict):
        dac_pcm = str(outputd_dac.get("pcm") or "")
        dac_card = str(outputd_dac.get("card") or "")
        raw_sample_rate = outputd_dac.get("sample_rate")
        if isinstance(raw_sample_rate, (str, int, float, bool)) or raw_sample_rate is None:
            sample_rate = raw_sample_rate
    if not dac_pcm:
        dac_pcm = (
            system_env.get("JASPER_OUTPUTD_DAC_PCM")
            or os.environ.get("JASPER_OUTPUTD_DAC_PCM")
            or "outputd_dac"
        )
    if not dac_card:
        dac_card = (
            system_env.get("JASPER_AUDIO_DAC_CARD")
            or os.environ.get("JASPER_AUDIO_DAC_CARD")
            or ""
        )
    dac_id = (
        system_env.get("JASPER_AUDIO_DAC_ID")
        or os.environ.get("JASPER_AUDIO_DAC_ID")
        or dac_pcm
    )
    dac_route = (
        system_env.get("JASPER_OUTPUT_DAC_ROUTE")
        or os.environ.get("JASPER_OUTPUT_DAC_ROUTE")
        or ""
    )
    return {
        "id": dac_id,
        "pcm": dac_pcm,
        "card": dac_card,
        "route": dac_route,
        "backend": str(
            (outputd_status or {}).get("backend")
            or system_env.get("JASPER_OUTPUTD_BACKEND")
            or os.environ.get("JASPER_OUTPUTD_BACKEND")
            or "unknown"
        ),
        "sample_rate": sample_rate,
    }


def current_artifact_filter_kwargs(
    *,
    requested_profile: str | None = None,
    system_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    outputd_status: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    """Build hardware-bound filters for status-surface artifact reads.

    Always include mic/dac identity, even when detection is unavailable.
    Passing ``unknown`` is intentional: it prevents a previous pass from a
    real mic/DAC from being accepted when the current hardware identity
    cannot be established.
    """

    env = dict(system_env) if system_env is not None else _read_system_env()
    mic = _mic_details(mic_probe if mic_probe is not None else _probe_xvf_mic())
    dac = _dac_details(env, outputd_status)
    return {
        "requested_profile": requested_profile,
        "mic_id": str(mic.get("id") or "unknown"),
        "dac_id": str(dac.get("id") or "unknown"),
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


def _dac_identity_check(
    dac: Mapping[str, JsonValue],
    *,
    expected_id: str,
) -> dict[str, JsonValue]:
    dac_card = str(dac.get("card") or "").strip()
    observed = {
        "id": dac.get("id"),
        "card": dac_card,
        "pcm": dac.get("pcm"),
        "route": dac.get("route"),
        "backend": dac.get("backend"),
        "sample_rate": dac.get("sample_rate"),
    }
    expected = {
        "id": expected_id,
        "card": "recognized non-fallback ALSA card",
    }
    card_ok = bool(dac_card) and dac_card != "A"
    if dac.get("id") == expected_id and card_ok:
        return _check(
            "pass",
            summary=f"Expected output DAC identity {expected_id} is active.",
            observed=observed,
            expected=expected,
        )
    if dac.get("id") == expected_id:
        summary = (
            f"Expected output DAC identity {expected_id} is active, "
            "but ALSA card identity is missing or fallback-like."
        )
    else:
        summary = f"This validation profile must run on {expected_id}."
    return _check(
        "fail",
        summary=summary,
        observed=observed,
        expected=expected,
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


def _outputd_pipeline_service_state_check(service_states: Mapping[str, str]) -> dict[str, JsonValue]:
    required_units = (
        "jasper-outputd.service",
        "jasper-camilla.service",
        "jasper-fanin.service",
    )
    missing = {
        unit: service_states.get(unit, "unknown")
        for unit in required_units
        if service_states.get(unit) != "active"
    }
    if not missing:
        return _check(
            "pass",
            summary="Required outputd/content-pipeline services are active.",
            observed=dict(service_states),
        )
    return _check(
        "fail",
        summary="One or more outputd/content-pipeline services are not active.",
        observed=dict(service_states),
        expected={unit: "active" for unit in required_units},
    )


def _outputd_dac_status_check(outputd_status: Mapping[str, Any] | None) -> dict[str, JsonValue]:
    if not isinstance(outputd_status, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS was unavailable; DAC state could not be read.",
        )
    dac = outputd_status.get("dac")
    if not isinstance(dac, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS does not expose DAC state.",
        )
    raw_sample_rate = dac.get("sample_rate")
    sample_rate = _as_int(raw_sample_rate)
    observed = {
        "pcm": dac.get("pcm"),
        "sample_rate": sample_rate if sample_rate is not None else raw_sample_rate,
        "period_frames": dac.get("period_frames"),
        "buffer_frames": dac.get("buffer_frames"),
        "frames_written": dac.get("frames_written"),
        "xrun_count": dac.get("xrun_count"),
    }
    if observed["pcm"] and sample_rate == 48000:
        return _check(
            "pass",
            summary="outputd exposes active 48 kHz DAC state.",
            observed=observed,
        )
    return _check(
        "fail",
        summary="outputd DAC state is incomplete or not at the expected rate.",
        observed=observed,
        expected={"pcm": "non-empty", "sample_rate": 48000},
    )


def _dac_reference_check(outputd_status: Mapping[str, Any] | None) -> dict[str, JsonValue]:
    if not isinstance(outputd_status, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS was unavailable; speaker-reference state could not be read.",
        )
    refs = outputd_status.get("reference_outputs")
    if not isinstance(refs, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS does not expose reference_outputs.",
        )
    observed = {
        "speaker_reference_source": refs.get("speaker_reference_source"),
        "speaker_reference_active": refs.get("speaker_reference_active"),
        "speaker_reference_channels": refs.get("speaker_reference_channels"),
        "chip_ref_pcm": refs.get("chip_ref_pcm"),
        "chip_ref_sample_rate": refs.get("chip_ref_sample_rate"),
        "chip_ref_period_frames": refs.get("chip_ref_period_frames"),
        "chip_ref_buffer_frames": refs.get("chip_ref_buffer_frames"),
        "udp_target": refs.get("udp_target"),
    }
    if (
        observed["speaker_reference_source"] == "outputd_final_electrical"
        and observed["speaker_reference_channels"] == 2
        and observed["chip_ref_pcm"]
        and observed["udp_target"]
        and observed["chip_ref_sample_rate"] == 16000
    ):
        return _check(
            "pass",
            summary="outputd exposes the speaker monitor plus chip PCM reference outputs.",
            observed=observed,
        )
    return _check(
        "fail",
        summary="outputd speaker/chip reference outputs are not fully configured.",
        observed=observed,
        expected={
            "speaker_reference_source": "outputd_final_electrical",
            "speaker_reference_channels": 2,
            "chip_ref_pcm": "non-empty",
            "udp_target": "non-empty",
            "chip_ref_sample_rate": 16000,
        },
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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except (OverflowError, ValueError):
                return None
    return None


def _nested_int(mapping: Mapping[str, Any] | None, *keys: str) -> int | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _as_int(current)


def _nested_mapping(mapping: Mapping[str, Any] | None, *keys: str) -> Mapping[str, Any] | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current if isinstance(current, Mapping) else None


def _counter_delta(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *keys: str,
) -> int | None:
    start = _nested_int(before, *keys)
    end = _nested_int(after, *keys)
    if start is None or end is None:
        return None
    return max(0, end - start)


def _mapping_delta_total(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *keys: str,
) -> int | None:
    start_map = _nested_mapping(before, *keys)
    end_map = _nested_mapping(after, *keys)
    if start_map is None or end_map is None:
        return None
    total = 0
    for key in set(start_map) | set(end_map):
        start = _as_int(start_map.get(key)) or 0
        end = _as_int(end_map.get(key)) or 0
        total += max(0, end - start)
    return total


def _profile_runtime_ready(checks: Mapping[str, JsonValue]) -> bool:
    required = (
        "runtime_profile",
        "mic_detected",
        "runtime_env",
        "service_state",
        "dac_reference",
    )
    for name in required:
        check = checks.get(name)
        if not isinstance(check, Mapping) or check.get("status") != "pass":
            return False
    return True


def _outputd_reference_health_check(
    samples: list[Mapping[str, Any]],
    *,
    duration_seconds: float,
    report_only: bool,
) -> dict[str, JsonValue]:
    if report_only:
        return _check(
            "not_run",
            summary="Report-only mode did not observe outputd reference movement.",
            required=True,
            observed={"duration_seconds": duration_seconds, "sample_count": len(samples)},
        )
    if len(samples) < 2:
        return _check(
            "unknown",
            summary="outputd STATUS could not be sampled across the validation window.",
            observed={"duration_seconds": duration_seconds, "sample_count": len(samples)},
        )
    before = samples[0]
    after = samples[-1]
    sequence_delta = _counter_delta(before, after, "mix", "reference_sequence")
    dac_frames_delta = _counter_delta(before, after, "dac", "frames_written")
    dac_xrun_delta = _counter_delta(before, after, "dac", "xrun_count")
    content_xrun_delta = _counter_delta(before, after, "content", "xrun_count")
    clipped_delta = _counter_delta(before, after, "mix", "clipped_samples")
    progress_age_ms = _nested_int(after, "watchdog", "last_progress_age_ms")
    observed = {
        "duration_seconds": round(duration_seconds, 3),
        "sample_count": len(samples),
        "reference_sequence_start": _nested_int(before, "mix", "reference_sequence"),
        "reference_sequence_end": _nested_int(after, "mix", "reference_sequence"),
        "reference_sequence_delta": sequence_delta,
        "dac_frames_written_delta": dac_frames_delta,
        "dac_xrun_delta": dac_xrun_delta,
        "content_xrun_delta": content_xrun_delta,
        "clipped_samples_delta": clipped_delta,
        "last_progress_age_ms": progress_age_ms,
    }
    if (dac_xrun_delta or 0) > 0 or (content_xrun_delta or 0) > 0:
        return _check(
            "fail",
            summary="outputd reported xruns during the validation window.",
            observed=observed,
            expected={"xrun_delta": 0},
        )
    if (clipped_delta or 0) > 0:
        return _check(
            "fail",
            summary="outputd reported clipped samples during the validation window.",
            observed=observed,
            expected={"clipped_samples_delta": 0},
        )
    if sequence_delta is None or sequence_delta <= 0:
        return _check(
            "warn",
            summary="outputd reference sequence did not advance during the validation window.",
            observed=observed,
            expected={"reference_sequence_delta": ">0"},
        )
    if progress_age_ms is not None and progress_age_ms > 2500:
        return _check(
            "warn",
            summary="outputd watchdog progress is stale at the end of the validation window.",
            observed=observed,
            expected={"last_progress_age_ms": "<=2500"},
        )
    return _check(
        "pass",
        summary="outputd reference state advanced without xruns or clipping.",
        observed=observed,
    )


def _bridge_counter_window_check(
    samples: list[Mapping[str, Any]],
    *,
    duration_seconds: float,
    report_only: bool,
) -> dict[str, JsonValue]:
    if report_only:
        return _check(
            "not_run",
            summary="Report-only mode did not observe bridge counter movement.",
            observed={"duration_seconds": duration_seconds, "sample_count": len(samples)},
        )
    if len(samples) < 2:
        return _check(
            "unknown",
            summary="AEC bridge stats could not be sampled across the validation window.",
            observed={"duration_seconds": duration_seconds, "sample_count": len(samples)},
        )
    before = samples[0]
    after = samples[-1]
    frames_delta = _counter_delta(before, after, "counters", "frames_processed")
    ref_starved_delta = _counter_delta(before, after, "counters", "ref_starved_frames")
    queue_drop_delta = _mapping_delta_total(before, after, "counters", "queue_drops")
    udp_drop_delta = _mapping_delta_total(before, after, "counters", "udp_send_drops_by_leg")
    observed = {
        "duration_seconds": round(duration_seconds, 3),
        "sample_count": len(samples),
        "frames_processed_delta": frames_delta,
        "ref_starved_frames_delta": ref_starved_delta,
        "queue_drop_delta": queue_drop_delta,
        "udp_send_drop_delta": udp_drop_delta,
    }
    drop_delta = (queue_drop_delta or 0) + (udp_drop_delta or 0)
    if drop_delta > 0:
        return _check(
            "fail",
            summary="AEC bridge dropped queued or UDP frames during the validation window.",
            observed=observed,
            expected={"queue_drop_delta": 0, "udp_send_drop_delta": 0},
        )
    if (ref_starved_delta or 0) > 0:
        return _check(
            "warn",
            summary="AEC bridge reused stale reference frames during the validation window.",
            observed=observed,
            expected={"ref_starved_frames_delta": 0},
        )
    if frames_delta is None or frames_delta <= 0:
        return _check(
            "warn",
            summary="AEC bridge did not process mic frames during the validation window.",
            observed=observed,
            expected={"frames_processed_delta": ">0"},
        )
    return _check(
        "pass",
        summary="AEC bridge counters advanced without drops or reference starvation.",
        observed=observed,
    )


def _hardware_drift_delay_check(
    *,
    bridge_window_check: Mapping[str, JsonValue],
    duration_seconds: float,
) -> dict[str, JsonValue]:
    observed = {
        "duration_seconds": round(duration_seconds, 3),
        "bridge_window_status": bridge_window_check.get("status"),
        "bridge_window_observed": bridge_window_check.get("observed"),
    }
    return _check(
        "not_run",
        summary=(
            "Fixed delay and long-window clock drift were not directly measured. "
            "This runner only records passive bridge/reference stability evidence."
        ),
        observed=observed,
        expected={"operator_probe": "explicit playback/capture drift-delay run"},
    )


def _expected_chip_readback(system_env: Mapping[str, str]) -> dict[str, list[int]]:
    raw_delay = (
        system_env.get("JASPER_AEC_CHIP_SYS_DELAY")
        or os.environ.get("JASPER_AEC_CHIP_SYS_DELAY")
        or "12"
    )
    try:
        sys_delay = int(raw_delay)
    except ValueError:
        sys_delay = 12
    return {
        "SHF_BYPASS": [0],
        "AUDIO_MGR_SYS_DELAY": [sys_delay],
        "AEC_ASROUTONOFF": [1],
        "AEC_FIXEDBEAMSONOFF": [1],
        "AEC_FIXEDBEAMSGATING": [1],
    }


def _normalize_xvf_values(values: Any) -> list[int | float | str]:
    if values is None:
        return []
    if isinstance(values, list):
        raw = values
    elif isinstance(values, tuple):
        raw = list(values)
    else:
        raw = [values]
    out: list[int | float | str] = []
    for value in raw:
        if isinstance(value, bool):
            out.append(int(value))
        elif isinstance(value, (int, float, str)):
            out.append(value)
    return out


def _values_equal(expected: list[int | float], observed: list[int | float | str]) -> bool:
    if len(expected) != len(observed):
        return False
    for want, got in zip(expected, observed, strict=True):
        try:
            if isinstance(want, float):
                if abs(float(got) - want) > 1e-4:
                    return False
            elif int(got) != want:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _chip_profile_readback_check(
    readback: Mapping[str, Any] | None,
    *,
    system_env: Mapping[str, str],
    skipped: bool,
    skip_reason: str = "",
) -> dict[str, JsonValue]:
    expected = _expected_chip_readback(system_env)
    if skipped:
        return _check(
            "not_run",
            summary=skip_reason or "Chip readback was skipped.",
            expected=expected,
        )
    if not isinstance(readback, Mapping) or not readback:
        return _check(
            "unknown",
            summary="XVF3800 profile readback was unavailable.",
            expected=expected,
        )
    observed = {
        key: _normalize_xvf_values(readback.get(key))
        for key in CHIP_AEC_PROFILE_READBACK_COMMANDS
    }
    mismatches = {
        key: {"expected": value, "observed": observed.get(key, [])}
        for key, value in expected.items()
        if not _values_equal(value, observed.get(key, []))
    }
    if mismatches:
        return _check(
            "fail",
            summary="XVF3800 chip-AEC profile readback does not match expected volatile settings.",
            observed={"values": observed, "mismatches": mismatches},
            expected=expected,
        )
    return _check(
        "pass",
        summary="XVF3800 chip-AEC volatile profile readback matches expected settings.",
        observed=observed,
        expected=expected,
    )


def _chip_convergence_check(
    polls: list[Mapping[str, Any]],
    *,
    skipped: bool,
    skip_reason: str = "",
) -> dict[str, JsonValue]:
    if skipped:
        return _check(
            "not_run",
            summary=skip_reason or "Chip convergence polling was skipped.",
            expected={
                CHIP_AEC_CONVERGENCE_COMMAND: (
                    "read-only poll after runtime/ref health passes"
                ),
            },
        )
    if not polls:
        return _check(
            "unknown",
            summary="XVF3800 convergence polling produced no samples.",
            expected={CHIP_AEC_CONVERGENCE_COMMAND: "0 or 1"},
        )
    values: list[int] = []
    errors: list[str] = []
    for poll in polls:
        if "error" in poll:
            errors.append(str(poll["error"]))
            continue
        value = _normalize_xvf_values(poll.get(CHIP_AEC_CONVERGENCE_COMMAND))
        if value:
            try:
                values.append(int(value[0]))
            except (TypeError, ValueError):
                errors.append(f"invalid value {value[0]!r}")
    observed = {
        "poll_count": len(polls),
        "values": values,
        "errors": errors,
        "converged_count": sum(1 for value in values if value == 1),
    }
    if not values:
        return _check(
            "unknown",
            summary="XVF3800 convergence readback was unavailable.",
            observed=observed,
            expected={CHIP_AEC_CONVERGENCE_COMMAND: "0 or 1"},
        )
    if 1 in values:
        first_converged_index = values.index(1)
        nonconverged_after_first = [
            value for value in values[first_converged_index + 1:] if value != 1
        ]
        observed["first_converged_sample_index"] = first_converged_index
        observed["nonconverged_after_first_count"] = len(nonconverged_after_first)
        if nonconverged_after_first:
            return _check(
                "warn",
                summary=(
                    "XVF3800 reported AEC convergence but did not remain "
                    "converged for the full validation window."
                ),
                observed=observed,
                expected={
                    CHIP_AEC_CONVERGENCE_COMMAND:
                        "1, with no later 0 once convergence is observed",
                },
            )
        return _check(
            "pass",
            summary="XVF3800 reported stable AEC convergence during the validation window.",
            observed=observed,
            expected={CHIP_AEC_CONVERGENCE_COMMAND: 1},
        )
    return _check(
        "not_observed",
        summary=(
            "XVF3800 did not report AEC convergence during the passive window. "
            "Without an explicit far-end stimulus, this may mean there was "
            "nothing meaningful for the chip to converge on."
        ),
        observed=observed,
        expected={
            CHIP_AEC_CONVERGENCE_COMMAND:
                "1 when meaningful far-end audio is present",
        },
    )


def _hardware_recommendation(status: str, checks: Mapping[str, Mapping[str, Any]]) -> str:
    readiness_names = {
        "runtime_identity",
        "runtime_profile",
        "mic_detected",
        "runtime_env",
        "service_state",
        "dac_reference",
        "wake_legs",
        "bridge_counters",
        "measured_drift_delay",
    }
    readiness_checks = {
        name: check for name, check in checks.items() if name in readiness_names
    }
    readiness = _readiness_recommendation(status, readiness_checks)
    if readiness not in {
        "run_hardware_validation",
        "chip_aec_validated",
        "review_audio_validation_warnings",
    }:
        return readiness
    if checks.get("outputd_reference_health", {}).get("status") == "fail":
        return "fix_outputd_reference_health_before_chip_validation"
    if checks.get("bridge_counter_window", {}).get("status") == "fail":
        return "fix_aec_bridge_stability_before_chip_validation"
    if checks.get("chip_profile_readback", {}).get("status") == "fail":
        return "rerun_aec_init_or_reconciler_before_chip_validation"
    if checks.get("outputd_reference_health", {}).get("status") in {"unknown", "not_run", "warn"}:
        return "review_outputd_reference_health_before_chip_validation"
    if checks.get("bridge_counter_window", {}).get("status") in {"unknown", "not_run", "warn"}:
        return "review_aec_bridge_reference_stability"
    if checks.get("measured_drift_delay", {}).get("status") in {"not_run", "unknown"}:
        return "run_drift_delay_validation"
    if checks.get("chip_convergence", {}).get("status") in {
        "unknown",
        "not_run",
        "not_observed",
        "warn",
    }:
        return "review_chip_convergence_or_run_long_window"
    if status == "pass":
        return "chip_aec_measured_validated"
    return "review_audio_validation_warnings"


def _outputd_stability_recommendation(
    status: str,
    checks: Mapping[str, Mapping[str, Any]],
) -> str:
    if checks.get("service_state", {}).get("status") == "fail":
        return "fix_outputd_pipeline_services_before_validation"
    if checks.get("dac_identity", {}).get("status") == "fail":
        return "run_on_hifiberry_dac8x_target_before_validation"
    if checks.get("dac_output", {}).get("status") in {"fail", "unknown", "not_run"}:
        return "fix_outputd_runtime_observability_before_validation"
    if checks.get("outputd_reference_health", {}).get("status") == "fail":
        return "fix_outputd_stability_before_dac_validation"
    if checks.get("outputd_reference_health", {}).get("status") in {
        "unknown",
        "not_run",
        "warn",
    }:
        return "review_outputd_reference_health_before_dac_validation"
    if status == "pass":
        return "outputd_dac_stability_validated"
    return "review_audio_validation_warnings"


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


def build_outputd_stability_hardware_validation_artifact(
    *,
    now: datetime | None = None,
    profile: str = DAC8X_OUTPUTD_STABILITY_PROFILE,
    system_env: Mapping[str, str] | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
) -> ValidationArtifact:
    """Build a measured outputd/DAC stability artifact.

    This profile intentionally excludes chip-AEC and voice prerequisites so
    DAC/content-loop stability can be validated while chip-AEC is disabled or
    the voice daemon is parked for first-time provider setup.
    """

    now = datetime.now(timezone.utc) if now is None else now
    system_env = dict(system_env) if system_env is not None else _read_system_env()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: _service_state(unit)
            for unit in (
                "jasper-outputd.service",
                "jasper-camilla.service",
                "jasper-fanin.service",
            )
        }
    )
    outputd_status_samples = list(outputd_status_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if outputd_status is None:
        outputd_status = _query_outputd_status(_outputd_socket_path(system_env))

    dac = _dac_details(system_env, outputd_status)
    checks: dict[str, Mapping[str, Any]] = {
        "runtime_identity": _runtime_identity_check(system_env),
        "service_state": _outputd_pipeline_service_state_check(service_states),
        "dac_identity": _dac_identity_check(dac, expected_id=DAC8X_DAC_ID),
        "dac_output": _outputd_dac_status_check(outputd_status),
        "outputd_reference_health": _outputd_reference_health_check(
            outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
        ),
        "operator_control": _check(
            "pass",
            required=False,
            summary="Validation was explicitly operator-invoked and bounded.",
            observed={
                "duration_seconds": round(duration_seconds, 3),
                "report_only": report_only,
                "forced": forced,
                "playback_generated": False,
                "capture_loop_opened": False,
                "xvf_reads": False,
                "xvf_persistent_writes": False,
            },
        ),
    }
    status = _rollup_status(checks)
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    notes = [
        f"{HARDWARE_VALIDATION_KIND}: passive outputd/DAC stability evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        (
            "Chip-AEC, AEC bridge, XVF readback, and jasper-voice state are "
            "not prerequisites for this profile."
        ),
    ]
    return make_artifact(
        validated_at=now,
        mic_id="not_applicable",
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_outputd_stability_recommendation(status, checks),
        notes=tuple(notes),
        errors=tuple(errors),
    )


def build_chip_aec_hardware_validation_artifact(
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
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    bridge_stats_samples: list[Mapping[str, Any]] | None = None,
    chip_readback: Mapping[str, Any] | None = None,
    chip_convergence_polls: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
    chip_probe_skipped: bool = False,
    chip_probe_skip_reason: str = "",
) -> ValidationArtifact:
    """Build a schema-v1 measured chip-AEC validation artifact.

    The default hardware runner is passive: it samples already-running
    outputd/bridge state and read-only XVF parameters. It never generates
    speaker output, opens capture streams, or writes/persists chip settings.
    """

    if profile == DAC8X_OUTPUTD_STABILITY_PROFILE:
        return build_outputd_stability_hardware_validation_artifact(
            now=now,
            profile=profile,
            system_env=system_env,
            service_states=service_states,
            outputd_status=outputd_status,
            outputd_status_samples=outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
            forced=forced,
        )

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else _read_mode_env()
    system_env = dict(system_env) if system_env is not None else _read_system_env()
    outputd_status_samples = list(outputd_status_samples or [])
    bridge_stats_samples = list(bridge_stats_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if bridge_stats is None and bridge_stats_samples:
        bridge_stats = bridge_stats_samples[0]

    readiness = build_chip_aec_readiness_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=outputd_status,
        bridge_stats=bridge_stats,
        voice_wake_legs=voice_wake_legs,
        bridge_journal_text=bridge_journal_text,
    )
    checks: dict[str, Mapping[str, Any]] = {
        key: value
        for key, value in readiness.checks.items()
        if isinstance(value, Mapping)
    }
    outputd_health = _outputd_reference_health_check(
        outputd_status_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    bridge_window = _bridge_counter_window_check(
        bridge_stats_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    checks["outputd_reference_health"] = outputd_health
    checks["bridge_counter_window"] = bridge_window
    checks["measured_drift_delay"] = _hardware_drift_delay_check(
        bridge_window_check=bridge_window,
        duration_seconds=duration_seconds,
    )
    skip_chip = chip_probe_skipped or not (
        _profile_runtime_ready(checks)
        and outputd_health.get("status") == "pass"
    )
    skip_reason = chip_probe_skip_reason
    if skip_chip and not skip_reason:
        skip_reason = (
            "Chip readback/convergence polling waits for passing runtime "
            "and outputd reference health."
        )
    checks["chip_profile_readback"] = _chip_profile_readback_check(
        chip_readback,
        system_env=system_env,
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["chip_convergence"] = _chip_convergence_check(
        list(chip_convergence_polls or []),
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["operator_control"] = _check(
        "pass",
        required=False,
        summary="Validation was explicitly operator-invoked and bounded.",
        observed={
            "duration_seconds": round(duration_seconds, 3),
            "report_only": report_only,
            "forced": forced,
            "playback_generated": False,
            "capture_loop_opened": False,
            "xvf_persistent_writes": False,
        },
    )
    status = _rollup_status(checks)
    dac = _dac_details(system_env, outputd_status)
    notes = (
        f"{HARDWARE_VALIDATION_KIND}: passive operator-controlled hardware evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        "Only read-only XVF parameters were polled.",
        "No XVF chip settings were written or persisted.",
        (
            "Fixed delay and long-window drift still require an explicit "
            "playback/capture validation mode."
        ),
    )
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    return make_artifact(
        validated_at=now,
        mic_id=readiness.mic_id,
        dac_id=str(dac["id"] or readiness.dac_id or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_hardware_recommendation(status, checks),
        notes=notes,
        errors=tuple(errors),
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


def _collect_service_states() -> dict[str, str]:
    return {
        unit: _service_state(unit)
        for unit in (
            "jasper-outputd.service",
            "jasper-camilla.service",
            "jasper-fanin.service",
            "jasper-aec-bridge.service",
            "jasper-aec-init.service",
            "jasper-voice.service",
        )
    }


def _parse_xvf_cli_value(stdout: str, command: str) -> list[int | float | str] | None:
    pattern = re.compile(rf"^{re.escape(command)}:\s*\[(?P<body>.*)\]\s*$", re.MULTILINE)
    match = pattern.search(stdout)
    if not match:
        return None
    body = match.group("body").strip()
    if not body:
        return []
    values: list[int | float | str] = []
    for part in body.split(","):
        raw = part.strip().strip("'\"")
        if not raw:
            continue
        try:
            values.append(int(raw, 0))
            continue
        except ValueError:
            pass
        try:
            values.append(float(raw))
            continue
        except ValueError:
            values.append(raw)
    return values


def _read_xvf_parameter(command: str, *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "jasper.xvf.xvf_host", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(
            "event=audio_hw_validation.xvf_read_failed command=%s error=%s",
            command,
            e,
        )
        return {"error": str(e)}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {"error": detail or f"xvf_host exited {result.returncode}"}
    values = _parse_xvf_cli_value(result.stdout, command)
    if values is None:
        return {"error": "xvf_host output did not include a parseable value"}
    return {command: values}


def _read_chip_profile_parameters(*, timeout: float = 5.0) -> dict[str, Any]:
    readback: dict[str, Any] = {}
    for command in CHIP_AEC_PROFILE_READBACK_COMMANDS:
        result = _read_xvf_parameter(command, timeout=timeout)
        if "error" in result:
            readback[command] = {"error": result["error"]}
        else:
            readback[command] = result.get(command, [])
    return readback


def _poll_chip_convergence(
    *,
    duration_seconds: float,
    interval_seconds: float,
    timeout: float = 5.0,
) -> list[Mapping[str, Any]]:
    polls: list[Mapping[str, Any]] = []
    deadline = time.monotonic() + max(0.0, duration_seconds)
    while True:
        result = _read_xvf_parameter(CHIP_AEC_CONVERGENCE_COMMAND, timeout=timeout)
        polls.append(result)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.1, interval_seconds), remaining))
    return polls


def _duration_limit(duration_seconds: float, *, allow_long: bool) -> float:
    limit = (
        MAX_LONG_HARDWARE_OBSERVE_SECONDS
        if allow_long else MAX_SHORT_HARDWARE_OBSERVE_SECONDS
    )
    if duration_seconds < 0:
        raise ValueError("duration must be non-negative")
    if duration_seconds > limit:
        mode = "--allow-long or --long-window" if not allow_long else "a shorter duration"
        raise ValueError(
            f"duration {duration_seconds:g}s exceeds the {limit:g}s bound; use {mode}"
        )
    return duration_seconds


def _chip_runtime_refusal_reason(artifact: ValidationArtifact) -> str:
    checks = artifact.checks
    runtime_profile = checks.get("runtime_profile")
    if isinstance(runtime_profile, Mapping) and runtime_profile.get("status") != "pass":
        return str(runtime_profile.get("summary") or "Chip-AEC profile is not active.")
    runtime_env = checks.get("runtime_env")
    if isinstance(runtime_env, Mapping) and runtime_env.get("status") != "pass":
        return str(runtime_env.get("summary") or "Chip-AEC runtime env is incomplete.")
    return ""


def _complete_hardware_validation_result(
    artifact: ValidationArtifact,
    *,
    directory: Path | None,
    report_only: bool,
    stdout: bool,
) -> HardwareValidationRun:
    if stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    if report_only:
        logger.info(
            "event=audio_hw_validation.report profile=%s status=%s recommendation=%s",
            artifact.profile,
            artifact.status,
            artifact.recommendation,
        )
        return HardwareValidationRun(artifact=artifact)

    target_dir = directory or artifact_directory()
    try:
        path = write_artifact(artifact, directory=target_dir)
        latest_path = write_latest_pointer(artifact, directory=target_dir)
    except OSError as e:
        logger.error(
            "event=audio_hw_validation.write_failed profile=%s status=%s error=%s",
            artifact.profile,
            artifact.status,
            e,
        )
        return HardwareValidationRun(artifact=artifact, refused=True, refusal_reason=str(e))
    logger.info(
        "event=audio_hw_validation.write profile=%s status=%s recommendation=%s path=%s latest=%s",
        artifact.profile,
        artifact.status,
        artifact.recommendation,
        path,
        latest_path,
    )
    return HardwareValidationRun(artifact=artifact, path=path, latest_path=latest_path)


def run_audio_hardware_validation(
    *,
    profile: str = CHIP_AEC_PROFILE,
    directory: Path | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    poll_interval_seconds: float = DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
    report_only: bool = False,
    force: bool = False,
    allow_long: bool = False,
    stdout: bool = False,
    now: datetime | None = None,
) -> HardwareValidationRun:
    """Run a bounded operator-controlled audio hardware validator."""

    now = datetime.now(timezone.utc) if now is None else now
    duration_seconds = _duration_limit(
        0.0 if report_only else duration_seconds,
        allow_long=allow_long,
    )
    if poll_interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info(
        "event=audio_hw_validation.start profile=%s duration_seconds=%.3f report_only=%s force=%s",
        profile,
        duration_seconds,
        int(report_only),
        int(force),
    )

    system_env = _read_system_env()
    if profile == DAC8X_OUTPUTD_STABILITY_PROFILE:
        service_states = _collect_service_states()
        outputd_socket = _outputd_socket_path(system_env)
        first_outputd = _query_outputd_status(outputd_socket)
        outputd_samples: list[Mapping[str, Any]] = []
        if isinstance(first_outputd, Mapping):
            outputd_samples.append(first_outputd)
        if not report_only and duration_seconds > 0:
            time.sleep(duration_seconds)
            final_outputd = _query_outputd_status(outputd_socket)
            if isinstance(final_outputd, Mapping):
                outputd_samples.append(final_outputd)
        artifact = build_outputd_stability_hardware_validation_artifact(
            now=now,
            profile=profile,
            system_env=system_env,
            service_states=service_states,
            outputd_status=first_outputd,
            outputd_status_samples=outputd_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
            forced=force,
        )
        return _complete_hardware_validation_result(
            artifact,
            directory=directory,
            report_only=report_only,
            stdout=stdout,
        )

    mode_env = _read_mode_env()
    mic_probe = _probe_xvf_mic()
    service_states = _collect_service_states()
    outputd_socket = _outputd_socket_path(system_env)
    first_outputd = _query_outputd_status(outputd_socket)
    first_bridge = _read_bridge_stats()
    voice_wake_legs = _read_voice_wake_legs()
    bridge_journal_text = _recent_bridge_journal()

    readiness = build_chip_aec_readiness_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
        bridge_journal_text=bridge_journal_text,
    )
    refusal_reason = _chip_runtime_refusal_reason(readiness)
    if refusal_reason and not force:
        logger.warning(
            "event=audio_hw_validation.refused profile=%s reason=%s",
            profile,
            refusal_reason,
        )
        return HardwareValidationRun(
            artifact=None,
            refused=True,
            refusal_reason=refusal_reason,
        )

    outputd_samples: list[Mapping[str, Any]] = []
    bridge_samples: list[Mapping[str, Any]] = []
    if isinstance(first_outputd, Mapping):
        outputd_samples.append(first_outputd)
    if isinstance(first_bridge, Mapping):
        bridge_samples.append(first_bridge)
    preflight_seconds = 0.0
    remaining_seconds = 0.0
    if not report_only and duration_seconds > 0:
        preflight_seconds = min(1.0, duration_seconds)
        remaining_seconds = max(0.0, duration_seconds - preflight_seconds)
        time.sleep(preflight_seconds)
        preflight_outputd = _query_outputd_status(outputd_socket)
        if isinstance(preflight_outputd, Mapping):
            outputd_samples.append(preflight_outputd)
        preflight_bridge = _read_bridge_stats()
        if isinstance(preflight_bridge, Mapping):
            bridge_samples.append(preflight_bridge)
    probe_gate = build_chip_aec_hardware_validation_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
        bridge_journal_text=bridge_journal_text,
        outputd_status_samples=outputd_samples,
        bridge_stats_samples=bridge_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
        forced=force,
        chip_probe_skipped=True,
        chip_probe_skip_reason="probe gate evaluation",
    )
    outputd_health = probe_gate.checks.get("outputd_reference_health")
    chip_probe_allowed = (
        not report_only
        and _profile_runtime_ready(probe_gate.checks)
        and isinstance(outputd_health, Mapping)
        and outputd_health.get("status") == "pass"
    )
    chip_readback: Mapping[str, Any] | None = None
    chip_polls: list[Mapping[str, Any]] = []
    skip_reason = ""
    if chip_probe_allowed:
        logger.info("event=audio_hw_validation.chip_probe_start profile=%s", profile)
        chip_readback = _read_chip_profile_parameters()
        chip_polls = _poll_chip_convergence(
            duration_seconds=remaining_seconds,
            interval_seconds=poll_interval_seconds,
        )
        logger.info(
            "event=audio_hw_validation.chip_probe_complete profile=%s polls=%d",
            profile,
            len(chip_polls),
        )
    else:
        skip_reason = (
            "Chip readback/convergence polling waits for report_only=0, "
            "passing runtime checks, and passing outputd reference health."
        )
        if remaining_seconds > 0:
            time.sleep(remaining_seconds)
    if not report_only and duration_seconds > preflight_seconds:
        final_outputd = _query_outputd_status(outputd_socket)
        if isinstance(final_outputd, Mapping):
            outputd_samples.append(final_outputd)
        final_bridge = _read_bridge_stats()
        if isinstance(final_bridge, Mapping):
            bridge_samples.append(final_bridge)

    artifact = build_chip_aec_hardware_validation_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=first_outputd,
        bridge_stats=first_bridge,
        voice_wake_legs=voice_wake_legs,
        bridge_journal_text=bridge_journal_text,
        outputd_status_samples=outputd_samples,
        bridge_stats_samples=bridge_samples,
        chip_readback=chip_readback,
        chip_convergence_polls=chip_polls,
        duration_seconds=duration_seconds,
        report_only=report_only,
        forced=force,
        chip_probe_skipped=not chip_probe_allowed,
        chip_probe_skip_reason=skip_reason,
    )
    return _complete_hardware_validation_result(
        artifact,
        directory=directory,
        report_only=report_only,
        stdout=stdout,
    )


def run_chip_aec_hardware_validation(
    *,
    profile: str = CHIP_AEC_PROFILE,
    directory: Path | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    poll_interval_seconds: float = DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
    report_only: bool = False,
    force: bool = False,
    allow_long: bool = False,
    stdout: bool = False,
    now: datetime | None = None,
) -> HardwareValidationRun:
    """Compatibility wrapper for the original chip-AEC validation API."""

    return run_audio_hardware_validation(
        profile=profile,
        directory=directory,
        duration_seconds=duration_seconds,
        poll_interval_seconds=poll_interval_seconds,
        report_only=report_only,
        force=force,
        allow_long=allow_long,
        stdout=stdout,
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


def hardware_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded operator-controlled audio hardware validation.",
    )
    parser.add_argument(
        "--profile",
        default=CHIP_AEC_PROFILE,
        choices=HARDWARE_VALIDATION_PROFILES,
        help="Audio profile to validate.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=DEFAULT_HARDWARE_OBSERVE_SECONDS,
        help=(
            "Passive outputd observation window (plus bridge counters for "
            "chip-AEC), not a hard total wall-clock cap because bounded "
            "XVF readback/poll subprocesses may add time for chip-AEC "
            f"(default: {DEFAULT_HARDWARE_OBSERVE_SECONDS:g}s; "
            f"max without --allow-long: {MAX_SHORT_HARDWARE_OBSERVE_SECONDS:g}s)."
        ),
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_CHIP_POLL_INTERVAL_SECONDS,
        help=(
            "Read-only chip convergence poll interval "
            f"(default: {DEFAULT_CHIP_POLL_INTERVAL_SECONDS:g}s)."
        ),
    )
    parser.add_argument(
        "--long-window",
        action="store_true",
        help=(
            "Use the explicit 30-minute passive observation window. "
            "This does not generate playback."
        ),
    )
    parser.add_argument(
        "--allow-long",
        action="store_true",
        help=(
            "Allow the passive observation window above the default short "
            "bound, up to 30 minutes."
        ),
    )
    parser.add_argument(
        "--dry-run",
        "--report-only",
        dest="report_only",
        action="store_true",
        help="Collect a report without sleeping for the validation window or writing artifacts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="For chip-AEC, write/report an artifact even when chip-AEC is not requested and active.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full artifact JSON to stdout.",
    )
    args = parser.parse_args(argv)
    duration = (
        LONG_HARDWARE_OBSERVE_SECONDS
        if args.long_window else args.duration_seconds
    )
    allow_long = args.allow_long or args.long_window
    try:
        result = run_audio_hardware_validation(
            profile=args.profile,
            directory=args.directory,
            duration_seconds=duration,
            poll_interval_seconds=args.poll_interval_seconds,
            report_only=args.report_only,
            force=args.force,
            allow_long=allow_long,
            stdout=args.stdout or args.report_only,
        )
    except ValueError as e:
        parser.error(str(e))
    if result.refused:
        return 2
    if result.artifact is None:
        return 2
    return 0 if result.artifact.status != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
