# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle state for the optional enhanced WebRTC AEC3 engine.

The ordinary speaker install always provides ``jasper_aec3._aec3``: the
small binding around Debian's WebRTC audio-processing library.  The vendored
v2 engine is an optional capability because compiling it on a Pi is slow.

This module is deliberately the single owner of the capability contract:

* :data:`INTENT_PATH` records the durable operator request.
* :data:`JOB_STATE_PATH` records transient/background installer progress.
* :data:`INSTALLED_MARKER_PATH` is root-owned proof of the last verified
  activation. It deliberately lives outside the group-writable shared state
  directory.
* :func:`desired_fingerprint` identifies the exact source, ABI, and vendored
  dependency that an installed extension must match.
* :func:`status` translates those facts into the stable HTTP/UI vocabulary.

The privileged builder lives in :mod:`jasper.cli.enhanced_aec_install`.
Keeping state and policy here lets jasper-control report the same truth without
importing the optional native module or duplicating installer decisions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
import sysconfig
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .atomic_io import advisory_file_lock, atomic_write_text, read_json_mapping
from .install_profile import (
    install_profile_supports_wake_detection,
    read_install_profile,
)
from .log_event import log_event

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FEATURE_ID = "enhanced_aec"

INTENT_PATH = Path("/var/lib/jasper/enhanced-aec-intent.json")
JOB_STATE_PATH = Path("/var/lib/jasper/enhanced-aec-install.json")
ACTIVATION_PROOF_DIR = Path("/var/lib/jasper-enhanced-aec")
INSTALLED_MARKER_PATH = ACTIVATION_PROOF_DIR / "installed.json"
STATE_LOCK_PATH = Path("/var/lib/jasper/.enhanced-aec-state.lock")
INSTALL_LOCK_PATH = ACTIVATION_PROOF_DIR / ".install.lock"

SOURCE_ROOT = Path("/opt/jasper/jasper_aec3")
VENV_ROOT = Path("/opt/jasper/.venv")
TARGET_MANIFEST_NAME = "enhanced-aec-source.env"

_ACTIVE_PHASES = frozenset({
    "queued",
    "snapshotting",
    "downloading",
    "building",
    "verifying",
    "activating",
})
_QUEUED_HANDOFF_GRACE_SECONDS = 15.0
_SOURCE_SUFFIXES = frozenset({".py", ".cpp", ".h", ".hpp"})
_REQUIRED_TARGET_KEYS = frozenset({
    "WEBRTC_AEC3_VERSION",
    "WEBRTC_AEC3_COMMIT",
    "WEBRTC_AEC3_ARCHIVE_URL",
    "WEBRTC_AEC3_SHA256",
})


