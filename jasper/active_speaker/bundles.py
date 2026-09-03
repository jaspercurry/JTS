# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker commissioning bundle: durable, append-only session evidence.

The append-only half of the measurement flow, whose latest-wins pointer state
(``measurement.py``) overwrites itself on every capture: one durable, hashed,
retention-bounded directory per commissioning attempt. Manifest primitives come
from ``jasper.audio_measurement.bundles``; only the active-speaker shape (its
fields, retention policy and core-artifact list) lives here.

Two invariants keep ownership explicit:

- **Split authority.** Capture, proposal and apply payloads in ``info.json`` are
  a fail-soft forensic mirror, never reconstructed into measurement or candidate
  authority. A FRESH bundle directory additionally owns Shared's exact admission
  marker; a missing or historical marker refuses audible production work.
  Nothing in ``measurement.py`` imports this module.
- **Fail-soft forensic writes.** Every public write entry point catches
  ``OSError``/``BundleError``/``ValueError``, logs one
  ``active_speaker.bundle_write_failed`` WARNING, and returns ``None``. Failure
  to create or reopen the admission authority is different: the flow may
  continue as non-admitted diagnostic evidence, but no production playback or
  positive authority may be minted from it.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_measurement.bundles import (
    BundleError,
    read_artifact_manifest,
    record_artifact,
    sha256_file,
    write_json_artifact,
)
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    AdmissionAuthority,
    create_admission_authority,
    open_admission_authority,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from . import measurement as _measurement
from .capture_geometry import DRIVER_PLACEMENT_POLICY_ID
from .test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_KIND = "jts_active_speaker_commissioning_bundle"

# Before the manifest primitive moved out of Room, an Active partial-bundle
# write with no info.json inherited Room's fallback schema header. Preserve
# those bytes until partial-bundle writes always seed info.json; ordinary
# Active bundles continue to resolve schema 1 from their owning info.json.
LEGACY_PARTIAL_BUNDLE_SCHEMA_VERSION = 5

DEFAULT_SESSIONS_DIR = Path("/var/lib/jasper/active_speaker/sessions")
SESSIONS_DIR_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_DIR"

# Aliased, not mirrored, from the owner
# test_signal_plan.CROSSOVER_CAPTURE_MAX_WAV_BYTES. A bundle copy is never
# larger than the capture it was made from, so one ceiling bounds both.
MAX_CAPTURE_WAV_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES

# Retention ceiling for the commissioning-bundle store. Sized so twelve cloud
# sessions fit: a run retains one ~1-2 MiB capture WAV per prompted position,
# around 30 MB. Full per-position WAVs are kept rather than derived summaries —
# see docs/historical/linearization-campaign-2026-07.md.
#
# A RETENTION budget only; the publish-time free-space precondition is
# ``commissioning_evidence_store.MIN_FREE_SPACE_AFTER_PUBLISH_BYTES``.
DEFAULT_SESSIONS_MAX_BYTES = 1024 * 1024 * 1024
SESSIONS_MAX_BYTES_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BYTES"
DEFAULT_SESSIONS_MAX_BUNDLES = 12
SESSIONS_MAX_BUNDLES_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES"

# Mirrors web_measurement.CAPTURE_FILE_MODE. Bundle directories and capture
# subdirectories are explicitly chmod'd 0o750 (umask-proof; group keeps
# traverse/read under the /var/lib/jasper group model) in open_bundle() and
# _copy_wav_into_bundle(). Files stay at this mode.
BUNDLE_FILE_MODE = 0o640

#: One capture entry's kind: ``driver`` is one driver alone, ``summed`` every
#: driver at once, ``sequential`` every driver in turn inside ONE recording (the
#: CHECK and MEASURE programs). Records banked before ``sequential`` existed
#: read as ``summed``, disambiguated by their ``phase``.
#:
#: A ``sequential`` capture keeps ``summed``'s ``summed/`` subdirectory and
#: ``summed_captures`` list: the kind names what was PLAYED, not where the bytes
#: land, and an opened bundle's layout is write-once.
CAPTURE_KIND_SEQUENTIAL = "sequential"
_CAPTURE_KINDS = frozenset({"driver", "summed", CAPTURE_KIND_SEQUENTIAL})

