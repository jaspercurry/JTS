# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical, fail-closed artifacts for two-boundary excitation admission.

The persisted admission payload remains the frozen schema-version-1
``ExcitationAdmission``.  Boundary and provenance are carried by its
``ArtifactIdentity``: a new, exclusive admission-authority directory has a
canonical contract marker, and generation/playback decisions occupy distinct
versioned path roles inside it.

Feature hosts retain target policy, graph readback, writer locking, playback,
capture, and bundle-manifest ownership.  This module imports or retains none of
those hosts.  Its value-based playback recheck is deliberately pure; the owning
playback adapter must issue current limits/protection under its live guard and
call the recheck immediately before persisting/playing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from jasper.log_event import log_event

from .evidence_identity import ArtifactIdentity
from .excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ProtectionEvidence,
    admit_excitation,
)

ADMISSION_ARTIFACT_CONTRACT_VERSION = 1
ADMISSION_AUTHORITY_KIND = "jts_excitation_admission_authority"
ADMISSION_AUTHORITY_MARKER = "admission_authority.json"
ADMISSION_PATH_ROOT = f"admission/v{ADMISSION_ARTIFACT_CONTRACT_VERSION}"
GENERATION_PATH_PREFIX = f"{ADMISSION_PATH_ROOT}/generation"
PLAYBACK_PATH_PREFIX = f"{ADMISSION_PATH_ROOT}/playback"
MAX_ADMISSION_ARTIFACT_BYTES = 64 * 1024
ADMISSION_FILE_MODE = 0o640
ADMISSION_DIRECTORY_MODE = 0o750

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
logger = logging.getLogger(__name__)


class AdmissionArtifactErrorCode(StrEnum):
    """Stable failures for authority creation, persistence, and resolution."""

    HISTORICAL_EVIDENCE_NOT_ADMITTED = "historical_evidence_not_admitted"
    AUTHORITY_PARENT_INVALID = "admission_authority_parent_invalid"
    AUTHORITY_ALREADY_EXISTS = "admission_authority_already_exists"
    AUTHORITY_MISSING = "admission_authority_missing"
    AUTHORITY_INVALID = "admission_authority_invalid"
    ARTIFACT_PATH_INVALID = "admission_artifact_path_invalid"
    ARTIFACT_PATH_CONFLICT = "admission_artifact_path_conflict"
    ARTIFACT_MISSING = "admission_artifact_missing"
    ARTIFACT_READ_FAILED = "admission_artifact_read_failed"
    ARTIFACT_NOT_REGULAR = "admission_artifact_not_regular"
    ARTIFACT_TOO_LARGE = "admission_artifact_too_large"
    ARTIFACT_INTEGRITY_MISMATCH = "admission_artifact_integrity_mismatch"
    ARTIFACT_NOT_CANONICAL = "admission_artifact_not_canonical"
    ARTIFACT_MALFORMED = "admission_artifact_malformed"
    ARTIFACT_NOT_ALLOWED = "admission_artifact_not_allowed"
    ARTIFACT_PERSIST_FAILED = "admission_artifact_persist_failed"
    ARTIFACT_PERSIST_OUTCOME_UNKNOWN = "admission_artifact_persist_outcome_unknown"