class EnhancedAecError(RuntimeError):
    """Expected capability-state or build-input failure."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _age_seconds(raw: Any) -> float | None:
    try:
        updated = datetime.strptime(
            str(raw), "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max((datetime.now(timezone.utc) - updated).total_seconds(), 0.0)


def _read_versioned_object(path: Path) -> dict[str, Any]:
    payload = read_json_mapping(path) or {}
    version = payload.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return {}
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        mode=0o664,
        durable=True,
    )


def read_intent(*, path: Path = INTENT_PATH) -> dict[str, Any]:
    """Read durable operator intent. Missing/malformed means not requested."""

    payload = _read_versioned_object(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "requested": payload.get("requested") is True,
        "requested_at": str(payload.get("requested_at") or ""),
    }


def read_job_state(*, path: Path = JOB_STATE_PATH) -> dict[str, Any]:
    return _read_versioned_object(path)


def read_installed_marker(
    *, path: Path = INSTALLED_MARKER_PATH,
) -> dict[str, Any]:
    return _read_versioned_object(path)


def install_profile_supports_enhanced_aec() -> bool:
    """Whether this install has the voice/mic/AEC consumer for the feature."""

    try:
        return install_profile_supports_wake_detection(read_install_profile())
    except ValueError:
        # An invalid product role must never authorize a root compiler job.
        return False


def _log_job_transition(phase: str) -> None:
    """Emit only stable phase/result fields; compiler details stay in state."""

    if phase in {"success", "failed", "stale"}:
        log_event(
            logger,
            "enhanced_aec.install_terminal",
            result=phase,
        )
    else:
        log_event(
            logger,
            "enhanced_aec.install_phase",
            phase=phase,
        )


def request_install(
    *,
    intent_path: Path = INTENT_PATH,
    job_state_path: Path = JOB_STATE_PATH,
    state_lock_path: Path = STATE_LOCK_PATH,
) -> None:
    """Persist opt-in intent and publish a queued state atomically per file.

    This does not start a privileged process.  jasper-control writes intent
    first, then asks the restart broker to start the allowlisted root oneshot.
    If that handoff fails the request is still durable and the next boot/deploy
    reconcile can retry it.
    """

    now = _utc_now()
    with advisory_file_lock(
        state_lock_path,
        mode=0o660,
        timeout_sec=2.0,
    ):
        existing = read_intent(path=intent_path)
        _write_json(
            intent_path,
            {
                "schema_version": SCHEMA_VERSION,
                "requested": True,
                "requested_at": existing["requested_at"] or now,
                "last_requested_at": now,
            },
        )
        _write_json(
            job_state_path,
            {
                "schema_version": SCHEMA_VERSION,
                "phase": "queued",
                "detail": "Enhanced echo cancellation installation is queued.",
                "error": "",
                "updated_at": now,
            },
        )
    _log_job_transition("queued")


def write_job_state(
    phase: str,
    detail: str,
    *,
    error: str = "",
    desired_fingerprint: str = "",
    extra: Mapping[str, Any] | None = None,
    path: Path = JOB_STATE_PATH,
    state_lock_path: Path = STATE_LOCK_PATH,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "detail": detail,
        "error": error,
        "desired_fingerprint": desired_fingerprint,
        "updated_at": _utc_now(),
    }
    if extra:
        payload.update(extra)
    with advisory_file_lock(
        state_lock_path,
        mode=0o660,
        timeout_sec=5.0,
    ):
        _write_json(path, payload)
    _log_job_transition(phase)


def write_installed_marker(
    *,
    fingerprint: str,
    extension_sha256: str,
    extension_name: str,
    path: Path = INSTALLED_MARKER_PATH,
) -> None:
    """Publish verified activation proof.

    Callers must write this only after both the mandatory v1 and staged/active
    v2 modules import and process a frame successfully.
    """

    # Intent and progress are deliberately shared with jasper-control, but the
    # marker authorizes native code execution and therefore is not shared
    # mutable state.  Its parent is provisioned root:root 0755 by install.sh;
    # publishing root:root 0644 here lets non-root readers verify it without
    # letting any member of the shared `jasper` group rewrite or replace it.
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "extension_sha256": extension_sha256,
                "extension_name": extension_name,
                "installed_at": _utc_now(),
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        mode=0o644,
        group_from_parent=False,
        durable=True,
    )


def parse_target_manifest(
    source_root: Path = SOURCE_ROOT,
) -> dict[str, str]:
    """Parse the checked-in vendored-source manifest.

    It intentionally uses a shell-compatible ``KEY=value`` format: the same
    file is sourced by deploy/install.sh and read here, avoiding a second copy
    of the pinned URL/hash/commit.
    """

    path = source_root / TARGET_MANIFEST_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EnhancedAecError(f"missing enhanced-AEC source manifest: {path}") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    missing = sorted(key for key in _REQUIRED_TARGET_KEYS if not values.get(key))
    if missing:
        raise EnhancedAecError(
            "enhanced-AEC source manifest is missing: " + ", ".join(missing)
        )
    digest = values["WEBRTC_AEC3_SHA256"].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise EnhancedAecError("WEBRTC_AEC3_SHA256 is not a SHA-256 digest")
    if not values["WEBRTC_AEC3_ARCHIVE_URL"].startswith("https://"):
        raise EnhancedAecError("WEBRTC_AEC3_ARCHIVE_URL must use HTTPS")
    return values


def _fingerprinted_source_files(source_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if any(part in {"build", "dist", "__pycache__", ".git"} for part in relative.parts):
            continue
        if (
            path.name in {"pyproject.toml", TARGET_MANIFEST_NAME}
            or path.suffix in _SOURCE_SUFFIXES
        ):
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(source_root).as_posix())


def desired_fingerprint(source_root: Path = SOURCE_ROOT) -> str:
    """Return the exact enhanced-extension compatibility fingerprint."""

    # Validate before hashing so a malformed target never looks installable.
    parse_target_manifest(source_root)
    hasher = hashlib.sha256()
    hasher.update(b"jasper-enhanced-aec-v1\0")
    hasher.update(
        f"python={sys.version_info.major}.{sys.version_info.minor}\0".encode()
    )
    hasher.update(f"soabi={sysconfig.get_config_var('SOABI') or ''}\0".encode())
    for path in _fingerprinted_source_files(source_root):
        relative = path.relative_to(source_root).as_posix().encode()
        hasher.update(relative)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return "enhanced-aec-v1:" + hasher.hexdigest()


def extension_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


@lru_cache(maxsize=8)
def _cached_digest_matches(
    path_text: str,
    device: int,
    inode: int,
    size: int,
    mtime_ns: int,
    expected: str,
) -> bool:
    # stat fields are part of the cache key; any atomic replacement or
    # in-place mutation invalidates the cached digest without hashing the
    # native module on every /aec poll.
    del device, inode, size, mtime_ns
    try:
        return extension_sha256(Path(path_text)) == expected
    except OSError:
        return False


def extension_digest_matches(path: Path, expected: str) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False
    return _cached_digest_matches(
        str(path),
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        expected,
    )


def installed_v2_extensions(venv_root: Path = VENV_ROOT) -> list[Path]:
    pattern = "lib/python*/site-packages/jasper_aec3/_aec3_v2*.so"
    return sorted(path for path in venv_root.glob(pattern) if path.is_file())


def runtime_v2_verified(
    *,
    source_root: Path = SOURCE_ROOT,
    venv_root: Path = VENV_ROOT,
    installed_marker_path: Path = INSTALLED_MARKER_PATH,
) -> bool:
    """Return whether runtime may import the enhanced native module.

    This is the activation commit gate, not merely a status hint. A crash can
    happen after the atomic ``.so`` replacement but before the verified marker
    is published. In that window the extension is intentionally ignored and
    the bridge uses mandatory v1; only a marker whose source fingerprint and
    extension digest both match authorizes v2.
    """

    marker = read_installed_marker(path=installed_marker_path)
    fingerprint = str(marker.get("fingerprint") or "")
    digest = str(marker.get("extension_sha256") or "")
    name = str(marker.get("extension_name") or "")
    if not fingerprint or not digest or not name:
        return False
    try:
        if fingerprint != desired_fingerprint(source_root):
            return False
        extension = next(
            (path for path in installed_v2_extensions(venv_root) if path.name == name),
            None,
        )
        return bool(extension and extension_digest_matches(extension, digest))
    except (OSError, EnhancedAecError):
        return False


def _available(
    *,
    source_root: Path,
    venv_root: Path,
    machine: str,
) -> tuple[bool, str, str]:
    normalized_machine = machine.strip().lower()
    if normalized_machine not in {"aarch64", "arm64"}:
        return (
            False,
            "Enhanced echo cancellation is available on 64-bit Raspberry Pi installs.",
            "unsupported_architecture",
        )
    python_path = venv_root / "bin" / "python"
    if not python_path.is_file():
        return False, "The JTS Python runtime is not installed.", "runtime_missing"
    try:
        parse_target_manifest(source_root)
        desired_fingerprint(source_root)
    except (OSError, EnhancedAecError) as exc:
        return False, str(exc), "source_manifest_unavailable"
    return True, "", ""


def status(
    *,
    chip_aec_active: bool = False,
    service_active: bool = False,
    source_root: Path = SOURCE_ROOT,
    venv_root: Path = VENV_ROOT,
    intent_path: Path = INTENT_PATH,
    job_state_path: Path = JOB_STATE_PATH,
    installed_marker_path: Path = INSTALLED_MARKER_PATH,
    machine: str | None = None,
) -> dict[str, Any]:
    """Build the stable API/UI representation from persisted and live facts."""

    intent = read_intent(path=intent_path)
    if not install_profile_supports_enhanced_aec():
        return {
            "schema_version": SCHEMA_VERSION,
            "feature": FEATURE_ID,
            "state": "not_needed",
            "requested": bool(intent["requested"]),
            "installed": False,
            "current": False,
            "summary": "Enhanced echo cancellation is not used on this install",
            "detail": (
                "This streambox does not run the local voice, microphone, or "
                "echo-cancellation brain."
            ),
            "last_error": "",
            "desired_fingerprint": "",
            "installed_fingerprint": "",
            "engine": "v1",
            "engine_semantics": "verified_capability",
            "runtime_refresh": "",
            "unavailable_reason": "voice_brain_not_installed",
            "action": {
                "enabled": False,
                "label": "Install enhancement",
                "reason": (
                    "Enhanced echo cancellation is only used on a full "
                    "speaker install."
                ),
            },
        }
    job = read_job_state(path=job_state_path)
    marker = read_installed_marker(path=installed_marker_path)
    available, unavailable_detail, unavailable_reason = _available(
        source_root=source_root,
        venv_root=venv_root,
        machine=machine or platform.machine(),
    )
    try:
        wanted_fingerprint = desired_fingerprint(source_root) if available else ""
    except (OSError, EnhancedAecError):
        wanted_fingerprint = ""

    extensions = installed_v2_extensions(venv_root)
    marker_fingerprint = str(marker.get("fingerprint") or "")
    marker_digest = str(marker.get("extension_sha256") or "")
    marker_name = str(marker.get("extension_name") or "")
    matching_extension = next(
        (path for path in extensions if path.name == marker_name),
        None,
    )
    installed = bool(marker_fingerprint and matching_extension is not None)
    current = bool(
        installed
        and matching_extension is not None
        and wanted_fingerprint
        and marker_fingerprint == wanted_fingerprint
        and marker_digest
        and extension_digest_matches(matching_extension, marker_digest)
    )
    phase = str(job.get("phase") or "")
    queued_age = _age_seconds(job.get("updated_at"))
    queued_handoff = bool(
        phase == "queued"
        and queued_age is not None
        and queued_age <= _QUEUED_HANDOFF_GRACE_SECONDS
    )
    installing = bool(service_active or queued_handoff)
    interrupted = bool(
        not service_active
        and phase in _ACTIVE_PHASES
        and not queued_handoff
    )
    last_error = str(job.get("error") or "")

    if installing:
        state = "installing"
        summary = "Installing enhanced echo cancellation"
        detail = str(job.get("detail") or "Preparing the background installation.")
    elif chip_aec_active:
        state = "not_needed"
        summary = "Hardware echo cancellation is active"
        detail = (
            "This speaker is using microphone hardware echo cancellation, so "
            "the optional enhanced software engine is not needed."
        )
    elif not available:
        state = "unavailable"
        summary = "Enhanced echo cancellation is unavailable"
        detail = unavailable_detail
    elif current:
        state = "installed"
        summary = "Enhanced echo cancellation is installed"
        detail = "The verified enhanced AEC engine matches this JTS build."
    elif marker_fingerprint or phase == "stale":
        state = "stale"
        summary = "Enhanced echo cancellation needs an update"
        detail = (
            str(job.get("detail") or "")
            or "The installed engine does not match this JTS build."
        )
    elif (phase == "failed" or interrupted) and intent["requested"]:
        state = "failed"
        summary = "Enhanced echo cancellation was not installed"
        detail = (
            "The background installation was interrupted. Standard echo "
            "cancellation remains active; you can retry safely."
            if interrupted
            else (
                str(job.get("detail") or "")
                or "The background installation failed; standard echo cancellation remains active."
            )
        )
    else:
        state = "not_installed"
        summary = "Enhanced echo cancellation is optional"
        detail = (
            "Standard echo cancellation is already installed. This optional "
            "engine adds advanced tuning for difficult rooms and experiments."
        )

    action_enabled = bool(
        available
        and not chip_aec_active
        and not installing
        and not current
    )
    action_label = "Install enhancement"
    if state in {"failed", "stale"}:
        action_label = "Retry installation"
    action_reason = ""
    if not action_enabled:
        if chip_aec_active:
            action_reason = "Hardware echo cancellation is already active."
        elif installing:
            action_reason = "Installation is already running."
        elif current:
            action_reason = "The enhancement is already installed."
        elif not available:
            action_reason = unavailable_detail

    return {
        "schema_version": SCHEMA_VERSION,
        "feature": FEATURE_ID,
        "state": state,
        "requested": bool(intent["requested"]),
        "installed": installed,
        "current": current,
        "summary": summary,
        "detail": detail,
        "last_error": last_error,
        "desired_fingerprint": wanted_fingerprint,
        "installed_fingerprint": marker_fingerprint,
        # Capability availability, not observed bridge runtime. The live
        # bridge's stats/logs remain authoritative for which engine a running
        # process instantiated.
        "engine": "chip" if chip_aec_active else ("v2" if current else "v1"),
        "engine_semantics": "verified_capability",
        "runtime_refresh": str(job.get("runtime_refresh") or ""),
        "unavailable_reason": unavailable_reason,
        "action": {
            "enabled": action_enabled,
            "label": action_label,
            "reason": action_reason,
        },
    }


__all__ = [
    "ACTIVATION_PROOF_DIR",
    "EnhancedAecError",
    "FEATURE_ID",
    "INSTALL_LOCK_PATH",
    "INSTALLED_MARKER_PATH",
    "INTENT_PATH",
    "JOB_STATE_PATH",
    "SCHEMA_VERSION",
    "SOURCE_ROOT",
    "STATE_LOCK_PATH",
    "TARGET_MANIFEST_NAME",
    "VENV_ROOT",
    "desired_fingerprint",
    "extension_sha256",
    "install_profile_supports_enhanced_aec",
    "installed_v2_extensions",
    "parse_target_manifest",
    "read_installed_marker",
    "read_intent",
    "read_job_state",
    "request_install",
    "runtime_v2_verified",
    "status",
    "write_installed_marker",
    "write_job_state",
]
