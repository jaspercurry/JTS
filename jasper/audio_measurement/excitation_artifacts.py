# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
from pathlib import Path
from typing import Any

from jasper.atomic_io import fsync_directory
from jasper.log_event import log_event

from .evidence_identity import ArtifactIdentity

ADMISSION_ARTIFACT_CONTRACT_VERSION = 1
ADMISSION_AUTHORITY_KIND = "jts_excitation_admission_authority"
ADMISSION_AUTHORITY_MARKER = "admission_authority.json"
MAX_ADMISSION_ARTIFACT_BYTES = 64 * 1024
ADMISSION_FILE_MODE = 0o640
# No SUID/SGID bits: a hardened unit (RestrictSUIDSGID=) refuses a requested
# one. The installer's setgid parent directories already confer group
# inheritance for root/service co-publishing; this constant never asks for it.
ADMISSION_DIRECTORY_MODE = 0o750

def ensure_directory_mode(path: "str | os.PathLike[str]") -> None:
    """Correct a directory's permission bits to ADMISSION_DIRECTORY_MODE only when
    the umask left them different: chmod never names the setgid bit, so the bit
    an installer-owned setgid parent conferred is neither requested (refused
    under RestrictSUIDSGID) nor stripped."""
    if stat.S_IMODE(os.stat(path).st_mode) & 0o777 != ADMISSION_DIRECTORY_MODE:
        os.chmod(path, ADMISSION_DIRECTORY_MODE)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
logger = logging.getLogger(__name__)


class AdmissionArtifactErrorCode(StrEnum):
    """Stable failures for authority creation, persistence, and resolution."""

    AUTHORITY_PARENT_INVALID = "admission_authority_parent_invalid"
    AUTHORITY_ALREADY_EXISTS = "admission_authority_already_exists"
    AUTHORITY_MISSING = "admission_authority_missing"
    AUTHORITY_INVALID = "admission_authority_invalid"
    ARTIFACT_MISSING = "admission_artifact_missing"
    ARTIFACT_READ_FAILED = "admission_artifact_read_failed"
    ARTIFACT_NOT_REGULAR = "admission_artifact_not_regular"
    ARTIFACT_TOO_LARGE = "admission_artifact_too_large"
    ARTIFACT_MALFORMED = "admission_artifact_malformed"
    ARTIFACT_PERSIST_FAILED = "admission_artifact_persist_failed"
    ARTIFACT_PERSIST_OUTCOME_UNKNOWN = "admission_artifact_persist_outcome_unknown"


class AdmissionArtifactError(RuntimeError):
    """One authority or artifact cannot be trusted or persisted."""

    def __init__(self, code: AdmissionArtifactErrorCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


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


class _PublishOutcomeUnknown(OSError):
    pass


def _remove_empty_directory(path: Path) -> bool:
    try:
        os.rmdir(path)
    except OSError:
        return False
    try:
        fsync_directory(path.parent)
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
        # Every artifact write re-walks this path, including the marker
        # write inside create_admission_authority itself -- an unconditional
        # chmod here would undo that function's own skip-if-matching check.
        ensure_directory_mode(current)
        fsync_directory(current)
        if created:
            fsync_directory(current.parent)


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
            fsync_directory(path.parent)
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
        # A setgid parent can already leave mkdir's result at MODE (umask
        # permitting); chmod only to correct it, never to add SGID back.
        ensure_directory_mode(target)
        fsync_directory(target)
        fsync_directory(target.parent)
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