class AdmissionArtifactError(RuntimeError):
    """One authority or artifact cannot be trusted or persisted."""

    def __init__(self, code: AdmissionArtifactErrorCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class HistoricalExcitationEvidence:
    """Diagnostic evidence which predates production admission authority."""

    evidence_fingerprint: str

    def __post_init__(self) -> None:
        _sha256(self.evidence_fingerprint, field="evidence_fingerprint")


@dataclass(frozen=True, slots=True)
class AdmissionAuthority:
    """Verified marker for one new, fail-closed authority directory."""

    directory: Path
    bundle_kind: str
    bundle_id: str
    marker: ArtifactIdentity
    fingerprint: str

    def __post_init__(self) -> None:
        directory = Path(self.directory)
        _identifier(self.bundle_id, field="bundle_id")
        _text(self.bundle_kind, field="bundle_kind")
        _sha256(self.fingerprint, field="fingerprint")
        if directory.name != self.bundle_id:
            raise ValueError("authority directory name must equal bundle_id")
        if not isinstance(self.marker, ArtifactIdentity):
            raise ValueError("marker must be an ArtifactIdentity")
        if (
            self.marker.bundle_kind != self.bundle_kind
            or self.marker.bundle_id != self.bundle_id
            or self.marker.relative_path != ADMISSION_AUTHORITY_MARKER
        ):
            raise ValueError("authority marker identity is inconsistent")
        object.__setattr__(self, "directory", directory)


@dataclass(frozen=True, slots=True)
class GenerationAdmissionArtifact:
    """One verified allowed decision in the versioned generation path role."""

    authority: AdmissionAuthority
    admission_id: str
    admission: ExcitationAdmission
    artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        _validate_admission_artifact(
            self.authority,
            self.admission_id,
            self.admission,
            self.artifact,
            role="generation",
        )


@dataclass(frozen=True, slots=True)
class PlaybackAdmissionArtifact:
    """Final verified playback decision tied to its generation artifact."""

    generation: GenerationAdmissionArtifact
    admission: ExcitationAdmission
    artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.generation, GenerationAdmissionArtifact):
            raise ValueError("generation must be a GenerationAdmissionArtifact")
        _validate_admission_artifact(
            self.generation.authority,
            self.generation.admission_id,
            self.admission,
            self.artifact,
            role="playback",
        )
        if (
            self.admission.request != self.generation.admission.request
            or self.admission.limits != self.generation.admission.limits
        ):
            raise ValueError(
                "playback admission must retain its generation request and limits"
            )
        if self.artifact.fingerprint == self.generation.artifact.fingerprint:
            raise ValueError("playback and generation artifacts must be distinct")


@dataclass(frozen=True, slots=True)
class PlaybackAdmissionResult:
    """One current recheck and its artifact when allowed."""

    decision: ExcitationAdmission
    artifact: PlaybackAdmissionArtifact | None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ExcitationAdmission):
            raise ValueError("decision must be an ExcitationAdmission")
        if self.artifact is not None and not isinstance(
            self.artifact, PlaybackAdmissionArtifact
        ):
            raise ValueError("artifact must be a PlaybackAdmissionArtifact or None")
        if self.decision.allowed != (self.artifact is not None):
            raise ValueError("only an allowed playback decision has an artifact")
        if self.artifact is not None and self.artifact.admission != self.decision:
            raise ValueError("playback artifact must contain the returned decision")

    @property
    def allowed(self) -> bool:
        return self.artifact is not None

    @property
    def refusal_codes(self) -> tuple[str, ...]:
        return tuple(reason.value for reason in self.decision.refusal_reasons)


class _PublishOutcomeUnknown(OSError):
    pass


def _remove_empty_directory(path: Path) -> bool:
    try:
        os.rmdir(path)
    except OSError:
        return False
    try:
        _fsync_directory(path.parent)
    except OSError:
        return False
    return True


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe 1-128 character identifier")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_admission_bytes(admission: ExcitationAdmission) -> bytes:
    """Return the frozen compact bytes used by capture/receipt identities."""

    if not isinstance(admission, ExcitationAdmission):
        raise ValueError("admission must be an ExcitationAdmission")
    return _canonical_json(admission.to_dict())


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json_object(raw: bytes, *, artifact: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_MALFORMED,
            f"{artifact} is invalid JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_MALFORMED,
            f"{artifact} must be a JSON object",
        )
    return payload