_VALID_STATES = frozenset({"open", "proposal_ready", "applied", "failed", "abandoned"})
_UNFINISHED_STATES = frozenset({"open", "proposal_ready"})

_BUILD_MANIFEST_PATH = Path("/var/lib/jasper/build.txt")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _default_sessions_dir() -> Path:
    return Path(os.environ.get(SESSIONS_DIR_ENV) or DEFAULT_SESSIONS_DIR)


def sessions_dir() -> Path:
    """Return the active-speaker commissioning-bundle storage root."""

    return _default_sessions_dir()


def _sessions_max_bytes() -> int:
    return _env_int(SESSIONS_MAX_BYTES_ENV, DEFAULT_SESSIONS_MAX_BYTES)


def _sessions_max_bundles() -> int:
    return _env_int(SESSIONS_MAX_BUNDLES_ENV, DEFAULT_SESSIONS_MAX_BUNDLES)


def _fail_soft(op: str):
    """Wrap a public write entry point in the module's fail-soft contract.

    Catches ``OSError`` / ``BundleError`` (plus a stray ``ValueError``, e.g.
    from malformed JSON already on disk) and logs
    ``active_speaker.bundle_write_failed`` at WARNING instead of propagating.
    The session id for the log line is read from the wrapped function's
    ``bundle_dir`` argument (by convention the first positional/keyword
    parameter of every wrapped function except :func:`open_bundle`, which has
    no bundle yet when it can fail) via its directory basename.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (OSError, BundleError, AdmissionArtifactError, ValueError) as exc:
                bundle_dir = kwargs.get("bundle_dir")
                if bundle_dir is None and args and isinstance(args[0], Path):
                    bundle_dir = args[0]
                session_id = bundle_dir.name if isinstance(bundle_dir, Path) else None
                log_event(
                    logger,
                    "active_speaker.bundle_write_failed",
                    level=logging.WARNING,
                    session=session_id,
                    op=op,
                    error=str(exc),
                )
                return None

        return wrapper

    return decorator


def _safe_slug(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    out = "_".join(part for part in out.split("_") if part)
    return out[:64] or fallback


def capture_artifact_relpath(kind: str, group: Any, role: Any) -> str:
    """Deterministic bundle-relative WAV path for one driver/summed capture.

    Minted BEFORE the measurement write so the same relative path can be
    embedded as the record's ``bundle_ref.artifact_path`` and later handed to
    :func:`append_capture`, keeping the on-disk WAV equal to the path the
    durable measurement record names.
    """

    subdir = "captures" if kind == "driver" else "summed"
    parts = [kind, _safe_slug(group, fallback="group")]
    if role:
        parts.append(_safe_slug(role, fallback="role"))
    parts.append(uuid.uuid4().hex)
    return f"{subdir}/{'_'.join(parts)}.wav"


def _detect_build_sha() -> str | None:
    """Best-effort ``JASPER_GIT_SHA`` from the install-time build manifest.

    Mirrors the reader in ``jasper/web/_common.py``'s ``_asset_version()``,
    except an absent/unknown/dev value returns ``None`` here (this is a
    forensic field on a bundle, not a cache-busting token that needs SOME
    value).
    """

    try:
        with _BUILD_MANIFEST_PATH.open() as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line.startswith("JASPER_GIT_SHA="):
                    continue
                sha = line.split("=", 1)[1].strip()
                return sha if sha and sha not in {"unknown", "dev"} else None
    except OSError:
        return None
    return None


def _calibration_sha256(calibration_id: str) -> str | None:
    """Best-effort sha256 of the calibration file backing ``calibration_id``.

    Any lookup failure (missing calibration, malformed metadata) yields
    ``None``: a forensic field, never a gate.
    """

    if not calibration_id:
        return None
    try:
        from jasper.audio_measurement.calibration import load_calibration_record

        record = load_calibration_record(calibration_id)
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
        return None
    sha = getattr(record, "file_sha256", None)
    if sha:
        return str(sha)
    try:
        return sha256_file(Path(record.raw_path))
    except OSError:
        return None


def _info_path(bundle_dir: Path) -> Path:
    return bundle_dir / "info.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise BundleError(f"could not read {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BundleError(f"{path.name} is invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise BundleError(f"{path.name} must be a JSON object")
    return data


def _read_info(bundle_dir: Path) -> dict[str, Any]:
    return _read_json(_info_path(bundle_dir))


def _write_info(bundle_dir: Path, info: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(info)
    write_json_artifact(
        bundle_dir,
        "info.json",
        payload,
        kind="metadata",
        sensitivity="config",
        recomputable=False,
        generated_by="active_speaker.bundles",
        schema_version=BUNDLE_SCHEMA_VERSION,
        file_mode=BUNDLE_FILE_MODE,
    )
    return payload


def _bundle_byte_size(bundle_dir: Path) -> int:
    total = 0
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _iter_bundle_dirs(root: Path) -> list[Path]:
    """Parseable bundle directories under ``root``, newest ``started_at`` first."""

    if not root.is_dir():
        return []
    candidates: list[tuple[float, str, Path]] = []
    for sub in root.iterdir():
        if not sub.is_dir() or not _info_path(sub).exists():
            continue
        try:
            info = _read_info(sub)
        except BundleError:
            continue
        try:
            started_at = float(info.get("started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0.0
        candidates.append((started_at, sub.name, sub))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [bundle_dir for _, _, bundle_dir in candidates]


def _iter_retention_dirs(root: Path) -> list[Path]:
    """All bundle-shaped directories, including interrupted partial creates."""

    if not root.is_dir():
        return []
    candidates: list[tuple[float, str, Path]] = []
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        started_at: float | None = None
        if _info_path(sub).exists():
            try:
                info = _read_info(sub)
                started_at = float(info.get("started_at") or 0.0)
            except (BundleError, TypeError, ValueError):
                started_at = None
        if started_at is None:
            try:
                started_at = sub.stat().st_mtime
            except OSError:
                started_at = 0.0
        candidates.append((started_at, sub.name, sub))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [bundle_dir for _, _, bundle_dir in candidates]


def _abandon_open_bundles(root: Path) -> None:
    """Mark every currently-``open`` bundle ``abandoned`` (at most one open)."""

    for bundle_dir in _iter_bundle_dirs(root):
        try:
            info = _read_info(bundle_dir)
        except BundleError:
            continue
        if info.get("state") == "open":
            _write_info(
                bundle_dir,
                {
                    **info,
                    "state": "abandoned",
                    "updated_at": time.time(),
                },
            )


@_fail_soft("open_bundle")
def open_bundle(
    topology: OutputTopology,
    *,
    calibration_id: str,
    comparison_set_fingerprint: str | None = None,
    mic_calibration_sha256: str | None = None,
    build_sha: str | None = None,
    now: float | None = None,
    sessions_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Open a new active-speaker commissioning bundle.

    Marks any prior ``state == "open"`` bundle ``abandoned`` first — at most one
    open bundle at a time. ``comparison_set_fingerprint`` is typically unknown
    at open time; pass ``None`` and back-fill with :func:`attach_comparison_set`.

    Returns the info payload (with ``session_id`` and a string ``bundle_dir``
    merged in) or ``None`` on any I/O failure. The comparison-set flow may
    continue diagnostically after ``None``, but the resulting evidence cannot
    enter the admitted playback/candidate path, having no exact authority.
    """

    root = sessions_dir if sessions_dir is not None else _default_sessions_dir()
    root.mkdir(parents=True, exist_ok=True)
    _abandon_open_bundles(root)

    session_id = uuid.uuid4().hex[:12]
    bundle_dir = root / session_id
    created_at = now if now is not None else time.time()
    resolved_build_sha = build_sha if build_sha is not None else _detect_build_sha()
    resolved_mic_sha = (
        mic_calibration_sha256
        if mic_calibration_sha256 is not None
        else _calibration_sha256(calibration_id)
    )

    # A caller-supplied topology of an unexpected shape degrades to "no bundle
    # recorded" rather than crashing the flow it describes. Normalized into
    # BundleError so the shared fail-soft guard covers it without widening.
    try:
        topology_fingerprints = {
            "topology_id": topology.topology_id,
            "topology_fingerprint": _measurement._fingerprint(
                {
                    "topology_id": topology.topology_id,
                    "hardware": _measurement._hardware_payload(topology),
                }
            ),
            "output_assignments": [
                {
                    "group_id": target["speaker_group_id"],
                    "role": target["role"],
                    "physical_output_index": target["output_index"],
                }
                for target in _measurement.active_driver_targets(topology)
            ],
        }
    except (AttributeError, TypeError, KeyError) as exc:
        raise BundleError(f"malformed output topology: {exc}") from exc

    # Establish production authority only after every pure input has validated.
    # A malformed caller therefore cannot leave a marker-only directory behind.
    create_admission_authority(
        bundle_dir,
        bundle_kind=BUNDLE_KIND,
        bundle_id=session_id,
    )

    info = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "session_id": session_id,
        "started_at": created_at,
        "updated_at": created_at,
        "state": "open",
        "fingerprints": {
            **topology_fingerprints,
            "graph_fingerprint": None,
            "mic": {
                "calibration_id": str(calibration_id or ""),
                "calibration_sha256": resolved_mic_sha,
            },
            "comparison_set_fingerprint": comparison_set_fingerprint,
            "comparison_set_id": None,
            "build_sha": resolved_build_sha,
        },
        "placement": {
            "policy_id": DRIVER_PLACEMENT_POLICY_ID,
            "acknowledged": False,
        },
        "captures": [],
        "summed_captures": [],
        "repeat_progress": {},
        "proposal": None,
        "previous_values": None,
        "proposed_values": None,
        "corrections_provenance": None,
        "compile_validation": None,
        "apply": None,
        "rollback_target": None,
        "verification": None,
    }
    os.chmod(bundle_dir, 0o750)
    _write_info(bundle_dir, info)
    result = {**info, "bundle_dir": str(bundle_dir)}
    enforce_retention(root)
    return result


