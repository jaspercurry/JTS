# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bundle artifact sink — bytes/JSON on disk to content-addressed identities.

Thin wrapper over the neutral ``jasper.audio_measurement.bundles`` manifest
primitives that records one PCM / capture / analysis / receipt artifact and
returns its
:class:`~jasper.audio_measurement.evidence_identity.ArtifactIdentity`. The
runner records receipts and PCM/capture artifacts through this seam; the bundle
emitter (:mod:`~jasper.bass_extension.bench.bundle`) only shapes the identities
it returns. This is the only I/O surface in the bench package.
"""

from __future__ import annotations

from pathlib import Path

from jasper.audio_measurement.bundles import (
    record_artifact,
    sha256_file,
    write_json_artifact,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity

BUNDLE_KIND_ARTIFACT = "jts_bass_extension_limiter_bench"
_BUNDLE_SCHEMA_VERSION = 1


class BundleSink:
    """Writes bench artifacts into one bundle directory."""

    def __init__(self, bundle_dir: Path, *, bundle_id: str) -> None:
        self._dir = Path(bundle_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bundle_id = bundle_id

    @property
    def bundle_dir(self) -> Path:
        return self._dir

    @property
    def bundle_id(self) -> str:
        return self._bundle_id

    def _identity(self, relative_path: str, *, sha256: str, byte_size: int) -> ArtifactIdentity:
        return ArtifactIdentity(
            bundle_kind=BUNDLE_KIND_ARTIFACT,
            bundle_id=self._bundle_id,
            relative_path=relative_path,
            sha256=sha256,
            byte_size=byte_size,
        )

    def write_json(self, relative_path: str, payload: dict[str, object], *, kind: str) -> ArtifactIdentity:
        write_json_artifact(
            self._dir,
            relative_path,
            payload,
            kind=kind,
            sensitivity="internal",
            recomputable=False,
            generated_by="jasper-bass-extension-bench",
            bundle_schema_version=_BUNDLE_SCHEMA_VERSION,
        )
        target = self._dir / relative_path
        return self._identity(
            relative_path, sha256=sha256_file(target), byte_size=target.stat().st_size
        )

    def write_bytes(self, relative_path: str, data: bytes, *, kind: str) -> ArtifactIdentity:
        target = self._dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record_artifact(
            self._dir,
            relative_path,
            kind=kind,
            sensitivity="internal",
            recomputable=False,
            generated_by="jasper-bass-extension-bench",
            bundle_schema_version=_BUNDLE_SCHEMA_VERSION,
        )
        return self._identity(
            relative_path, sha256=sha256_file(target), byte_size=target.stat().st_size
        )

    def record_existing(self, path: Path, *, relative_path: str, kind: str) -> ArtifactIdentity:
        """Record an already-written file (e.g. a generated WAV) into the bundle."""

        target = self._dir / relative_path
        if Path(path).resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(path).read_bytes())
        record_artifact(
            self._dir,
            relative_path,
            kind=kind,
            sensitivity="internal",
            recomputable=True,
            generated_by="jasper-bass-extension-bench",
            bundle_schema_version=_BUNDLE_SCHEMA_VERSION,
        )
        return self._identity(
            relative_path, sha256=sha256_file(target), byte_size=target.stat().st_size
        )
