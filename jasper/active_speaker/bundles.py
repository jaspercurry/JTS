# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker commissioning bundle: durable, append-only session evidence.

The active-crossover measurement flow already keeps a "latest-wins" current
state ([`measurement.py`](measurement.py), backed by
``/var/lib/jasper/active_speaker_measurements.json``) so the baseline compiler
always has one clean answer to "what does the household's speaker currently
measure as?". That pointer state intentionally overwrites itself on every new
capture — it is not evidence for later forensics, corpus review, or "what
actually happened during commissioning?" questions.

This module is the missing append-only half, ported from the room-correction
session-bundle pattern ([`jasper.correction.bundles`](../correction/bundles.py)):
one durable, hashed, retention-bounded directory per commissioning attempt
(a "bundle"), holding the fingerprints, captures, and apply transaction that
produced (or failed to produce) a baseline. It reuses
``jasper.correction.bundles``'s generic manifest primitives
(:func:`~jasper.correction.bundles.record_artifact`,
:func:`~jasper.correction.bundles.write_json_artifact`,
:func:`~jasper.correction.bundles.read_artifact_manifest`) directly rather than
forking them. The primitives resolve the owning bundle schema from ``info.json``;
only the active-speaker-specific shape (its fields, retention policy, and the
active core-artifact list) lives here.

Two invariants keep this module safe to bolt onto an already-shipped flow:

- **Direction.** This module may import ``measurement.py``,
  ``capture_geometry.py``, and friends. Nothing in ``measurement.py`` imports
  this module, and the bundle is never read back as an input to any decision
  path (baseline compilation, apply gating, proposal) — it is forensic
  evidence only. The one join key between the two is ``session_id``: a
  measurement record's optional ``bundle`` field
  (``{session_id, artifact_path}``) points at a bundle; the bundle never
  points back the other way except by the same id.
- **Fail-soft everywhere.** Every public write entry point catches
  ``OSError`` / :class:`~jasper.correction.bundles.BundleError` (plus a stray
  ``ValueError`` normalized the same way), logs one
  ``active_speaker.bundle_write_failed`` WARNING via
  :func:`jasper.log_event.log_event`, and returns ``None`` instead of
  raising. A bundle-write failure must never block capture recording or a
  baseline apply — see the ``No silent failure paths`` / resilience rules in
  ``AGENTS.md``. Callers treat ``None`` as "no bundle evidence recorded this
  time" and carry on; the measurement/apply path they are threading through
  has already (or will already) succeed or fail on its own terms.
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