def parse_canonical_admission_bytes(raw: bytes) -> ExcitationAdmission:
    """Parse only exact canonical schema-v1 admission bytes."""

    if not isinstance(raw, bytes):
        raise ValueError("admission artifact must be bytes")
    if len(raw) > MAX_ADMISSION_ARTIFACT_BYTES:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_TOO_LARGE,
            "admission artifact exceeds the bounded size limit",
        )
    payload = _parse_json_object(raw, artifact="admission artifact")
    try:
        admission = ExcitationAdmission.from_dict(payload)
    except ValueError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_MALFORMED,
            f"admission artifact is invalid: {exc}",
        ) from exc
    if canonical_admission_bytes(admission) != raw:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_NOT_CANONICAL,
            "admission artifact bytes are not canonical",
        )
    return admission


def _authority_payload(bundle_kind: str, bundle_id: str) -> dict[str, Any]:
    core = {
        "schema_version": 1,
        "kind": ADMISSION_AUTHORITY_KIND,
        "admission_artifact_contract_version": (ADMISSION_ARTIFACT_CONTRACT_VERSION),
        "bundle_kind": bundle_kind,
        "bundle_id": bundle_id,
    }
    return {**core, "fingerprint": hashlib.sha256(_canonical_json(core)).hexdigest()}


def _parse_authority_marker(raw: bytes) -> dict[str, Any]:
    payload = _parse_json_object(raw, artifact="admission authority marker")
    expected_fields = {
        "schema_version",
        "kind",
        "admission_artifact_contract_version",
        "bundle_kind",
        "bundle_id",
        "fingerprint",
    }
    if set(payload) != expected_fields:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            "admission authority marker fields are invalid",
        )
    try:
        expected = _authority_payload(
            _text(payload["bundle_kind"], field="bundle_kind"),
            _identifier(payload["bundle_id"], field="bundle_id"),
        )
    except ValueError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            str(exc),
        ) from exc
    if payload != expected or _canonical_json(payload) != raw:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            "admission authority marker is not exact canonical version 1",
        )
    return payload


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_MISSING,
            f"{path.name} is missing",
        ) from exc
    except OSError as exc:
        code = (
            AdmissionArtifactErrorCode.ARTIFACT_NOT_REGULAR
            if path.is_symlink()
            else AdmissionArtifactErrorCode.ARTIFACT_READ_FAILED
        )
        raise AdmissionArtifactError(
            code, f"could not open {path.name}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdmissionArtifactError(
                AdmissionArtifactErrorCode.ARTIFACT_NOT_REGULAR,
                f"{path.name} must be a regular file",
            )
        if metadata.st_size > max_bytes:
            raise AdmissionArtifactError(
                AdmissionArtifactErrorCode.ARTIFACT_TOO_LARGE,
                f"{path.name} exceeds the bounded size limit",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise AdmissionArtifactError(
                    AdmissionArtifactErrorCode.ARTIFACT_TOO_LARGE,
                    f"{path.name} exceeds the bounded size limit",
                )
            return raw
    except AdmissionArtifactError:
        raise
    except OSError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_READ_FAILED,
            f"could not read {path.name}: {exc}",
        ) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_artifact_parent(root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise OSError("artifact parent escapes its authority directory") from exc
    current = root
    for part in ("", *relative.parts):
        created = False
        if part:
            current /= part
            try:
                current.mkdir()
                created = True
            except FileExistsError:
                pass
        if current.is_symlink() or not current.is_dir():
            raise OSError("artifact parent must be a real directory")
        os.chmod(current, ADMISSION_DIRECTORY_MODE)
        _fsync_directory(current)
        if created:
            _fsync_directory(current.parent)


def _write_once(path: Path, payload: bytes, *, root: Path) -> None:
    _prepare_artifact_parent(root, path.parent)
    parent_gid = path.parent.stat().st_gid
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    published = False
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fchown(stream.fileno(), -1, parent_gid)
            os.fchmod(stream.fileno(), ADMISSION_FILE_MODE)
            os.fsync(stream.fileno())
        os.link(temporary, path)
        try:
            published = True
            os.unlink(temporary)
            temporary = ""
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise _PublishOutcomeUnknown(str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    if not published:
        raise AssertionError("admission artifact was not published")


def _artifact_relative_path(role: str, admission_id: str) -> str:
    _identifier(admission_id, field="admission_id")
    if role not in {"generation", "playback"}:
        raise ValueError("unsupported admission artifact role")
    prefix = GENERATION_PATH_PREFIX if role == "generation" else PLAYBACK_PATH_PREFIX
    return f"{prefix}/{admission_id}.json"


def _admission_id_from_path(relative_path: str, *, role: str) -> str:
    path = PurePosixPath(relative_path)
    expected_parent = PurePosixPath(
        GENERATION_PATH_PREFIX if role == "generation" else PLAYBACK_PATH_PREFIX
    )
    if path.parent != expected_parent or path.suffix != ".json":
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PATH_INVALID,
            f"admission artifact is not in the versioned {role} path role",
        )
    try:
        return _identifier(path.stem, field="admission_id")
    except ValueError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PATH_INVALID,
            str(exc),
        ) from exc


