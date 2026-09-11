# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, bundle-scoped storage for Active commissioning evidence.

It reopens the bundle's Shared admission authority, publishes immutable
canonical artifacts under ``evidence/v1/artifacts/``, and verifies exact bytes
on every reopen. ``info.json`` and the fail-soft forensic manifest are not
evidence authority.

One raw artifact is capped at the 5 MiB crossover-capture ceiling; the total
bound is a hard safety ceiling, not a retention target. A capture WAV is
published once at its authoritative path; this store creates no manifest or
shadow WAV copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from jasper.atomic_io import fsync_directory
from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    AdmissionAuthority,
)

from .bundles import (
    ensure_directory_mode,
    BUNDLE_FILE_MODE,
    BUNDLE_KIND,
    open_bundle_admission_authority,
)
from .test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

# "v1" is the artifact namespace's on-disk schema version, still written by
# crossover_v2 and attribution -- unrelated to the deleted v1 commissioning
# lane (ADR-0288).
EVIDENCE_ROOT = "evidence/v1"
MAX_EVIDENCE_ARTIFACT_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES
# Bound for a read outside ``evidence/v1/artifacts/`` -- e.g.
# crossover_v2/record_index.reopen_measurement_capture reopening take JSON
# and ``summed/*.wav`` capture bytes.
MAX_NON_ARTIFACT_READ_BYTES = 32 * 1024 * 1024
# Hard ceiling on every byte `_authoritative_total` walks: `evidence/v1`,
# `stimuli` and `admission`. v2 capture WAVs under `summed/`/`captures/`
# live outside those subtrees and are not counted here. Fixed disk budget
# (≈3.8 GiB) carried over from the deleted v1 commissioning lane's
# proven-maximum capture matrix (582 artifacts × 5 MiB + 1 GiB); v2 writes
# only KB-scale JSON under this subtree, so the ceiling does not bind today.
MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES = (
    582 * MAX_EVIDENCE_ARTIFACT_BYTES
) + (1024 * 1024 * 1024)
# Free space a durable evidence publish must leave behind, as headroom for the
# run still in progress; open/current bundles are retention-protected, so
# retention alone cannot rescue a full filesystem while a run is growing.
# Deliberately NOT ``bundles.DEFAULT_SESSIONS_MAX_BYTES``: that answers "how
# much history do we keep", and tying the two makes a retention raise quadruple
# the free space a Pi needs before it may publish anything. Change this only
# when the publish-headroom argument itself changes.
MIN_FREE_SPACE_AFTER_PUBLISH_BYTES = 256 * 1024 * 1024


class CommissioningEvidenceStoreErrorCode(StrEnum):
    """Stable strict-store failure classes."""

    INVALID_PATH = "commissioning_evidence_invalid_path"
    WRONG_AUTHORITY = "commissioning_evidence_wrong_authority"
    MISSING = "commissioning_evidence_missing"
    NOT_REGULAR = "commissioning_evidence_not_regular"
    TOO_LARGE = "commissioning_evidence_too_large"
    TOTAL_TOO_LARGE = "commissioning_evidence_total_too_large"
    INSUFFICIENT_SPACE = "commissioning_evidence_insufficient_space"
    INTEGRITY_MISMATCH = "commissioning_evidence_integrity_mismatch"
    NOT_CANONICAL = "commissioning_evidence_not_canonical"
    MALFORMED = "commissioning_evidence_malformed"
    PATH_CONFLICT = "commissioning_evidence_path_conflict"
    PERSIST_FAILED = "commissioning_evidence_persist_failed"
    PERSIST_OUTCOME_UNKNOWN = "commissioning_evidence_persist_outcome_unknown"


class CommissioningEvidenceStoreError(RuntimeError):
    """One authoritative evidence artifact cannot be trusted or persisted."""

    def __init__(
        self,
        code: CommissioningEvidenceStoreErrorCode,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _PublishOutcomeUnknown(OSError):
    pass


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.MALFORMED,
            f"JSON evidence is not finite canonical data: {exc}",
        ) from exc


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_canonical_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.MALFORMED,
            f"evidence artifact is invalid JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.MALFORMED,
            "evidence JSON artifact must be an object",
        )
    if _canonical_json(value) != raw:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.NOT_CANONICAL,
            "evidence JSON artifact is not exact canonical JSON",
        )
    return value


def _normalized_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str):
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INVALID_PATH,
            "evidence path must be a string",
        )
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or relative_path in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in relative_path
    ):
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INVALID_PATH,
            "evidence path must be normalized bundle-relative POSIX syntax",
        )
    return relative_path


def _artifact_path(relative_path: str) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{_normalized_relative_path(relative_path)}"


def _max_bytes_for_path(relative_path: str) -> int:
    if relative_path.startswith(f"{EVIDENCE_ROOT}/artifacts/"):
        return MAX_EVIDENCE_ARTIFACT_BYTES
    return MAX_NON_ARTIFACT_READ_BYTES