from jasper.correction.bundles import (
    BundleError,
    _sha256_file,
    read_artifact_manifest,
    record_artifact,
    write_json_artifact,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from . import measurement as _measurement
from .capture_geometry import DRIVER_PLACEMENT_POLICY_ID
from .test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_KIND = "jts_active_speaker_commissioning_bundle"

DEFAULT_SESSIONS_DIR = Path("/var/lib/jasper/active_speaker/sessions")
SESSIONS_DIR_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_DIR"

# Mirrors jasper.active_speaker.web_measurement.MAX_CAPTURE_WAV_BYTES — the
# browser capture store's own cap. A bundle copy is never larger than the
# capture it was made from, so the same ceiling bounds the copy.
MAX_CAPTURE_WAV_BYTES = CROSSOVER_CAPTURE_MAX_WAV_BYTES

DEFAULT_SESSIONS_MAX_BYTES = 256 * 1024 * 1024
SESSIONS_MAX_BYTES_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BYTES"
DEFAULT_SESSIONS_MAX_BUNDLES = 12
SESSIONS_MAX_BUNDLES_ENV = "JASPER_ACTIVE_SPEAKER_SESSIONS_MAX_BUNDLES"

# Mirrors web_measurement.CAPTURE_FILE_MODE. Bundle directories and capture
# subdirectories are explicitly chmod'd 0o750 (umask-proof; group keeps
# traverse/read under the /var/lib/jasper group model) in open_bundle() and
# _copy_wav_into_bundle(). Files stay at this mode.
BUNDLE_FILE_MODE = 0o640

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
            except (OSError, BundleError, ValueError) as exc:
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

    Callers (``web_measurement.py``) mint this path BEFORE the single
    measurement write so the same relative path can be embedded as the
    record's ``bundle_ref.artifact_path`` and later handed to
    :func:`append_capture` as ``relative_path`` — the on-disk WAV then equals
    the path the durable measurement record points at.
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

    Prefers :class:`~jasper.audio_measurement.calibration.CalibrationRecord`'s
    already-computed ``file_sha256``; falls back to hashing ``raw_path``
    directly only if that field is somehow absent. Any lookup failure
    (missing calibration, malformed metadata) yields ``None`` — a forensic
    field, never a gate.
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
        return _sha256_file(Path(record.raw_path))
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

    Marks any prior ``state == "open"`` bundle ``abandoned`` first (at most
    one open bundle at a time — a new comparison set supersedes the last),
    mints a fresh ``session_id``, writes ``info.json`` with every SC-4
    required field, then sweeps retention. ``comparison_set_fingerprint`` is
    typically unknown at open time (the comparison set is minted moments
    later by ``measurement.start_active_comparison_set``); pass ``None`` and
    back-fill with :func:`attach_comparison_set` once it exists.

    ``build_sha`` defaults to a best-effort read of
    ``/var/lib/jasper/build.txt`` when not supplied.

    Returns the info payload (with ``session_id`` and ``bundle_dir`` — a
    string — merged in) or ``None`` on any I/O failure (WARN-logged; see
    :func:`_fail_soft`). Bundle evidence is forensic-only, so a failure here
    must never block the comparison-set/level-match flow that called it.
    """

    root = sessions_dir if sessions_dir is not None else _default_sessions_dir()
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

    # A caller-supplied topology that doesn't shape up as expected (a bad
    # mock, a future caller change) must degrade to "no bundle recorded",
    # like any other bundle-write failure — never crash the comparison-set
    # flow it's describing. Normalize into BundleError so the shared
    # fail-soft decorator's (OSError, BundleError, ValueError) guard covers
    # it without widening that guard for every other entry point.
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
    bundle_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bundle_dir, 0o750)
    _write_info(bundle_dir, info)
    result = {**info, "bundle_dir": str(bundle_dir)}
    enforce_retention(root)
    return result


@_fail_soft("attach_comparison_set")
def attach_comparison_set(
    bundle_dir: Path,
    *,
    comparison_set_id: str,
    comparison_set_fingerprint: str,
) -> dict[str, Any] | None:
    """Back-fill the comparison-set fingerprint once it exists.

    ``comparison_set_fingerprint`` is unknowable at :func:`open_bundle` time
    (the comparison set is minted in the very next call at the actual
    integration site); this fills the gap so the bundle carries the same
    forensic convenience field as ``session_id``-joined measurement records,
    without gating anything on it.
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

    Prefers top-level ``speaker_group_id`` / ``role`` keys on ``payload`` (a
    caller — ``web_measurement.py`` — already has these in scope and can
    enrich the payload it hands to :func:`append_capture`); falls back to the
    nested ``measurement`` record, which carries them for a *recorded*
    driver capture. A summed record's nested ``measurement`` never carries
    ``role`` (group-level), and a *skipped* (``recorded=False``) capture has
    no nested ``measurement`` record at all — for that case only the
    caller-supplied top-level keys are available.
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
    record_artifact(
        bundle_dir,
        rel_path,
        kind="capture_wav",
        sensitivity="private_raw_audio",
        recomputable=False,
        generated_by="active_speaker.bundles",
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

    ``payload`` is the dict a ``record_driver_acoustic_capture`` /
    ``record_summed_acoustic_capture`` call returned (or a caller-enriched
    superset of it — see :func:`_capture_group_role`). The full payload
    (including the nested ``measurement`` record) is written verbatim as the
    capture's ``*.json`` artifact; a compact entry (§4 of the design) is
    appended to ``info.json``'s ``captures`` / ``summed_captures`` list.

    Guards the source file's existence and size before copying; a missing
    or oversized source WARNs and returns ``None`` without touching the
    bundle.
    """

    if kind not in {"driver", "summed"}:
        raise BundleError(f"unsupported capture kind: {kind!r}")
    source = _guarded_capture_source(bundle_dir, wav_source_path, op="append_capture")
    if source is None:
        return None

    group, role = _capture_group_role(payload)
    rel_path = relative_path or capture_artifact_relpath(kind, group, role)
    _copy_wav_into_bundle(bundle_dir, source, rel_path)

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
        # The acknowledgement is server-normalized after the relay verifies
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

    Unlike :func:`append_capture`, a repeat attempt has no compact
    ``info.json`` list entry of its own — the
    ``commissioning_capture.aggregate_driver_repeats`` result already
    indexes every attempt via its own ``per_repeat[]`` array (attached to
    the WINNING capture's entry once the caller records the aggregate
    through the normal :func:`append_capture` path), and that array is
    where each repeat's ``artifact_path`` is discoverable. This function
    only files the raw evidence: the WAV plus its quality JSON, both in the
    manifest with a dependency edge between them, mirroring
    :func:`append_capture`'s WAV+JSON pair.

    Returns ``{artifact_path, quality_json_path}`` or ``None`` on any
    guard/write failure (WARN-logged; fail-soft, same as every other public
    write entry point in this module).
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

    ``candidate`` is the baseline-profile candidate/applied/failed dict
    ``apply_baseline_profile`` is about to return (any of its three return
    shapes). Success is ``apply_state`` truthy AND ``candidate["status"] ==
    "applied"``; anything else records ``state = "failed"`` (a refused/
    blocked apply never reached the DSP transaction at all, so there is
    nothing to roll back, but the attempt itself is still evidence).
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

    Modeled on ``jasper.correction.bundles.summarize_bundle`` with its own
    active-speaker-specific core-artifact list. Raises ``BundleError`` for a
    missing/malformed bundle — callers that want to skip bad entries use
    :func:`list_bundles`, which already does that.
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

    Every unfinished bundle (``state`` in ``open``/``proposal_ready``) plus
    the single newest bundle overall are protected, so a live or
    just-completed session can never be evicted by its own size. Deletion is
    whole-bundle (``shutil.rmtree``), oldest-``started_at``-first among the
    unprotected set. Fail-soft: any I/O error during the sweep is
    WARN-logged and the sweep simply stops — this is always called from
    :func:`open_bundle`'s own fail-soft wrapper, but is public and
    independently fail-soft so a future direct caller (a maintenance script)
    gets the same guarantee.
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
    bundle_dirs = _iter_bundle_dirs(root)  # newest-first
    if not bundle_dirs:
        return

    protected: set[Path] = {bundle_dirs[0]}
    for bundle_dir in bundle_dirs:
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