def open_bundle_admission_authority(
    bundle_dir: str | Path,
    *,
    expected_session_id: str,
) -> AdmissionAuthority:
    """Open only a new commissioning bundle with exact Shared authority.

    Bundles created before production admission have no authority marker and
    remain historical evidence.  This function never creates or repairs a
    marker on an existing directory.
    """

    target = Path(bundle_dir)
    info = _read_info(target)
    if info.get("kind") != BUNDLE_KIND or info.get("session_id") != expected_session_id:
        raise BundleError("commissioning bundle identity does not match its session")
    return open_admission_authority(
        target,
        expected_bundle_kind=BUNDLE_KIND,
        expected_bundle_id=expected_session_id,
    )


@_fail_soft("attach_comparison_set")
def attach_comparison_set(
    bundle_dir: Path,
    *,
    comparison_set_id: str,
    comparison_set_fingerprint: str,
) -> dict[str, Any] | None:
    """Back-fill the comparison-set fingerprint once it exists.

    ``comparison_set_fingerprint`` is unknowable at :func:`open_bundle` time;
    this fills the gap so the bundle carries the same forensic field as the
    ``session_id``-joined measurement records, without gating anything on it.
    """

    info = _read_info(bundle_dir)
    fingerprints = dict(info.get("fingerprints") or {})
    fingerprints["comparison_set_id"] = comparison_set_id
    fingerprints["comparison_set_fingerprint"] = comparison_set_fingerprint
    return _write_info(
        bundle_dir,
        {
            **info,
            "fingerprints": fingerprints,
            "updated_at": time.time(),
        },
    )


