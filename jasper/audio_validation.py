"""Persistent audio validation artifacts.

Validation artifacts are small JSON files that record whether a
mic/DAC/profile combination has been validated. This module owns the
schema, parsing, freshness checks, and atomic writes; it does not run
hardware probes or mutate audio services.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CURRENT_SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_DIR = Path("/var/lib/jasper/audio-validation")
DEFAULT_STALE_AFTER = timedelta(days=30)
DEFAULT_FUTURE_SKEW = timedelta(minutes=5)
ALLOWED_STATUSES = frozenset({"pass", "warn", "fail", "unknown"})

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


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
        paths = sorted(p for p in directory_path.glob("*.json") if p.is_file())
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
