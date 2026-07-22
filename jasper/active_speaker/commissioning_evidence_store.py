# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, bundle-scoped storage for Active commissioning evidence.

This is the authoritative I/O half of :mod:`commissioning_evidence`.  It
reopens the bundle's existing Shared admission authority, publishes immutable
canonical artifacts, and verifies exact bytes on every reopen.  ``info.json``
and the fail-soft forensic manifest are deliberately not evidence authority.

One raw artifact is capped at the existing 5 MiB crossover-capture ceiling.
The total bound covers the proven maximum stereo three-way run: four regions,
each with 3 normal + 3 reverse + (27 coordinates * 5 repeats), plus 3 isolated
captures for each of six physical drivers, or 582 captures at the full raw cap,
plus 1 GiB for generated stimuli and canonical metadata.
This is a hard safety ceiling, not a retention target.  A capture WAV is
published once at its authoritative path; this store creates no manifest or
shadow WAV copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, TypeVar

from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    AdmissionAuthority,
    read_generation_admission,
    read_playback_admission,
)
from jasper.audio_measurement.null_walk import (
    MAX_SCHEDULED_CANDIDATES,
    MIN_CAPTURE_COUNT,
    BoundedNullWalkSchedule,
    NullWalkSpec,
)

from .bundles import (
    BUNDLE_FILE_MODE,
    BUNDLE_KIND,
    DEFAULT_SESSIONS_MAX_BYTES,
    open_bundle_admission_authority,
)
from .commissioning_evidence import (
    STATIONARY_CAPTURE_COUNT,
    AdmittedIsolatedDriverCapture,
    AdmittedRegionCapture,
    CompleteIsolatedDriverEvidence,
    CompleteCommissioningEvidence,
    DelayPointEvidence,
    DelayWalkEvidence,
    IsolatedDriverEvidence,
    RegionCommissioningEvidence,
    RegionEvidencePlan,
    StationaryRegionEvidence,
)
from .commissioning_run import CommissioningRunHandle
from .profile import ADJACENT_PAIRS_BY_WAY, DRIVER_ROLES_BY_WAY, SIDES_BY_LAYOUT
from .test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

EVIDENCE_ROOT = "evidence/v1"
MAX_EVIDENCE_ARTIFACT_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES
MAX_TYPED_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_COMMISSIONING_REGIONS = max(
    len(sides) * len(regions)
    for sides in SIDES_BY_LAYOUT.values()
    for regions in ADJACENT_PAIRS_BY_WAY.values()
)
MAX_SUMMED_CAPTURE_ARTIFACT_COUNT = MAX_COMMISSIONING_REGIONS * (
    (2 * STATIONARY_CAPTURE_COUNT)
    + (MAX_SCHEDULED_CANDIDATES * MIN_CAPTURE_COUNT)
)
MAX_ISOLATED_DRIVER_TARGETS = max(
    len(sides) * len(roles)
    for sides in SIDES_BY_LAYOUT.values()
    for roles in DRIVER_ROLES_BY_WAY.values()
)
MAX_ISOLATED_CAPTURE_ARTIFACT_COUNT = (
    MAX_ISOLATED_DRIVER_TARGETS * STATIONARY_CAPTURE_COUNT
)
MAX_CAPTURE_ARTIFACT_COUNT = (
    MAX_SUMMED_CAPTURE_ARTIFACT_COUNT + MAX_ISOLATED_CAPTURE_ARTIFACT_COUNT
)
MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES = (
    MAX_CAPTURE_ARTIFACT_COUNT * MAX_EVIDENCE_ARTIFACT_BYTES
) + (1024 * 1024 * 1024)
# Keep at least one ordinary Active retention budget free. Open/current bundles
# are intentionally retention-protected, so retention alone cannot rescue a
# full filesystem while an authoritative run is growing.
MIN_FREE_SPACE_AFTER_PUBLISH_BYTES = DEFAULT_SESSIONS_MAX_BYTES

_ATTEMPT_CAPTURE_NAME_RE = re.compile(r"([0-9]{4})\.json")
_ATTEMPT_TEMP_NAME_RE = re.compile(
    r"\.[0-9]{4}\.json\.[A-Za-z0-9_-]+\.tmp"
)
_T = TypeVar("_T")


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