@_fail_soft("mark_state")
def mark_state(bundle_dir: Path, state: str) -> dict[str, Any] | None:
    """Set ``info.json``'s ``state`` field directly (validated enum)."""

    if state not in _VALID_STATES:
        raise BundleError(f"unsupported bundle state: {state!r}")
    info = _read_info(bundle_dir)
    return _write_info(
        bundle_dir,
        {
            **info,
            "state": state,
            "updated_at": time.time(),
        },
    )


def _capture_group_role(payload: Mapping[str, Any]) -> tuple[Any, Any]:
    """Resolve ``(group, role)`` for a capture entry.

    Prefers top-level ``speaker_group_id``/``role`` keys on ``payload``, falling
    back to the nested ``measurement`` record, which carries them only for a
    RECORDED driver capture: a summed record's nested ``measurement`` has no
    ``role`` (group-level), and a skipped capture has no nested record at all.
    """

    measurement_block = payload.get("measurement")
    if not isinstance(measurement_block, Mapping):
        measurement_block = {}
    group = payload.get("speaker_group_id") or measurement_block.get("speaker_group_id")
    role = payload.get("role") or measurement_block.get("role")
    return group, role


def _guarded_capture_source(
    bundle_dir: Path, wav_source_path: Path | str, *, op: str
) -> Path | None:
    """Validate a capture WAV source exists and is within the size cap.

    Returns ``None`` (WARN-logged under the shared fail-soft event name)
    when the guard fails, so the caller can bail out before touching the
    bundle at all — never a partial write from a missing/oversized source.
    """

    try:
        source = Path(wav_source_path)
    except TypeError:
        log_event(
            logger,
            "active_speaker.bundle_write_failed",
            level=logging.WARNING,
            session=bundle_dir.name,
            op=op,
            error="capture wav source is not a filesystem path",
        )
        return None
    try:
        source_size = source.stat().st_size
    except OSError:
        source_size = None
    if source_size is None or source_size > MAX_CAPTURE_WAV_BYTES:
        log_event(
            logger,
            "active_speaker.bundle_write_failed",
            level=logging.WARNING,
            session=bundle_dir.name,
            op=op,
            error="capture wav source is missing or too large",
        )
        return None
    return source