def _artifact_path(authority: AdmissionAuthority, artifact: ArtifactIdentity) -> Path:
    if (
        artifact.bundle_kind != authority.bundle_kind
        or artifact.bundle_id != authority.bundle_id
    ):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "admission artifact belongs to another authority",
        )
    root = authority.directory.resolve()
    target = root / artifact.relative_path
    try:
        target.parent.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PATH_INVALID,
            "admission artifact path escapes its authority directory",
        ) from exc
    return target


def _validate_admission_artifact(
    authority: AdmissionAuthority,
    admission_id: str,
    admission: ExcitationAdmission,
    artifact: ArtifactIdentity,
    *,
    role: str,
) -> None:
    if not isinstance(authority, AdmissionAuthority):
        raise ValueError("authority must be an AdmissionAuthority")
    _identifier(admission_id, field="admission_id")
    if not isinstance(admission, ExcitationAdmission) or not admission.allowed:
        raise ValueError("persisted admission must be an allowed ExcitationAdmission")
    if not isinstance(artifact, ArtifactIdentity):
        raise ValueError("artifact must be an ArtifactIdentity")
    if _admission_id_from_path(artifact.relative_path, role=role) != admission_id:
        raise ValueError("artifact path does not match admission_id")
    _artifact_path(authority, artifact)
    raw = canonical_admission_bytes(admission)
    if artifact.sha256 != hashlib.sha256(raw).hexdigest() or artifact.byte_size != len(
        raw
    ):
        raise ValueError("artifact identity does not match canonical admission bytes")


def create_admission_authority(
    directory: str | Path,
    *,
    bundle_kind: str,
    bundle_id: str,
) -> AdmissionAuthority:
    """Create one new authority directory; existing evidence is never upgraded."""

    kind = _text(bundle_kind, field="bundle_kind")
    identifier = _identifier(bundle_id, field="bundle_id")
    target = Path(directory)
    if target.name != identifier:
        raise ValueError("authority directory name must equal bundle_id")
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_PARENT_INVALID,
            "feature-owned admission authority parent must already exist",
        )
    try:
        os.mkdir(target, ADMISSION_DIRECTORY_MODE)
    except FileExistsError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_ALREADY_EXISTS,
            "existing evidence directory cannot be upgraded to admission authority",
        ) from exc
    except OSError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED,
            f"could not create admission authority directory: {exc}",
        ) from exc
    try:
        os.chmod(target, ADMISSION_DIRECTORY_MODE)
        _fsync_directory(target)
        _fsync_directory(target.parent)
    except OSError as exc:
        _remove_empty_directory(target)
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            "admission authority directory publish outcome is unknown",
        ) from exc
    raw = _canonical_json(_authority_payload(kind, identifier))
    try:
        _write_once(
            target / ADMISSION_AUTHORITY_MARKER,
            raw,
            root=target,
        )
    except _PublishOutcomeUnknown as exc:
        _remove_empty_directory(target)
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            "admission authority marker publish outcome is unknown",
        ) from exc
    except OSError as exc:
        if _remove_empty_directory(target):
            raise AdmissionArtifactError(
                AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED,
                f"could not persist admission authority marker: {exc}",
            ) from exc
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            "admission authority cleanup outcome is unknown",
        ) from exc
    authority = open_admission_authority(
        target,
        expected_bundle_kind=kind,
        expected_bundle_id=identifier,
    )
    log_event(
        logger,
        "audio_measurement.excitation_admission",
        boundary="authority",
        result="created",
        bundle_kind=kind,
        bundle_id=identifier,
    )
    return authority