def _component_key(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INVALID_PATH,
            f"{field_name} must be a non-empty trimmed string",
        )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _artifact_path(relative_path: str) -> str:
    return f"{EVIDENCE_ROOT}/artifacts/{_normalized_relative_path(relative_path)}"


def _require_evidence_artifact(artifact: ArtifactIdentity, *, role: str) -> None:
    prefix = f"{EVIDENCE_ROOT}/artifacts/"
    if not artifact.relative_path.startswith(prefix):
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
            f"{role} must occupy the strict evidence artifact namespace",
        )


def _require_stimulus_path(
    artifact: ArtifactIdentity,
    *,
    admission_id: str,
) -> None:
    if artifact.relative_path != f"stimuli/{admission_id}.wav":
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
            "stimulus must occupy its exact one-shot admission path",
        )


def _require_identity_path(artifact: ArtifactIdentity, expected: str) -> None:
    if artifact.relative_path != expected:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
            "typed evidence identity does not occupy its canonical path",
        )


def _run_root(run_id: str) -> str:
    run = _component_key(run_id, field_name="run_id")
    return f"{EVIDENCE_ROOT}/runs/{run}"


def _generation_root(run: CommissioningRunHandle) -> str:
    if not isinstance(run, CommissioningRunHandle):
        raise TypeError("run must be CommissioningRunHandle")
    return f"{_run_root(run.run_id)}/generations/{run.owner_generation}"


def plan_relative_path(run: CommissioningRunHandle) -> str:
    return f"{_generation_root(run)}/plan.json"


def attempt_capture_relative_path(attempt_id: str, ordinal: int) -> str:
    if type(ordinal) is not int or not 0 <= ordinal <= 9999:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INVALID_PATH,
            "capture ordinal must be an integer from 0 through 9999",
        )
    attempt = _component_key(attempt_id, field_name="attempt_id")
    return f"{EVIDENCE_ROOT}/attempts/{attempt}/captures/{ordinal:04d}.json"


def isolated_attempt_capture_relative_path(attempt_id: str, ordinal: int) -> str:
    if type(ordinal) is not int or not 0 <= ordinal <= 9999:
        raise CommissioningEvidenceStoreError(
            CommissioningEvidenceStoreErrorCode.INVALID_PATH,
            "capture ordinal must be an integer from 0 through 9999",
        )
    attempt = _component_key(attempt_id, field_name="attempt_id")
    return f"{EVIDENCE_ROOT}/isolated-attempts/{attempt}/captures/{ordinal:04d}.json"


def stationary_relative_path(attempt_id: str) -> str:
    attempt = _component_key(attempt_id, field_name="attempt_id")
    return f"{EVIDENCE_ROOT}/attempts/{attempt}/stationary.json"


def delay_point_relative_path(attempt_id: str) -> str:
    attempt = _component_key(attempt_id, field_name="attempt_id")
    return f"{EVIDENCE_ROOT}/attempts/{attempt}/delay-point.json"


def _region_root(speaker_group_id: str, region_id: str) -> str:
    group = _component_key(speaker_group_id, field_name="speaker_group_id")
    region = _component_key(region_id, field_name="region_id")
    return f"regions/{group}/{region}"


def schedule_relative_path(
    run: CommissioningRunHandle,
    speaker_group_id: str,
    region_id: str,
) -> str:
    return (
        f"{_generation_root(run)}/{_region_root(speaker_group_id, region_id)}"
        "/delay-schedule.json"
    )


def delay_walk_relative_path(
    run: CommissioningRunHandle,
    speaker_group_id: str,
    region_id: str,
) -> str:
    return (
        f"{_generation_root(run)}/{_region_root(speaker_group_id, region_id)}"
        "/delay-walk.json"
    )


def region_relative_path(
    run: CommissioningRunHandle,
    speaker_group_id: str,
    region_id: str,
) -> str:
    return (
        f"{_generation_root(run)}/{_region_root(speaker_group_id, region_id)}"
        "/region-evidence.json"
    )


def complete_relative_path(run_id: str) -> str:
    return f"{_run_root(run_id)}/complete.json"