@dataclass(frozen=True, slots=True)
class CommissioningEvidenceStore:
    """One exact commissioning session's strict evidence repository."""

    admission_authority: AdmissionAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.admission_authority, AdmissionAuthority):
            raise TypeError("admission_authority must be AdmissionAuthority")
        if self.admission_authority.bundle_kind != BUNDLE_KIND:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "evidence store requires an Active commissioning bundle",
            )

    @classmethod
    def open(
        cls,
        bundle_dir: str | Path,
        *,
        expected_session_id: str,
    ) -> CommissioningEvidenceStore:
        """Open an existing exact session; never create or repair authority."""

        try:
            authority = open_bundle_admission_authority(
                bundle_dir,
                expected_session_id=expected_session_id,
            )
        except (AdmissionArtifactError, BundleError) as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                f"could not open exact commissioning authority: {exc}",
            ) from exc
        if authority.bundle_id != expected_session_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "opened admission authority does not equal the exact session",
            )
        return cls(authority)

    @property
    def bundle_dir(self) -> Path:
        return self.admission_authority.directory

    @property
    def session_id(self) -> str:
        return self.admission_authority.bundle_id

    def _target(self, relative_path: str) -> Path:
        relative = _normalized_relative_path(relative_path)
        try:
            root = self.bundle_dir.resolve(strict=True)
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INVALID_PATH,
                "could not resolve the exact evidence bundle authority",
            ) from exc
        target = root.joinpath(*PurePosixPath(relative).parts)
        try:
            target.parent.resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INVALID_PATH,
                "evidence path escapes its exact bundle authority",
            ) from exc
        return target

    def _prepare_parent(self, parent: Path) -> None:
        try:
            root = self.bundle_dir.resolve(strict=True)
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                "could not resolve the exact evidence bundle authority",
            ) from exc
        try:
            relative = parent.relative_to(root)
        except ValueError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INVALID_PATH,
                "evidence parent escapes its exact bundle authority",
            ) from exc
        current = root
        for part in relative.parts:
            current /= part
            created = False
            try:
                current.mkdir()
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                    f"could not create evidence directory: {exc}",
                ) from exc
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                    f"could not inspect evidence directory: {exc}",
                ) from exc
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.INVALID_PATH,
                    "evidence parents must be real directories",
                )
            try:
                ensure_directory_mode(current)
                fsync_directory(current)
                if created:
                    fsync_directory(current.parent)
            except OSError as exc:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                    f"could not persist evidence directory: {exc}",
                ) from exc

    def _read_path(self, relative_path: str) -> bytes:
        path = self._target(relative_path)
        max_bytes = _max_bytes_for_path(relative_path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.MISSING,
                f"evidence artifact is missing: {relative_path}",
            ) from exc
        except OSError as exc:
            code = (
                CommissioningEvidenceStoreErrorCode.NOT_REGULAR
                if path.is_symlink()
                else CommissioningEvidenceStoreErrorCode.INVALID_PATH
            )
            raise CommissioningEvidenceStoreError(
                code,
                f"could not open evidence artifact: {exc}",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
                    "evidence artifact must be a regular file",
                )
            if metadata.st_size > max_bytes:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.TOO_LARGE,
                    "evidence artifact exceeds its bounded size limit",
                )
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.TOO_LARGE,
                    "evidence artifact exceeds its bounded size limit",
                )
            return raw
        except CommissioningEvidenceStoreError:
            raise
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"could not read evidence artifact: {exc}",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _identity_for_path(self, relative_path: str) -> ArtifactIdentity:
        raw = self._read_path(relative_path)
        return ArtifactIdentity(
            bundle_kind=BUNDLE_KIND,
            bundle_id=self.session_id,
            relative_path=relative_path,
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )

    def identify_artifact(self, relative_path: str) -> ArtifactIdentity:
        """Create an identity only after a strict bounded read of exact bytes."""

        return self._identity_for_path(_normalized_relative_path(relative_path))

    def _read_identity(self, artifact: ArtifactIdentity) -> bytes:
        if not isinstance(artifact, ArtifactIdentity):
            raise TypeError("artifact must be ArtifactIdentity")
        if (
            artifact.bundle_kind != BUNDLE_KIND
            or artifact.bundle_id != self.session_id
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "evidence artifact belongs to another bundle authority",
            )
        if artifact.byte_size > _max_bytes_for_path(artifact.relative_path):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.TOO_LARGE,
                "evidence artifact identity exceeds its bounded size limit",
            )
        raw = self._read_path(artifact.relative_path)
        if (
            len(raw) != artifact.byte_size
            or hashlib.sha256(raw).hexdigest() != artifact.sha256
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "evidence artifact bytes do not match their exact identity",
            )
        return raw

    def reopen_artifact(self, artifact: ArtifactIdentity) -> bytes:
        return self._read_identity(artifact)

    def _verified_payload_identity(
        self,
        relative_path: str,
        payload: bytes,
    ) -> ArtifactIdentity:
        artifact = ArtifactIdentity(
            bundle_kind=BUNDLE_KIND,
            bundle_id=self.session_id,
            relative_path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
        )
        try:
            if self._read_identity(artifact) != payload:
                raise AssertionError("evidence readback changed")
        except (CommissioningEvidenceStoreError, AssertionError) as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN,
                "evidence path changed before exact success readback",
            ) from exc
        return artifact

    def _authoritative_total(self) -> int:
        """Count every strict subtree, including stimuli and admissions."""

        total = 0
        try:
            pending: list[Path] = []
            for relative in (EVIDENCE_ROOT, "stimuli", "admission"):
                root = self.bundle_dir / relative
                if not root.exists():
                    continue
                if root.is_symlink() or not root.is_dir():
                    raise CommissioningEvidenceStoreError(
                        CommissioningEvidenceStoreErrorCode.INVALID_PATH,
                        "authoritative subtree must be a real directory",
                    )
                pending.append(root)
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise CommissioningEvidenceStoreError(
                                CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
                                "authoritative evidence tree contains a symlink",
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        else:
                            raise CommissioningEvidenceStoreError(
                                CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
                                "authoritative evidence tree contains a non-file entry",
                            )
                        if total > MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES:
                            raise CommissioningEvidenceStoreError(
                                CommissioningEvidenceStoreErrorCode.TOTAL_TOO_LARGE,
                                "authoritative evidence exceeds the session byte limit",
                            )
        except CommissioningEvidenceStoreError:
            raise
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                f"could not measure authoritative evidence: {exc}",
            ) from exc
        return total

    def _write_once(self, relative_path: str, payload: bytes) -> ArtifactIdentity:
        if type(payload) is not bytes:
            raise TypeError("evidence payload must be exact bytes")
        if len(payload) > _max_bytes_for_path(relative_path):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.TOO_LARGE,
                "evidence artifact exceeds its bounded size limit",
            )
        path = self._target(relative_path)
        if path.exists() or path.is_symlink():
            try:
                existing = self._read_path(relative_path)
            except CommissioningEvidenceStoreError:
                raise
            if existing != payload:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.PATH_CONFLICT,
                    "write-once evidence path already contains different bytes",
                )
            try:
                fsync_directory(path.parent)
            except OSError as exc:
                raise CommissioningEvidenceStoreError(
                    CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN,
                    "existing evidence could not be confirmed directory-durable",
                ) from exc
            return self._verified_payload_identity(relative_path, payload)

        if self._authoritative_total() + len(payload) > MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.TOTAL_TOO_LARGE,
                "publishing evidence would exceed the session byte limit",
            )
        try:
            free = shutil.disk_usage(self.bundle_dir).free
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                f"could not measure free evidence storage: {exc}",
            ) from exc
        if free - len(payload) < MIN_FREE_SPACE_AFTER_PUBLISH_BYTES:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INSUFFICIENT_SPACE,
                "insufficient free space for a durable evidence publish",
            )
        self._prepare_parent(path.parent)
        try:
            parent_gid = path.parent.stat().st_gid
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                f"could not inspect evidence parent ownership: {exc}",
            ) from exc
        descriptor = -1
        temporary = ""
        published = False
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fchown(stream.fileno(), -1, parent_gid)
                os.fchmod(stream.fileno(), BUNDLE_FILE_MODE)
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self._read_path(relative_path)
                if existing != payload:
                    raise CommissioningEvidenceStoreError(
                        CommissioningEvidenceStoreErrorCode.PATH_CONFLICT,
                        "write-once evidence path raced with different bytes",
                    )
                try:
                    os.unlink(temporary)
                    temporary = ""
                    fsync_directory(path.parent)
                except OSError as exc:
                    raise _PublishOutcomeUnknown(str(exc)) from exc
                return self._verified_payload_identity(relative_path, payload)
            published = True
            try:
                os.unlink(temporary)
                temporary = ""
                fsync_directory(path.parent)
            except OSError as exc:
                raise _PublishOutcomeUnknown(str(exc)) from exc
        except CommissioningEvidenceStoreError:
            raise
        except _PublishOutcomeUnknown as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN,
                "evidence publish outcome is unknown after path publication",
            ) from exc
        except OSError as exc:
            code = (
                CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN
                if published
                else CommissioningEvidenceStoreErrorCode.PERSIST_FAILED
            )
            raise CommissioningEvidenceStoreError(
                code,
                f"could not durably publish evidence artifact: {exc}",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

        return self._verified_payload_identity(relative_path, payload)

    def publish_raw_artifact(
        self,
        relative_path: str,
        payload: bytes,
    ) -> ArtifactIdentity:
        """Publish one raw/analysis input at its single authoritative path."""

        return self._write_once(_artifact_path(relative_path), payload)

    def publish_json_artifact(
        self,
        relative_path: str,
        payload: Mapping[str, Any],
    ) -> ArtifactIdentity:
        """Publish canonical analysis, quality, geometry, or repeatability JSON."""

        if not isinstance(payload, Mapping):
            raise TypeError("JSON evidence payload must be a mapping")
        artifact = self._write_once(
            _artifact_path(relative_path),
            _canonical_json(payload),
        )
        self.reopen_json_artifact(artifact)
        return artifact

    def reopen_json_artifact(self, artifact: ArtifactIdentity) -> dict[str, Any]:
        return _parse_canonical_object(self._read_identity(artifact))