def open_admission_authority(
    directory: str | Path,
    *,
    expected_bundle_kind: str,
    expected_bundle_id: str,
) -> AdmissionAuthority:
    """Open only an exact authority marker from the new production API."""

    kind = _text(expected_bundle_kind, field="expected_bundle_kind")
    identifier = _identifier(expected_bundle_id, field="expected_bundle_id")
    target = Path(directory)
    if target.name != identifier or target.is_symlink():
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            "authority directory identity is invalid",
        )
    try:
        raw = _read_bounded_regular_file(
            target / ADMISSION_AUTHORITY_MARKER,
            max_bytes=MAX_ADMISSION_ARTIFACT_BYTES,
        )
    except AdmissionArtifactError as exc:
        code = (
            AdmissionArtifactErrorCode.AUTHORITY_MISSING
            if exc.code is AdmissionArtifactErrorCode.ARTIFACT_MISSING
            else AdmissionArtifactErrorCode.AUTHORITY_INVALID
        )
        raise AdmissionArtifactError(code, exc.detail) from exc
    try:
        payload = _parse_authority_marker(raw)
    except AdmissionArtifactError as exc:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            exc.detail,
        ) from exc
    if payload["bundle_kind"] != kind or payload["bundle_id"] != identifier:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            "admission authority marker does not match expected bundle identity",
        )
    marker = ArtifactIdentity(
        bundle_kind=kind,
        bundle_id=identifier,
        relative_path=ADMISSION_AUTHORITY_MARKER,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )
    return AdmissionAuthority(
        directory=target,
        bundle_kind=kind,
        bundle_id=identifier,
        marker=marker,
        fingerprint=payload["fingerprint"],
    )


def _verify_authority(authority: AdmissionAuthority) -> AdmissionAuthority:
    if not isinstance(authority, AdmissionAuthority):
        raise ValueError("authority must be an AdmissionAuthority")
    verified = open_admission_authority(
        authority.directory,
        expected_bundle_kind=authority.bundle_kind,
        expected_bundle_id=authority.bundle_id,
    )
    if verified != authority:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.AUTHORITY_INVALID,
            "admission authority changed after it was opened",
        )
    return verified


def _persist_error(
    authority: AdmissionAuthority,
    *,
    admission_id: str,
    role: str,
    code: AdmissionArtifactErrorCode,
    detail: str,
) -> AdmissionArtifactError:
    log_event(
        logger,
        "audio_measurement.excitation_admission",
        boundary=role,
        result="failed",
        failure_code=code.value,
        bundle_id=authority.bundle_id,
        admission_id=admission_id,
        level=logging.WARNING,
    )
    return AdmissionArtifactError(code, detail)