def _record_capture_wav(bundle_dir: Path, rel_path: str) -> None:
    """Enter one in-bundle capture WAV in the artifact manifest."""

    record_artifact(
        bundle_dir,
        rel_path,
        kind="capture_wav",
        sensitivity="private_raw_audio",
        recomputable=False,
        generated_by="active_speaker.bundles",
        bundle_schema_version=(
            BUNDLE_SCHEMA_VERSION
            if (bundle_dir / "info.json").exists()
            else LEGACY_PARTIAL_BUNDLE_SCHEMA_VERSION
        ),
    )


def _copy_wav_into_bundle(bundle_dir: Path, source: Path, rel_path: str) -> None:
    """Copy (never move) one WAV to ``bundle_dir / rel_path`` and record it.

    Copy, not move, so ``web_measurement.py``'s own browser-capture-store
    retention is untouched. Raises on failure (``OSError``/``BundleError``)
    — the caller is a ``_fail_soft``-wrapped public entry point.
    """

    dest = bundle_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o750)
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        shutil.copy2(source, tmp)
        os.chmod(tmp, BUNDLE_FILE_MODE)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    _record_capture_wav(bundle_dir, rel_path)


def _append_capture_entry(
    bundle_dir: Path, *, kind: str, rel_path: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Write one capture's ``*.json`` sidecar and append its ``info.json`` entry.

    Shared by the two placement routes — :func:`append_capture` copies the WAV
    in first, :func:`register_capture` finds it already there — so one entry
    shape serves both. Raises on failure; both callers are ``_fail_soft``.
    """

    group, role = _capture_group_role(payload)
    json_rel = str(Path(rel_path).with_suffix(".json"))
    write_json_artifact(
        bundle_dir,
        json_rel,
        dict(payload),
        kind="capture_analysis",
        sensitivity="derived",
        recomputable=True,
        generated_by="active_speaker.bundles",
        dependencies=[rel_path],
        schema_version=BUNDLE_SCHEMA_VERSION,
        file_mode=BUNDLE_FILE_MODE,
    )

    measurement_block = payload.get("measurement")
    if not isinstance(measurement_block, Mapping):
        measurement_block = {}
    entry: dict[str, Any] = {
        # The discriminator, written because the list an entry lands in no
        # longer answers it: ``summed`` and ``sequential`` share
        # ``summed_captures``. Absent on entries banked before this field.
        "kind": kind,
        "group": group,
        "artifact_path": rel_path,
        "capture_json_path": json_rel,
        "recorded_at": time.time(),
        "verdict": payload.get("verdict"),
        "outcome": payload.get("outcome"),
        "quality": payload.get("acoustic"),
        "excitation": payload.get("excitation"),
        "placement_ack": payload.get("placement_proof"),
        "measurement_id": (
            measurement_block.get("measurement_id")
            or measurement_block.get("validation_id")
        ),
    }
    if kind == "driver":
        entry["role"] = role
    else:
        entry["crossover_fc_hz"] = payload.get("crossover_fc_hz")

    info = _read_info(bundle_dir)
    list_key = "captures" if kind == "driver" else "summed_captures"
    placement = dict(info.get("placement") or {})
    placement_proof = payload.get("placement_proof")
    if (
        isinstance(placement_proof, Mapping)
        and placement_proof.get("accepted") is True
        and placement_proof.get("policy_id") == placement.get("policy_id")
    ):
        # The acknowledgement is server-normalized after the host verifies
        # the operator's checked box.  This repairs the former dead literal:
        # an opened bundle starts false and flips only on real accepted proof.
        placement["acknowledged"] = True
    _write_info(
        bundle_dir,
        {
            **info,
            "placement": placement,
            list_key: [*(info.get(list_key) or []), entry],
            "updated_at": time.time(),
        },
    )
    return entry


@_fail_soft("register_capture")
def register_capture(
    bundle_dir: Path,
    *,
    kind: str,
    relative_path: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Record a capture WAV the caller already wrote into the bundle.

    :func:`append_capture`'s sibling for callers that mint the bundle relpath
    with :func:`capture_artifact_relpath` and stream the bytes straight to it.
    With no source file outside the bundle there is nothing to copy or
    size-guard, so this does only the recording half.
    """

    if kind not in _CAPTURE_KINDS:
        raise BundleError(f"unsupported capture kind: {kind!r}")
    _record_capture_wav(bundle_dir, relative_path)
    return _append_capture_entry(
        bundle_dir, kind=kind, rel_path=relative_path, payload=payload
    )


@_fail_soft("append_capture")
def append_capture(
    bundle_dir: Path,
    *,
    kind: str,
    wav_source_path: Path | str,
    payload: Mapping[str, Any],
    relative_path: str | None = None,
) -> dict[str, Any] | None:
    """Copy one capture WAV into the bundle and record its compact entry.

    ``payload`` is what a ``record_*_acoustic_capture`` call returned, or a
    caller-enriched superset. It is written verbatim as the capture's ``*.json``
    artifact, and a compact entry is appended to ``info.json``'s
    ``captures``/``summed_captures`` list.

    Guards the source file's existence and size before copying: a missing or
    oversized source WARNs and returns ``None`` without touching the bundle.
    """

    if kind not in _CAPTURE_KINDS:
        raise BundleError(f"unsupported capture kind: {kind!r}")
    source = _guarded_capture_source(bundle_dir, wav_source_path, op="append_capture")
    if source is None:
        return None

    group, role = _capture_group_role(payload)
    rel_path = relative_path or capture_artifact_relpath(kind, group, role)
    _copy_wav_into_bundle(bundle_dir, source, rel_path)
    return _append_capture_entry(
        bundle_dir, kind=kind, rel_path=rel_path, payload=payload
    )


@_fail_soft("append_repeat_capture")
def append_repeat_capture(
    bundle_dir: Path,
    *,
    index: int,
    wav_source_path: Path | str,
    payload: Mapping[str, Any],
    relative_path: str | None = None,
) -> dict[str, Any] | None:
    """Copy one repeat-attempt WAV into ``repeat_captures/`` and record it.

    Unlike :func:`append_capture`, a repeat attempt gets no compact
    ``info.json`` entry: ``aggregate_driver_repeats``'s ``per_repeat[]`` array,
    attached to the WINNING capture's entry, is where each repeat's
    ``artifact_path`` is discoverable. This files only the raw evidence — the
    WAV plus its quality JSON, with a manifest dependency edge between them.

    Returns ``{artifact_path, quality_json_path}`` or ``None`` on any
    guard/write failure.
    """

    source = _guarded_capture_source(
        bundle_dir, wav_source_path, op="append_repeat_capture"
    )
    if source is None:
        return None

    rel_path = relative_path or f"repeat_captures/repeat_{index}_{uuid.uuid4().hex}.wav"
    _copy_wav_into_bundle(bundle_dir, source, rel_path)

    json_rel = str(Path(rel_path).with_suffix(".json"))
    write_json_artifact(
        bundle_dir,
        json_rel,
        dict(payload),
        kind="repeat_capture_analysis",
        sensitivity="derived",
        recomputable=True,
        generated_by="active_speaker.bundles",
        bundle_schema_version=(
            BUNDLE_SCHEMA_VERSION
            if (bundle_dir / "info.json").exists()
            else LEGACY_PARTIAL_BUNDLE_SCHEMA_VERSION
        ),
        dependencies=[rel_path],
        schema_version=BUNDLE_SCHEMA_VERSION,
        file_mode=BUNDLE_FILE_MODE,
    )
    return {"artifact_path": rel_path, "quality_json_path": json_rel}


@_fail_soft("record_repeat_progress")
def record_repeat_progress(
    bundle_dir: Path,
    *,
    comparison_set_id: str,
    target_fingerprint: str,
    target_id: str,
    attempts: int,
    accepted: int,
    target: int,
    per_repeat: list[Mapping[str, Any]],
    status: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Persist compact, comparison-bound interim repeat state.

    Raw WAVs and full analyses remain manifest artifacts. ``info.json`` keeps
    only a forensic mirror of the authoritative admission ledger so a session
    can be diagnosed without making bundle state a playback controller.
    """

    if status not in {"active", "completed", "refused"}:
        raise BundleError("repeat progress status is invalid")
    info = _read_info(bundle_dir)
    progress = dict(info.get("repeat_progress") or {})
    entry: dict[str, Any] = {
        "schema_version": 1,
        "comparison_set_id": str(comparison_set_id),
        "target_fingerprint": str(target_fingerprint),
        "target_id": str(target_id),
        "attempts": int(attempts),
        "accepted": int(accepted),
        "target": int(target),
        "status": status,
        "per_repeat": [
            {
                key: item.get(key)
                for key in (
                    "index",
                    "attempt",
                    "accepted",
                    "reject_reason",
                    "artifact_path",
                    "estimated_snr_db",
                    "clipping",
                    "above_validity_floor",
                    "level_dbfs",
                )
            }
            for item in per_repeat[:4]
        ],
        "updated_at": time.time(),
    }
    if reason:
        entry["reason"] = str(reason)
    progress[str(target_id)] = entry
    _write_info(
        bundle_dir,
        {
            **info,
            "repeat_progress": progress,
            "updated_at": time.time(),
        },
    )
    return entry


def _plain(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


@_fail_soft("record_apply")
def record_apply(
    bundle_dir: Path,
    *,
    candidate: Mapping[str, Any],
    apply_state: Mapping[str, Any] | None,
    rollback_target: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Record one apply attempt (success, failure, or refusal) into the bundle.

    ``candidate`` is any of ``apply_baseline_profile``'s three return shapes.
    Success is ``apply_state`` truthy AND ``candidate["status"] == "applied"``;
    anything else records ``state = "failed"`` — a refused apply never reached
    the DSP transaction, but the attempt is still evidence.
    """

    info = _read_info(bundle_dir)
    fingerprints = dict(info.get("fingerprints") or {})
    source = candidate.get("source")
    source_fingerprint = (
        source.get("fingerprint") if isinstance(source, Mapping) else None
    )
    if not fingerprints.get("graph_fingerprint") and source_fingerprint:
        fingerprints["graph_fingerprint"] = source_fingerprint

    success = bool(apply_state) and candidate.get("status") == "applied"
    updated = {
        **info,
        "fingerprints": fingerprints,
        "proposal": _plain(candidate.get("proposal")),
        "previous_values": _plain(candidate.get("previous_values")),
        "proposed_values": _plain(candidate.get("proposed_values")),
        "corrections_provenance": _plain(candidate.get("corrections_provenance")),
        "compile_validation": _plain(candidate.get("validation")),
        "apply": _plain(apply_state),
        "rollback_target": _plain(rollback_target),
        "state": "applied" if success else "failed",
        "updated_at": time.time(),
    }
    _write_info(bundle_dir, updated)
    write_json_artifact(
        bundle_dir,
        "proposal.json",
        dict(candidate),
        kind="candidate_profile",
        sensitivity="derived",
        recomputable=True,
        generated_by="active_speaker.bundles",
        schema_version=BUNDLE_SCHEMA_VERSION,
        file_mode=BUNDLE_FILE_MODE,
    )
    if apply_state is not None:
        write_json_artifact(
            bundle_dir,
            "apply.json",
            dict(apply_state),
            kind="apply_transaction",
            sensitivity="derived",
            recomputable=False,
            generated_by="active_speaker.bundles",
            schema_version=BUNDLE_SCHEMA_VERSION,
            file_mode=BUNDLE_FILE_MODE,
        )
    return updated


def summarize_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Return ``info.json`` plus derived counts/sizes for one bundle.

    Raises ``BundleError`` for a missing/malformed bundle; callers that want to
    skip bad entries use :func:`list_bundles`.
    """

    if not bundle_dir.is_dir():
        raise BundleError(f"{bundle_dir} is not a directory")
    info = dict(_read_info(bundle_dir))
    info["bundle_dir"] = str(bundle_dir)
    info["bundle_size_bytes"] = _bundle_byte_size(bundle_dir)
    info["capture_count"] = len(info.get("captures") or [])
    info["summed_capture_count"] = len(info.get("summed_captures") or [])
    info["has_proposal"] = (bundle_dir / "proposal.json").exists()
    info["has_apply"] = (bundle_dir / "apply.json").exists()
    manifest_path = bundle_dir / "artifact_manifest.json"
    info["has_artifact_manifest"] = manifest_path.exists()
    if manifest_path.exists():
        try:
            manifest = read_artifact_manifest(bundle_dir)
            artifacts = manifest.get("artifacts")
            info["artifact_count"] = (
                len(artifacts) if isinstance(artifacts, list) else 0
            )
        except BundleError:
            info["artifact_count"] = 0
            info["artifact_manifest_error"] = True
    else:
        info["artifact_count"] = 0
    return info


def list_bundles(root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """List parseable bundles newest-first, skipping partial/malformed writes."""

    if limit <= 0:
        return []
    entries: list[dict[str, Any]] = []
    for bundle_dir in _iter_bundle_dirs(root)[:limit]:
        try:
            entries.append(summarize_bundle(bundle_dir))
        except BundleError:
            continue
    return entries


def latest_bundle(root: Path) -> dict[str, Any] | None:
    """The single newest parseable bundle under ``root``, or ``None``."""

    found = list_bundles(root, limit=1)
    return found[0] if found else None


def enforce_retention(
    root: Path,
    *,
    max_bytes: int | None = None,
    max_bundles: int | None = None,
) -> None:
    """Delete oldest whole bundles once storage exceeds the configured cap.

    Every unfinished COMPLETE bundle (``state`` in ``open``/``proposal_ready``)
    plus the single newest complete bundle are protected, so a live or
    just-completed session cannot be evicted by its own size. Interrupted
    directories without a parseable ``info.json`` count against both caps and
    are never protected.
    Deletion is whole-bundle, oldest-``started_at``-first among the unprotected.
    Independently fail-soft: any I/O error WARNs and stops the sweep.
    """

    try:
        _enforce_retention(
            root,
            max_bytes=max_bytes if max_bytes is not None else _sessions_max_bytes(),
            max_bundles=(
                max_bundles if max_bundles is not None else _sessions_max_bundles()
            ),
        )
    except (OSError, BundleError) as exc:
        log_event(
            logger,
            "active_speaker.bundle_write_failed",
            level=logging.WARNING,
            session=None,
            op="enforce_retention",
            error=str(exc),
        )


def _enforce_retention(root: Path, *, max_bytes: int, max_bundles: int) -> None:
    bundle_dirs = _iter_retention_dirs(root)  # newest-first, including partials
    if not bundle_dirs:
        return

    complete_dirs = _iter_bundle_dirs(root)
    protected: set[Path] = {complete_dirs[0]} if complete_dirs else set()
    for bundle_dir in complete_dirs:
        try:
            info = _read_info(bundle_dir)
        except BundleError:
            continue
        if info.get("state") in _UNFINISHED_STATES:
            protected.add(bundle_dir)

    sizes = {bundle_dir: _bundle_byte_size(bundle_dir) for bundle_dir in bundle_dirs}
    kept_count = len(protected)
    kept_bytes = sum(sizes.get(bundle_dir, 0) for bundle_dir in protected)

    for bundle_dir in bundle_dirs:
        if bundle_dir in protected:
            continue
        size = sizes.get(bundle_dir, 0)
        if kept_count < max_bundles and kept_bytes + size <= max_bytes:
            kept_count += 1
            kept_bytes += size
            continue
        shutil.rmtree(bundle_dir, ignore_errors=True)