def isolated_driver_evidence_relative_path(run_id: str) -> str:
    return f"{_run_root(run_id)}/isolated-driver-evidence.json"


def isolated_driver_relative_path(
    run: CommissioningRunHandle,
    speaker_group_id: str,
    role: str,
) -> str:
    group = _component_key(speaker_group_id, field_name="speaker_group_id")
    driver_role = _component_key(role, field_name="role")
    return (
        f"{_generation_root(run)}/drivers/{group}/{driver_role}/"
        "isolated-driver-evidence.json"
    )


def _max_bytes_for_path(relative_path: str) -> int:
    if relative_path.startswith(f"{EVIDENCE_ROOT}/artifacts/"):
        return MAX_EVIDENCE_ARTIFACT_BYTES
    return MAX_TYPED_EVIDENCE_BYTES


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(slots=True)
class _ReadBudget:
    byte_limit: int = MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES
    byte_count: int = 0
    _seen: set[str] = field(default_factory=set)

    def consume(self, artifact: ArtifactIdentity) -> None:
        if artifact.fingerprint in self._seen:
            return
        total = self.byte_count + artifact.byte_size
        if total > self.byte_limit:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.TOTAL_TOO_LARGE,
                "authoritative evidence exceeds the bounded deep-read budget",
            )
        self._seen.add(artifact.fingerprint)
        self.byte_count = total


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
                os.chmod(current, 0o750)
                _fsync_directory(current)
                if created:
                    _fsync_directory(current.parent)
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

    def _read_identity(
        self,
        artifact: ArtifactIdentity,
        *,
        budget: _ReadBudget | None = None,
    ) -> bytes:
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
        if budget is not None:
            budget.consume(artifact)
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
                _fsync_directory(path.parent)
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
                    _fsync_directory(path.parent)
                except OSError as exc:
                    raise _PublishOutcomeUnknown(str(exc)) from exc
                return self._verified_payload_identity(relative_path, payload)
            published = True
            try:
                os.unlink(temporary)
                temporary = ""
                _fsync_directory(path.parent)
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

    def _assert_session(self, value: Any) -> None:
        authority = getattr(value, "authority", None)
        if authority is None:
            plan = getattr(value, "plan", None)
            authority = getattr(plan, "authority", None)
        if (
            authority is None
            or authority.commissioning_session_id != self.session_id
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "typed evidence does not belong to this exact session",
            )

    def _publish_typed(
        self,
        relative_path: str,
        value: _T,
        parser: Callable[[Any], _T],
        *,
        verify: Callable[[_T, _ReadBudget], None] | None = None,
    ) -> ArtifactIdentity:
        if verify is not None:
            verify(value, _ReadBudget())
        payload = _canonical_json(value.to_dict())  # type: ignore[attr-defined]
        artifact = self._write_once(relative_path, payload)
        try:
            reopened = self._reopen_typed(artifact, parser)
            if reopened != value:
                raise ValueError("typed evidence readback changed")
            if verify is not None:
                verify(reopened, _ReadBudget())
        except (CommissioningEvidenceStoreError, ValueError, TypeError) as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN,
                "published typed evidence could not be reopened exactly",
            ) from exc
        return artifact

    def _reopen_typed(
        self,
        artifact: ArtifactIdentity,
        parser: Callable[[Any], _T],
    ) -> _T:
        raw = self._read_identity(artifact)
        value = _parse_canonical_object(raw)
        try:
            return parser(value)
        except (ValueError, TypeError) as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.MALFORMED,
                f"typed evidence is invalid: {exc}",
            ) from exc

    def publish_region_evidence_plan(
        self,
        plan: RegionEvidencePlan,
    ) -> ArtifactIdentity:
        self._assert_session(plan)
        return self._publish_typed(
            plan_relative_path(plan.authority.run),
            plan,
            RegionEvidencePlan.from_mapping,
        )

    def reopen_region_evidence_plan(
        self,
        *,
        run: CommissioningRunHandle,
        artifact: ArtifactIdentity | None = None,
    ) -> RegionEvidencePlan:
        expected_path = plan_relative_path(run)
        identity = artifact or self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        result = self._reopen_typed(identity, RegionEvidencePlan.from_mapping)
        self._assert_session(result)
        if result.authority.run != run:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "plan path does not match the exact run owner generation",
            )
        return result

    def publish_admitted_region_capture(
        self,
        capture: AdmittedRegionCapture,
        *,
        ordinal: int,
    ) -> ArtifactIdentity:
        self._assert_session(capture)
        return self._publish_typed(
            attempt_capture_relative_path(capture.attempt_id, ordinal),
            capture,
            AdmittedRegionCapture.from_mapping,
            verify=self._verify_capture,
        )

    def reopen_admitted_region_capture(
        self,
        artifact: ArtifactIdentity,
    ) -> AdmittedRegionCapture:
        result = self._reopen_typed(artifact, AdmittedRegionCapture.from_mapping)
        self._assert_session(result)
        prefix = (
            f"{EVIDENCE_ROOT}/attempts/"
            f"{_component_key(result.attempt_id, field_name='attempt_id')}/captures/"
        )
        suffix = artifact.relative_path.removeprefix(prefix)
        match = _ATTEMPT_CAPTURE_NAME_RE.fullmatch(suffix)
        if not artifact.relative_path.startswith(prefix) or match is None:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "capture evidence does not occupy its exact attempt namespace",
            )
        _require_identity_path(
            artifact,
            attempt_capture_relative_path(result.attempt_id, int(match.group(1))),
        )
        self._verify_capture(result, _ReadBudget())
        return result

    def reopen_attempt_capture(
        self,
        attempt_id: str,
        ordinal: int,
    ) -> AdmittedRegionCapture:
        relative = attempt_capture_relative_path(attempt_id, ordinal)
        result = self.reopen_admitted_region_capture(self._identity_for_path(relative))
        if result.attempt_id != attempt_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "attempt capture path does not match its typed attempt",
            )
        return result

    def reopen_attempt_captures(
        self,
        attempt_id: str,
    ) -> tuple[AdmittedRegionCapture, ...]:
        ordinals = self._attempt_capture_ordinals(
            attempt_capture_relative_path(attempt_id, 0)
        )
        return tuple(self.reopen_attempt_capture(attempt_id, item) for item in ordinals)

    def _attempt_capture_ordinals(self, first_relative_path: str) -> tuple[int, ...]:
        first = Path(first_relative_path)
        directory = self._target(first.parent.as_posix())
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"could not inspect attempt capture collection: {exc}",
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
                "attempt capture collection must be a real directory",
            )
        ordinals: list[int] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    match = _ATTEMPT_CAPTURE_NAME_RE.fullmatch(entry.name)
                    child_metadata = entry.stat(follow_symlinks=False)
                    if (
                        _ATTEMPT_TEMP_NAME_RE.fullmatch(entry.name) is not None
                        and stat.S_ISREG(child_metadata.st_mode)
                    ):
                        continue
                    if match is None or not stat.S_ISREG(child_metadata.st_mode):
                        raise CommissioningEvidenceStoreError(
                            CommissioningEvidenceStoreErrorCode.NOT_REGULAR,
                            "attempt capture collection contains an unexpected entry",
                        )
                    ordinals.append(int(match.group(1)))
        except CommissioningEvidenceStoreError:
            raise
        except OSError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"could not read attempt capture collection: {exc}",
            ) from exc
        ordinals.sort()
        if ordinals != list(range(len(ordinals))):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "attempt capture ordinals must be contiguous from zero",
            )
        return tuple(ordinals)

    def publish_admitted_isolated_driver_capture(
        self,
        capture: AdmittedIsolatedDriverCapture,
        *,
        ordinal: int,
    ) -> ArtifactIdentity:
        """Publish one resumable strict isolated capture for its attempt."""

        self._assert_session(capture)
        return self._publish_typed(
            isolated_attempt_capture_relative_path(
                capture.attempt.attempt_id,
                ordinal,
            ),
            capture,
            AdmittedIsolatedDriverCapture.from_mapping,
            verify=self._verify_isolated_capture,
        )

    def reopen_admitted_isolated_driver_capture(
        self,
        artifact: ArtifactIdentity,
    ) -> AdmittedIsolatedDriverCapture:
        result = self._reopen_typed(
            artifact,
            AdmittedIsolatedDriverCapture.from_mapping,
        )
        self._assert_session(result)
        attempt_id = result.attempt.attempt_id
        prefix = (
            f"{EVIDENCE_ROOT}/isolated-attempts/"
            f"{_component_key(attempt_id, field_name='attempt_id')}/captures/"
        )
        suffix = artifact.relative_path.removeprefix(prefix)
        match = _ATTEMPT_CAPTURE_NAME_RE.fullmatch(suffix)
        if not artifact.relative_path.startswith(prefix) or match is None:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "isolated capture does not occupy its exact attempt namespace",
            )
        _require_identity_path(
            artifact,
            isolated_attempt_capture_relative_path(
                attempt_id,
                int(match.group(1)),
            ),
        )
        self._verify_isolated_capture(result, _ReadBudget())
        return result

    def reopen_isolated_attempt_capture(
        self,
        attempt_id: str,
        ordinal: int,
    ) -> AdmittedIsolatedDriverCapture:
        relative = isolated_attempt_capture_relative_path(attempt_id, ordinal)
        result = self.reopen_admitted_isolated_driver_capture(
            self._identity_for_path(relative)
        )
        if result.attempt.attempt_id != attempt_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "isolated attempt path does not match its typed attempt",
            )
        return result

    def reopen_isolated_attempt_captures(
        self,
        attempt_id: str,
    ) -> tuple[AdmittedIsolatedDriverCapture, ...]:
        ordinals = self._attempt_capture_ordinals(
            isolated_attempt_capture_relative_path(attempt_id, 0)
        )
        return tuple(
            self.reopen_isolated_attempt_capture(attempt_id, item) for item in ordinals
        )

    def isolated_attempt_capture_count(self, attempt_id: str) -> int:
        """Count contiguous write-once capture records without reading WAVs."""

        return len(
            self._attempt_capture_ordinals(
                isolated_attempt_capture_relative_path(attempt_id, 0)
            )
        )

    def publish_isolated_driver_evidence(
        self,
        evidence: IsolatedDriverEvidence,
    ) -> ArtifactIdentity:
        """Publish one completed physical-driver set for resumable assembly."""

        self._assert_session(evidence)
        return self._publish_typed(
            isolated_driver_relative_path(
                evidence.authority.run,
                evidence.speaker_group_id,
                evidence.role,
            ),
            evidence,
            IsolatedDriverEvidence.from_mapping,
            verify=self._verify_isolated_driver_evidence,
        )

    def reopen_isolated_driver_evidence(
        self,
        *,
        run: CommissioningRunHandle,
        speaker_group_id: str,
        role: str,
        artifact: ArtifactIdentity | None = None,
    ) -> IsolatedDriverEvidence:
        expected_path = isolated_driver_relative_path(
            run,
            speaker_group_id,
            role,
        )
        identity = artifact or self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        result = self._reopen_typed(identity, IsolatedDriverEvidence.from_mapping)
        self._assert_session(result)
        if (
            result.authority.run != run
            or result.speaker_group_id != speaker_group_id
            or result.role != role
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "isolated driver evidence path does not match its typed target",
            )
        self._verify_isolated_driver_evidence(result, _ReadBudget())
        return result

    def isolated_driver_evidence_is_published(
        self,
        *,
        run: CommissioningRunHandle,
        speaker_group_id: str,
        role: str,
    ) -> bool:
        """Validate the small status anchor without rereading child WAVs."""

        expected_path = isolated_driver_relative_path(
            run,
            speaker_group_id,
            role,
        )
        identity = self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        result = self._reopen_typed(identity, IsolatedDriverEvidence.from_mapping)
        self._assert_session(result)
        if (
            result.authority.run != run
            or result.speaker_group_id != speaker_group_id
            or result.role != role
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "isolated driver status anchor does not match its typed target",
            )
        return True

    def publish_stationary_region_evidence(
        self,
        evidence: StationaryRegionEvidence,
    ) -> ArtifactIdentity:
        self._assert_session(evidence)
        return self._publish_typed(
            stationary_relative_path(evidence.attempt.attempt_id),
            evidence,
            StationaryRegionEvidence.from_mapping,
            verify=self._verify_stationary,
        )

    def reopen_stationary_region_evidence(
        self,
        artifact: ArtifactIdentity,
    ) -> StationaryRegionEvidence:
        result = self._reopen_typed(artifact, StationaryRegionEvidence.from_mapping)
        self._assert_session(result)
        _require_identity_path(artifact, stationary_relative_path(result.attempt.attempt_id))
        self._verify_stationary(result, _ReadBudget())
        return result

    def publish_delay_point_evidence(
        self,
        evidence: DelayPointEvidence,
    ) -> ArtifactIdentity:
        self._assert_session(evidence)
        return self._publish_typed(
            delay_point_relative_path(evidence.attempt.attempt_id),
            evidence,
            DelayPointEvidence.from_mapping,
            verify=self._verify_delay_point,
        )

    def reopen_delay_point_evidence(
        self,
        artifact: ArtifactIdentity,
    ) -> DelayPointEvidence:
        result = self._reopen_typed(artifact, DelayPointEvidence.from_mapping)
        self._assert_session(result)
        _require_identity_path(artifact, delay_point_relative_path(result.attempt.attempt_id))
        self._verify_delay_point(result, _ReadBudget())
        return result

    def publish_bounded_null_walk_schedule(
        self,
        schedule: BoundedNullWalkSchedule,
        *,
        spec: NullWalkSpec,
        run: CommissioningRunHandle,
        speaker_group_id: str,
        region_id: str,
    ) -> ArtifactIdentity:
        if run.session_id != self.session_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "bounded schedule run does not belong to this exact session",
            )
        if schedule.spec_fingerprint != spec.fingerprint:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "bounded schedule does not belong to the supplied exact spec",
            )
        parser = lambda raw: BoundedNullWalkSchedule.from_mapping(raw, spec=spec)
        return self._publish_typed(
            schedule_relative_path(run, speaker_group_id, region_id),
            schedule,
            parser,
        )

    def reopen_bounded_null_walk_schedule(
        self,
        *,
        spec: NullWalkSpec,
        run: CommissioningRunHandle,
        speaker_group_id: str,
        region_id: str,
        artifact: ArtifactIdentity | None = None,
    ) -> BoundedNullWalkSchedule:
        if run.session_id != self.session_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY,
                "bounded schedule run does not belong to this exact session",
            )
        expected_path = schedule_relative_path(run, speaker_group_id, region_id)
        identity = artifact or self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        return self._reopen_typed(
            identity,
            lambda raw: BoundedNullWalkSchedule.from_mapping(raw, spec=spec),
        )

    def publish_delay_walk_evidence(
        self,
        evidence: DelayWalkEvidence,
    ) -> ArtifactIdentity:
        self._assert_session(evidence)
        return self._publish_typed(
            delay_walk_relative_path(
                evidence.authority.run,
                evidence.speaker_group_id,
                evidence.region_id,
            ),
            evidence,
            DelayWalkEvidence.from_mapping,
            verify=self._verify_delay_walk,
        )

    def reopen_delay_walk_evidence(
        self,
        artifact: ArtifactIdentity,
    ) -> DelayWalkEvidence:
        result = self._reopen_typed(artifact, DelayWalkEvidence.from_mapping)
        self._assert_session(result)
        _require_identity_path(
            artifact,
            delay_walk_relative_path(
                result.authority.run,
                result.speaker_group_id,
                result.region_id,
            ),
        )
        self._verify_delay_walk(result, _ReadBudget())
        return result

    def publish_region_commissioning_evidence(
        self,
        evidence: RegionCommissioningEvidence,
    ) -> ArtifactIdentity:
        self._assert_session(evidence)
        return self._publish_typed(
            region_relative_path(
                evidence.plan.authority.run,
                evidence.target.speaker_group_id,
                evidence.target.region_id,
            ),
            evidence,
            RegionCommissioningEvidence.from_mapping,
            verify=self._verify_region,
        )

    def reopen_region_commissioning_evidence(
        self,
        artifact: ArtifactIdentity,
    ) -> RegionCommissioningEvidence:
        result = self._reopen_typed(artifact, RegionCommissioningEvidence.from_mapping)
        self._assert_session(result)
        _require_identity_path(
            artifact,
            region_relative_path(
                result.plan.authority.run,
                result.target.speaker_group_id,
                result.target.region_id,
            ),
        )
        self._verify_region(result, _ReadBudget())
        return result

    def publish_complete_commissioning_evidence(
        self,
        evidence: CompleteCommissioningEvidence,
    ) -> ArtifactIdentity:
        self._assert_session(evidence)
        return self._publish_typed(
            complete_relative_path(evidence.plan.authority.run.run_id),
            evidence,
            CompleteCommissioningEvidence.from_mapping,
            verify=self._verify_complete,
        )

    def reopen_complete_commissioning_evidence(
        self,
        *,
        run_id: str,
        artifact: ArtifactIdentity | None = None,
    ) -> CompleteCommissioningEvidence:
        result = self.reopen_complete_commissioning_evidence_anchor(
            run_id=run_id,
            artifact=artifact,
        )
        self._verify_complete(result, _ReadBudget())
        return result

    def reopen_complete_commissioning_evidence_anchor(
        self,
        *,
        run_id: str,
        artifact: ArtifactIdentity | None = None,
    ) -> CompleteCommissioningEvidence:
        """Reopen the typed complete anchor without rereading child WAVs."""

        expected_path = complete_relative_path(run_id)
        identity = artifact or self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        result = self._reopen_typed(
            identity,
            CompleteCommissioningEvidence.from_mapping,
        )
        self._assert_session(result)
        if result.plan.authority.run.run_id != run_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "complete evidence path does not match its exact durable run",
            )
        return result

    def publish_complete_isolated_driver_evidence(
        self,
        evidence: CompleteIsolatedDriverEvidence,
    ) -> ArtifactIdentity:
        """Publish the one write-once isolated-driver set for this exact run."""

        self._assert_session(evidence)
        return self._publish_typed(
            isolated_driver_evidence_relative_path(
                evidence.plan.authority.run.run_id
            ),
            evidence,
            CompleteIsolatedDriverEvidence.from_mapping,
            verify=self._verify_complete_isolated_driver_evidence,
        )

    def reopen_complete_isolated_driver_evidence(
        self,
        *,
        run_id: str,
        artifact: ArtifactIdentity | None = None,
    ) -> CompleteIsolatedDriverEvidence:
        """Reopen one exact run-scoped set and every child artifact."""

        result = self.reopen_complete_isolated_driver_evidence_anchor(
            run_id=run_id,
            artifact=artifact,
        )
        self._verify_complete_isolated_driver_evidence(result, _ReadBudget())
        return result

    def reopen_complete_isolated_driver_evidence_anchor(
        self,
        *,
        run_id: str,
        artifact: ArtifactIdentity | None = None,
    ) -> CompleteIsolatedDriverEvidence:
        """Reopen the typed isolated anchor without rereading child WAVs."""

        expected_path = isolated_driver_evidence_relative_path(run_id)
        identity = artifact or self._identity_for_path(expected_path)
        _require_identity_path(identity, expected_path)
        result = self._reopen_typed(
            identity,
            CompleteIsolatedDriverEvidence.from_mapping,
        )
        self._assert_session(result)
        if result.plan.authority.run.run_id != run_id:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                "isolated evidence path does not match its exact durable run",
            )
        return result

    def complete_isolated_driver_evidence_fingerprint(
        self,
        *,
        run_id: str,
    ) -> str:
        """Read the typed status anchor without rereading every child WAV."""

        return self.reopen_complete_isolated_driver_evidence_anchor(
            run_id=run_id
        ).fingerprint

    def _verify_capture(
        self,
        capture: AdmittedRegionCapture,
        budget: _ReadBudget,
    ) -> None:
        self._verify_admitted_capture_bytes(capture, budget, label="region")

    def _verify_isolated_capture(
        self,
        capture: AdmittedIsolatedDriverCapture,
        budget: _ReadBudget,
    ) -> None:
        self._verify_admitted_capture_bytes(capture, budget, label="isolated")

    def _verify_admitted_capture_bytes(
        self,
        capture: AdmittedRegionCapture | AdmittedIsolatedDriverCapture,
        budget: _ReadBudget,
        *,
        label: str,
    ) -> None:
        for role, artifact in (
            (f"raw {label} capture", capture.capture.raw_artifact),
            (f"{label} analysis input", capture.capture.analysis_input_artifact),
            (f"{label} quality evidence", capture.capture.quality_artifact),
        ):
            _require_evidence_artifact(artifact, role=role)
        _require_stimulus_path(
            capture.stimulus.artifact,
            admission_id=capture.admission_id,
        )
        for artifact in (
            capture.capture.raw_artifact,
            capture.capture.analysis_input_artifact,
            capture.capture.quality_artifact,
            capture.capture.admission_artifact,
            capture.stimulus.artifact,
            capture.generation_artifact,
            capture.playback_artifact,
        ):
            self._read_identity(artifact, budget=budget)
        try:
            generation = read_generation_admission(
                self.admission_authority,
                capture.generation_artifact,
            )
        except AdmissionArtifactError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"{label} generation admission is not authoritative: {exc}",
            ) from exc
        if (
            generation.admission_id != capture.admission_id
            or generation.admission != capture.generation_admission
        ):
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"generation admission changed from {label} typed evidence",
            )
        try:
            playback = read_playback_admission(
                self.admission_authority,
                generation,
                capture.playback_artifact,
            )
        except AdmissionArtifactError as exc:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"{label} playback admission is not authoritative: {exc}",
            ) from exc
        if playback.admission != capture.playback_admission:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH,
                f"playback admission changed from {label} typed evidence",
            )

    def _verify_stationary(
        self,
        evidence: StationaryRegionEvidence,
        budget: _ReadBudget,
    ) -> None:
        for capture in evidence.captures:
            self._verify_capture(capture, budget)

    def _verify_delay_point(
        self,
        evidence: DelayPointEvidence,
        budget: _ReadBudget,
    ) -> None:
        for capture in evidence.captures:
            self._verify_capture(capture, budget)

    def _verify_delay_walk(
        self,
        evidence: DelayWalkEvidence,
        budget: _ReadBudget,
    ) -> None:
        _require_evidence_artifact(
            evidence.geometry_attestation.attestation_artifact,
            role="geometry attestation",
        )
        _require_evidence_artifact(
            evidence.repeatability_artifact,
            role="repeatability evidence",
        )
        self._read_identity(
            evidence.geometry_attestation.attestation_artifact,
            budget=budget,
        )
        self._read_identity(evidence.repeatability_artifact, budget=budget)
        for point in evidence.points:
            self._verify_delay_point(point, budget)

    def _verify_region(
        self,
        evidence: RegionCommissioningEvidence,
        budget: _ReadBudget,
    ) -> None:
        self._verify_stationary(evidence.normal, budget)
        self._verify_stationary(evidence.reverse, budget)
        self._verify_delay_walk(evidence.delay_walk, budget)

    def _verify_complete(
        self,
        evidence: CompleteCommissioningEvidence,
        budget: _ReadBudget,
    ) -> None:
        for region in evidence.regions:
            self._verify_region(region, budget)

    def _verify_complete_isolated_driver_evidence(
        self,
        evidence: CompleteIsolatedDriverEvidence,
        budget: _ReadBudget,
    ) -> None:
        for driver in evidence.drivers:
            self._verify_isolated_driver_evidence(driver, budget)

    def _verify_isolated_driver_evidence(
        self,
        evidence: IsolatedDriverEvidence,
        budget: _ReadBudget,
    ) -> None:
        _require_evidence_artifact(
            evidence.repeatability_artifact,
            role="isolated repeatability evidence",
        )
        self._read_identity(evidence.repeatability_artifact, budget=budget)
        for capture in evidence.captures:
            self._verify_isolated_capture(capture, budget)

    def verify_complete(
        self,
        evidence: CompleteCommissioningEvidence,
    ) -> CompleteCommissioningEvidence:
        """Strictly verify every child byte and both admission boundaries."""

        self._assert_session(evidence)
        self._verify_complete(evidence, _ReadBudget())
        return evidence

    def verify_complete_isolated_driver_evidence(
        self,
        evidence: CompleteIsolatedDriverEvidence,
    ) -> CompleteIsolatedDriverEvidence:
        """Strictly verify the complete isolated set and every child byte."""

        self._assert_session(evidence)
        self._verify_complete_isolated_driver_evidence(evidence, _ReadBudget())
        return evidence