def refuse_historical_evidence(evidence: HistoricalExcitationEvidence) -> NoReturn:
    """Explicitly refuse evidence which predates this authority contract."""

    if not isinstance(evidence, HistoricalExcitationEvidence):
        raise ValueError("evidence must be HistoricalExcitationEvidence")
    log_event(
        logger,
        "audio_measurement.excitation_admission",
        boundary="authority",
        result=AdmissionArtifactErrorCode.HISTORICAL_EVIDENCE_NOT_ADMITTED.value,
        evidence_fingerprint=evidence.evidence_fingerprint,
    )
    raise AdmissionArtifactError(
        AdmissionArtifactErrorCode.HISTORICAL_EVIDENCE_NOT_ADMITTED,
        "historical evidence predates the production admission API",
    )


def _read_admission(
    authority: AdmissionAuthority,
    artifact: ArtifactIdentity,
    *,
    role: str,
) -> tuple[str, ExcitationAdmission]:
    _verify_authority(authority)
    admission_id = _admission_id_from_path(artifact.relative_path, role=role)
    raw = _read_bounded_regular_file(
        _artifact_path(authority, artifact),
        max_bytes=MAX_ADMISSION_ARTIFACT_BYTES,
    )
    if (
        len(raw) != artifact.byte_size
        or hashlib.sha256(raw).hexdigest() != artifact.sha256
    ):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "admission artifact content does not match its identity",
        )
    admission = parse_canonical_admission_bytes(raw)
    if not admission.allowed:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_NOT_ALLOWED,
            "refused admission is not authority",
        )
    return admission_id, admission


def _persist_admission(
    authority: AdmissionAuthority,
    *,
    admission_id: str,
    role: str,
    admission: ExcitationAdmission,
) -> ArtifactIdentity:
    _verify_authority(authority)
    if not isinstance(admission, ExcitationAdmission) or not admission.allowed:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_NOT_ALLOWED,
            "a refused admission cannot be persisted as authority",
        )
    relative_path = _artifact_relative_path(role, admission_id)
    raw = canonical_admission_bytes(admission)
    artifact = ArtifactIdentity(
        bundle_kind=authority.bundle_kind,
        bundle_id=authority.bundle_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )
    path = _artifact_path(authority, artifact)
    try:
        _write_once(path, raw, root=authority.directory.resolve())
    except FileExistsError as exc:
        raise _persist_error(
            authority,
            admission_id=admission_id,
            role=role,
            code=AdmissionArtifactErrorCode.ARTIFACT_PATH_CONFLICT,
            detail="admission artifact path already exists",
        ) from exc
    except _PublishOutcomeUnknown as exc:
        raise _persist_error(
            authority,
            admission_id=admission_id,
            role=role,
            code=AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            detail="admission artifact publish outcome is unknown",
        ) from exc
    except OSError as exc:
        raise _persist_error(
            authority,
            admission_id=admission_id,
            role=role,
            code=AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED,
            detail=f"could not persist admission artifact: {exc}",
        ) from exc
    try:
        loaded_id, loaded = _read_admission(authority, artifact, role=role)
    except AdmissionArtifactError as exc:
        raise _persist_error(
            authority,
            admission_id=admission_id,
            role=role,
            code=AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            detail=f"published admission artifact could not be verified: {exc.detail}",
        ) from exc
    if loaded_id != admission_id or loaded != admission:
        raise _persist_error(
            authority,
            admission_id=admission_id,
            role=role,
            code=AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            detail="published admission artifact readback changed",
        )
    log_event(
        logger,
        "audio_measurement.excitation_admission",
        boundary=role,
        result="persisted",
        bundle_id=authority.bundle_id,
        admission_id=admission_id,
        artifact_sha256=artifact.sha256,
    )
    return artifact


def persist_generation_admission(
    authority: AdmissionAuthority,
    *,
    admission_id: str,
    admission: ExcitationAdmission,
) -> GenerationAdmissionArtifact:
    """Persist one allowed pre-generation decision at its enforced path role."""

    artifact = _persist_admission(
        authority,
        admission_id=admission_id,
        role="generation",
        admission=admission,
    )
    return GenerationAdmissionArtifact(
        authority=authority,
        admission_id=admission_id,
        admission=admission,
        artifact=artifact,
    )


def read_generation_admission(
    authority: AdmissionAuthority,
    artifact: ArtifactIdentity,
) -> GenerationAdmissionArtifact:
    """Strictly resolve one generation-role admission artifact."""

    admission_id, admission = _read_admission(
        authority,
        artifact,
        role="generation",
    )
    return GenerationAdmissionArtifact(
        authority=authority,
        admission_id=admission_id,
        admission=admission,
        artifact=artifact,
    )


def readmit_excitation_for_playback(
    generation_admission: ExcitationAdmission,
    *,
    current_limits: ExcitationLimits,
    current_protection_evidence: ProtectionEvidence,
) -> ExcitationAdmission:
    """Purely recompute the exact request against caller-issued current values."""

    if not isinstance(generation_admission, ExcitationAdmission):
        raise ValueError("generation_admission must be an ExcitationAdmission")
    if not generation_admission.allowed:
        raise ValueError("generation admission must be allowed")
    if not isinstance(current_limits, ExcitationLimits):
        raise ValueError("current_limits must be ExcitationLimits")
    if not isinstance(current_protection_evidence, ProtectionEvidence):
        raise ValueError("current_protection_evidence must be ProtectionEvidence")
    return admit_excitation(
        generation_admission.request,
        current_limits,
        protection_evidence=current_protection_evidence,
    )


def readmit_and_persist_playback_admission(
    authority: AdmissionAuthority,
    generation: GenerationAdmissionArtifact,
    *,
    current_limits: ExcitationLimits,
    current_protection_evidence: ProtectionEvidence,
) -> PlaybackAdmissionResult:
    """Re-read generation, recompute current admission, and persist if allowed."""

    if not isinstance(generation, GenerationAdmissionArtifact):
        raise ValueError("generation must be a GenerationAdmissionArtifact")
    verified_generation = read_generation_admission(authority, generation.artifact)
    if verified_generation != generation:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "generation admission changed before playback re-admission",
        )
    decision = readmit_excitation_for_playback(
        verified_generation.admission,
        current_limits=current_limits,
        current_protection_evidence=current_protection_evidence,
    )
    if not decision.allowed:
        log_event(
            logger,
            "audio_measurement.excitation_admission",
            boundary="playback",
            result="refused",
            bundle_id=authority.bundle_id,
            admission_id=generation.admission_id,
            refusal_codes=",".join(reason.value for reason in decision.refusal_reasons),
        )
        return PlaybackAdmissionResult(decision=decision, artifact=None)
    artifact = _persist_admission(
        authority,
        admission_id=generation.admission_id,
        role="playback",
        admission=decision,
    )
    playback = PlaybackAdmissionArtifact(
        generation=verified_generation,
        admission=decision,
        artifact=artifact,
    )
    return PlaybackAdmissionResult(decision=decision, artifact=playback)


def read_playback_admission(
    authority: AdmissionAuthority,
    generation: GenerationAdmissionArtifact,
    artifact: ArtifactIdentity,
) -> PlaybackAdmissionArtifact:
    """Strictly resolve a playback artifact tied to one generation artifact."""

    if not isinstance(generation, GenerationAdmissionArtifact):
        raise ValueError("generation must be a GenerationAdmissionArtifact")
    verified_generation = read_generation_admission(authority, generation.artifact)
    if verified_generation != generation:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "generation admission changed before playback artifact resolution",
        )
    admission_id, admission = _read_admission(authority, artifact, role="playback")
    if admission_id != generation.admission_id:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "playback artifact is tied to another generation admission",
        )
    if (
        admission.request != verified_generation.admission.request
        or admission.limits != verified_generation.admission.limits
    ):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "playback artifact does not retain its generation request and limits",
        )
    return PlaybackAdmissionArtifact(
        generation=verified_generation,
        admission=admission,
        artifact=artifact,
    )
