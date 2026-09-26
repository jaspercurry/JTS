# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wake-corpus recorder's open session and its crash recovery.

Begin, load, unload and delete the session ``RecordingBackend`` appends
clips to, write its sidecar, and recover after a crash from the two markers
under the metadata dir: the active-session marker names the session a
restarted process reattaches, and the test-mode marker records that corpus
test mode stopped jasper-voice. These functions read and write the
backend's session fields under its state lock; the backend runs each
session transition inside its lifecycle transaction.
"""
from __future__ import annotations

import json
import logging
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    config_metadata,
    variant_metadata,
)
from jasper.atomic_io import atomic_write_json
from jasper.cli.wake_enroll import VOICE_UNIT
from jasper.log_event import log_event

from . import session_store
from .bridge_session import (
    build_session_audio_context,
    chip_aec_config_metadata,
    exit_corpus_test_mode,
)
from .capture_plan import CAPTURE_PLAN_STATE_SESSION, build_capture_plan
from .errors import StateError
from .runtime_probe import (
    BASE_LEGS,
    CORPUS_PROFILES,
    DTLN_LEG,
    PROFILE_CHIP_AEC_COMPARISON,
    PROFILE_STANDARD,
    RAW0_LEG,
    USB_DTLN_LEG,
    XVF_RAW0_DTLN_LEG,
    session_aec3_sweep_source,
)
from .session_store import METADATA_SCHEMA_VERSION, ClipMetadata

if TYPE_CHECKING:
    from .recording_backend import RecordingBackend

logger = logging.getLogger("jasper-wake-corpus-web")

ACTIVE_SESSION_MARKER = ".active_session.json"
# Crash-safety marker for corpus test mode. Entering test mode stops
# jasper-voice (the UDP ports must be free to record), so an operator
# who opens the recorder and just closes the tab would otherwise leave
# the speaker permanently deaf — the socket-activated web service idle-
# exits after 10 min and nothing restarts jasper-voice. This marker
# records "test mode stopped voice" on disk; backend startup (which the
# socket re-runs on the next /wake-corpus/ request after an idle exit)
# restores production audio if the marker is stale and no session is
# being resumed. Cleared on a clean test-mode exit.
TEST_MODE_MARKER = ".corpus_test_mode.json"

# How long after the active-session marker was written (on begin or load)
# a restarted backend still resumes its session. Set to 1 hour so a quick
# crash-and-restart picks up cleanly, but a session abandoned overnight
# doesn't surprise the operator the next day with "wait, why does the
# UI show clips from yesterday?"
RESUME_WINDOW_SEC = 3600.0

# How long after entering corpus test mode we treat the marker as
# abandoned and self-heal jasper-voice back on. Kept well under the
# jasper-web 10-min idle-exit window so that whenever the socket re-
# spawns the service (the next /wake-corpus/ request after the operator
# walked away), the marker is reliably stale and recovery fires. While a
# tab is open it polls /api/status every ~2 s, so the service never idle-
# exits and this never runs against a live session.
TEST_MODE_STALE_SEC = 300.0

# session_store.parse_session_data()'s keys that map 1:1 onto a
# RecordingBackend `_<key>` attribute of the same name.
_SESSION_STATE_KEYS = (
    "session_id", "member", "enabled_legs", "include_raw_mic_0",
    "include_dtln", "include_usb_mic", "include_usb_dtln",
    "include_xvf_raw0_dtln", "include_aec3_sweep", "corpus_profile",
    "chip_aec_config", "aec3_sweep_source", "aec3_sweep_variants",
    "aec3_sweep_config", "capture_plan", "audio_context",
)
# Subset of the above returned verbatim in _load_session_data's summary.
_SESSION_SUMMARY_KEYS = (
    "session_id", "member", "include_raw_mic_0", "include_dtln",
    "include_usb_mic", "include_usb_dtln", "include_xvf_raw0_dtln",
    "include_aec3_sweep", "corpus_profile", "aec3_sweep_source",
)


def default_enabled_legs(ports: dict[str, int]) -> tuple[str, ...]:
    """Session default: base production legs that exist in this process."""
    return tuple(leg for leg in BASE_LEGS if leg in ports)


def _active_session_marker_path(backend: RecordingBackend) -> Path:
    return backend._metadata_dir / ACTIVE_SESSION_MARKER


def _write_active_session_marker(backend: RecordingBackend) -> None:
    """Persist the session currently open for appending.

    Metadata files are historical artifacts. This marker is the
    narrow crash-recovery signal: if the web process dies while a
    session is open, startup can reattach; if the operator unloads
    or exits test mode cleanly, the marker is removed.
    """
    with backend._lock:
        session_id = backend._session_id
        member = backend._member
    if session_id is None:
        return
    backend._metadata_dir.mkdir(parents=True, exist_ok=True)
    path = _active_session_marker_path(backend)
    data = {
        "session_id": session_id,
        "member": member,
        "updated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
    }
    atomic_write_json(path, data)


def _clear_active_session_marker(backend: RecordingBackend) -> None:
    try:
        _active_session_marker_path(backend).unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("failed to clear active session marker: %s", e)


def _test_mode_marker_path(backend: RecordingBackend) -> Path:
    return backend._metadata_dir / TEST_MODE_MARKER


def note_test_mode_entered(backend: RecordingBackend) -> None:
    """Record that corpus test mode just stopped jasper-voice.

    The marker lets a later backend startup self-heal the speaker if
    the operator entered test mode and then walked away without
    exiting (see TEST_MODE_MARKER). Best-effort: a write failure must
    not block the operator from recording.
    """
    backend._metadata_dir.mkdir(parents=True, exist_ok=True)
    path = _test_mode_marker_path(backend)
    data = {
        "entered_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
    }
    try:
        atomic_write_json(path, data)
    except OSError as e:
        logger.warning("failed to write test-mode marker: %s", e)


def note_test_mode_exited(backend: RecordingBackend) -> None:
    try:
        _test_mode_marker_path(backend).unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("failed to clear test-mode marker: %s", e)


def _clear_session_state_locked(backend: RecordingBackend) -> None:
    backend._session_id = None
    backend._member = None
    backend._clips.replace_locked([])
    backend._include_raw_mic_0 = False
    backend._include_dtln = False
    backend._include_usb_mic = False
    backend._include_usb_dtln = False
    backend._include_xvf_raw0_dtln = False
    backend._include_aec3_sweep = False
    backend._corpus_profile = PROFILE_STANDARD
    backend._chip_aec_config = None
    backend._aec3_sweep_source = AEC3_SWEEP_SOURCE_XVF
    backend._aec3_sweep_variants = []
    backend._aec3_sweep_config = None
    backend._enabled_legs = default_enabled_legs(backend._ports)
    backend._capture_plan = None
    backend._audio_context = None
    backend._current_plan_conformance = None


def _load_session_data(
    backend: RecordingBackend, data: dict[str, Any],
) -> dict[str, Any]:
    try:
        parsed = session_store.parse_session_data(data, backend._ports)
        clips = [ClipMetadata(**c) for c in parsed["clips"]]
    except (KeyError, TypeError) as e:
        raise ValueError(f"session schema mismatch: {e}") from e
    with backend._lock:
        for key in _SESSION_STATE_KEYS:
            setattr(backend, f"_{key}", parsed[key])
        backend._clips.replace_locked(clips)
    return {
        **{key: parsed[key] for key in _SESSION_SUMMARY_KEYS},
        "clip_count": sum(1 for c in clips if not c.deleted),
        "enabled_legs": list(parsed["enabled_legs"]),
        "has_capture_plan": parsed["capture_plan"] is not None,
        "has_audio_context": parsed["audio_context"] is not None,
    }


def maybe_load_recent_session(
    backend: RecordingBackend, now: float | None = None,
) -> None:
    """Recover the marked active session after a server crash.

    Called automatically from `start()`. Safe to call multiple
    times (only triggers if no session is currently set).
    """
    with backend._lock:
        if backend._session_id is not None:
            return  # already have a session, nothing to recover
    if not backend._metadata_dir.is_dir():
        return

    now = now if now is not None else time.time()
    marker = _active_session_marker_path(backend)
    if not marker.is_file():
        return
    age = now - marker.stat().st_mtime
    if age > RESUME_WINDOW_SEC:
        logger.info(
            "skipping recovery: active session marker is %.0fs old "
            "(window=%.0fs)", age, RESUME_WINDOW_SEC,
        )
        _clear_active_session_marker(backend)
        return

    try:
        marker_data = json.loads(marker.read_text())
        session_id = str(marker_data["session_id"])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "recovery skipped: failed to read %s: %s", marker, e,
        )
        return
    except KeyError:
        logger.warning(
            "recovery skipped: %s lacks session_id", marker,
        )
        return

    saved = session_store.find_session(backend._metadata_dir, session_id)
    if saved is None:
        logger.warning(
            "recovery skipped: active session metadata missing for %s",
            session_id,
        )
        _clear_active_session_marker(backend)
        return

    target, data = saved
    try:
        result = _load_session_data(backend, data)
    except ValueError as e:
        logger.warning(
            "recovery skipped: failed to restore %s: %s", target, e,
        )
        return
    logger.info(
        "recovered active session %s for %s clips=%d legs=%s",
        result["session_id"], result["member"], result["clip_count"],
        ",".join(result["enabled_legs"]),
    )


def maybe_recover_stale_test_mode(
    backend: RecordingBackend, now: float | None = None,
) -> None:
    """Restore production audio after an abandoned test-mode session.

    Entering corpus test mode stops jasper-voice; a clean exit clears
    the marker and restarts it. If the operator instead walks away,
    the marker is left behind and the speaker stays deaf. On a later
    backend startup (the socket re-spawns the service on the next
    request after its idle exit) this restores production audio when
    the marker is stale and nothing is actively recording.

    Conservative by design — it does NOT tear down when:
      - a recording is in progress, or
      - a session was just resumed (operator is mid-corpus-session;
        voice must stay stopped), or
      - the marker is still fresh (tab open, operator working).
    """
    marker = _test_mode_marker_path(backend)
    if not marker.is_file():
        return
    with backend._lock:
        recording = backend._current is not None
        session_active = backend._session_id is not None
    if recording or session_active:
        return
    now = now if now is not None else time.time()
    age = now - marker.stat().st_mtime
    if age <= TEST_MODE_STALE_SEC:
        return
    log_event(
        logger,
        "wake_corpus.test_mode_recover",
        age=f"{age:.0f}s",
        note=(
            f"stale={TEST_MODE_STALE_SEC:.0f}s: "
            f"restoring production audio + restarting {VOICE_UNIT}"
        ),
        level=logging.WARNING,
    )
    try:
        exit_corpus_test_mode()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as e:
        # Leave the marker so a later startup retries the recovery;
        # never crash the recorder over a failed restart.
        log_event(
            logger,
            "wake_corpus.test_mode_recover_failed",
            error=e,
            level=logging.WARNING,
        )
        return
    note_test_mode_exited(backend)


def begin_session(
    backend: RecordingBackend,
    member: str,
    corpus_profile: str = PROFILE_STANDARD,
    include_raw_mic_0: bool = False,
    include_dtln: bool = True,
    include_usb_mic: bool = False,
    include_usb_dtln: bool = False,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
    capture_plan: dict[str, Any] | None = None,
) -> str:
    """Open a fresh recording session. Resets the in-memory clip
    list (existing on-disk WAVs are untouched).

    `include_raw_mic_0` (default False) — when True, clips in this
    session also capture the truly-raw mic 0 leg (chip channel 2)
    into `aec_raw0_<condition>/`. Per-session, not per-clip, so
    downstream tools can rely on session-wide consistency.

    `include_dtln` (default True) — when True and the recorder has
    a DTLN port configured, clips capture the XVF raw-through-DTLN
    comparison leg.

    `include_usb_mic` (default False) — when True, clips also
    capture the corpus-only reference + cheap USB mic legs. These
    require matching bridge env flags to be enabled, otherwise the
    UDP captures will simply have no audio to write.

    `include_usb_dtln` (default False) — when True, clips capture
    the cheap USB raw-through-DTLN leg. The bridge must be started
    with JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1 for packets to arrive.

    `include_aec3_sweep` (default False) — when True, clips also
    capture the bounded same-utterance AEC3 tuning variants emitted
    by jasper-aec-bridge. These are pilot/tuning legs, not
    production wake inputs.

    `aec3_sweep_source` selects which raw mic feeds those variants.
    New sessions default to the cheap USB mic so one utterance yields
    USB baseline + three USB AEC3 variants while retaining the XVF
    baseline leg for comparison.

    Returns the new session_id (UTC timestamp).
    """
    backend._refuse_if_muted("begin_session")
    safe_member = "".join(c for c in member.lower() if c.isalnum() or c == "_")
    if not safe_member:
        raise ValueError(f"member name has no usable chars: {member!r}")
    if corpus_profile not in CORPUS_PROFILES:
        raise ValueError(f"unknown corpus profile: {corpus_profile!r}")
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        include_raw_mic_0 = True
        include_aec3_sweep = False
        sweep_source = AEC3_SWEEP_SOURCE_XVF
    else:
        sweep_source = (
            session_aec3_sweep_source(aec3_sweep_source)
            if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
        )
    effective_include_usb_mic = include_usb_mic or (
        include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
    )
    with backend._lock:
        if backend._current is not None:
            raise StateError(
                "can't begin session: recording in progress",
            )
        # session_id = UTC second-resolution timestamp + a 4-hex
        # suffix. The suffix avoids a collision when an operator
        # (or a test) calls begin_session() twice within the same
        # second — without it, two sessions would share both the
        # in-memory id AND the on-disk metadata filename, and the
        # second would silently overwrite the first.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if capture_plan is None:
            capture_plan = build_capture_plan(
                backend._ports,
                corpus_profile=corpus_profile,
                include_raw_mic_0=include_raw_mic_0,
                include_dtln=include_dtln,
                include_usb_mic=effective_include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=sweep_source,
                include_bridge_readiness=True,
                include_runtime_profile=True,
                plan_state=CAPTURE_PLAN_STATE_SESSION,
            )
        enabled_legs = tuple(
            str(leg) for leg in capture_plan.get("selected_legs", [])
            if str(leg) in backend._ports
        )
        sweep_variants = (
            variant_metadata(input_source=sweep_source)
            if include_aec3_sweep else []
        )
        sweep_config = (
            config_metadata(input_source=sweep_source)
            if include_aec3_sweep else None
        )
        session_id = f"{ts}-{secrets.token_hex(2)}"
        chip_config = (
            chip_aec_config_metadata()
            if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
        )
        backend._session_id = session_id
        backend._member = safe_member
        backend._clips.replace_locked([])
        backend._include_raw_mic_0 = RAW0_LEG in enabled_legs
        backend._include_dtln = DTLN_LEG in enabled_legs
        backend._include_usb_mic = effective_include_usb_mic
        backend._include_usb_dtln = USB_DTLN_LEG in enabled_legs
        backend._include_xvf_raw0_dtln = XVF_RAW0_DTLN_LEG in enabled_legs
        backend._include_aec3_sweep = include_aec3_sweep
        backend._corpus_profile = corpus_profile
        backend._chip_aec_config = chip_config
        backend._aec3_sweep_source = sweep_source
        backend._aec3_sweep_variants = sweep_variants
        backend._aec3_sweep_config = sweep_config
        backend._enabled_legs = enabled_legs
        backend._capture_plan = capture_plan
        backend._audio_context = None
    audio_context = build_session_audio_context(
        corpus_profile=corpus_profile,
        enabled_legs=enabled_legs,
        ports=backend._ports,
        include_raw_mic_0=RAW0_LEG in enabled_legs,
        include_dtln=DTLN_LEG in enabled_legs,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=USB_DTLN_LEG in enabled_legs,
        include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
        chip_aec_config=chip_config,
        capture_plan=capture_plan,
    )
    with backend._lock:
        if backend._session_id == session_id:
            backend._audio_context = audio_context
    backend._metadata_dir.mkdir(parents=True, exist_ok=True)
    save_metadata(backend)  # write the per-session flag before clips arrive
    _write_active_session_marker(backend)
    return session_id


def save_metadata(backend: RecordingBackend) -> None:
    """Atomic-rewrite the session JSON sidecar. Called after every
    clip write + delete so the file on disk always reflects the
    current state (resilient to a server crash mid-session)."""
    with backend._lock:
        if backend._session_id is None:
            return
        path = session_store.session_metadata_path(
            backend._metadata_dir, backend._member, backend._session_id,
        )
        data = {
            "metadata_schema_version": METADATA_SCHEMA_VERSION,
            "session_id": backend._session_id,
            "member": backend._member,
            "ports": backend._ports,
            "include_raw_mic_0": backend._include_raw_mic_0,
            "include_dtln": backend._include_dtln,
            "include_usb_mic": backend._include_usb_mic,
            "include_usb_dtln": backend._include_usb_dtln,
            "include_xvf_raw0_dtln": backend._include_xvf_raw0_dtln,
            "include_aec3_sweep": backend._include_aec3_sweep,
            "corpus_profile": backend._corpus_profile,
            "chip_aec_config": backend._chip_aec_config,
            "aec3_sweep_source": backend._aec3_sweep_source,
            "aec3_sweep_variants": list(backend._aec3_sweep_variants),
            "aec3_sweep_config": backend._aec3_sweep_config,
            "enabled_legs": list(backend._enabled_legs),
            "capture_plan": backend._capture_plan,
            "audio_context": backend._audio_context,
            "clips": backend._clips.to_json_locked(),
        }
    session_store.write_metadata_atomic(path, data)


def load_session(
    backend: RecordingBackend, session_id: str,
) -> dict[str, Any]:
    with backend._lock:
        if backend._current is not None or backend._starting_clip_id is not None:
            raise StateError(
                "can't load session: recording in progress",
            )
    saved = session_store.find_session(backend._metadata_dir, session_id)
    if saved is None:
        raise ValueError(f"session not found: {session_id}")

    _, data = saved
    try:
        result = _load_session_data(backend, data)
    except ValueError as e:
        raise ValueError(
            f"session {session_id} schema mismatch: {e}",
        ) from e
    _write_active_session_marker(backend)
    logger.info(
        "loaded session %s for %s with %d clip(s) include_raw_mic_0=%s "
        "include_dtln=%s include_usb_mic=%s include_usb_dtln=%s "
        "include_aec3_sweep=%s aec3_sweep_source=%s legs=%s",
        session_id, result["member"], result["clip_count"],
        result["include_raw_mic_0"], result["include_dtln"],
        result["include_usb_mic"], result["include_usb_dtln"],
        result["include_aec3_sweep"], result["aec3_sweep_source"],
        ",".join(result["enabled_legs"]),
    )
    return result


def unload_session(backend: RecordingBackend) -> str | None:
    with backend._lock:
        if backend._current is not None or backend._starting_clip_id is not None:
            raise StateError(
                "can't unload session: recording in progress",
            )
        session_id = backend._session_id
        _clear_session_state_locked(backend)
    _clear_active_session_marker(backend)
    if session_id is not None:
        logger.info("unloaded session %s", session_id)
    return session_id


def delete_session(
    backend: RecordingBackend, session_id: str,
) -> dict[str, int]:
    with backend._lock:
        if backend._current is not None or backend._starting_clip_id is not None:
            raise StateError(
                "can't delete session: recording in progress",
            )
    saved = session_store.find_session(backend._metadata_dir, session_id)
    if saved is None:
        raise ValueError(f"session not found: {session_id}")

    target, data = saved
    wavs_deleted, wavs_missing = session_store.delete_session_files(target, data)

    # If we just deleted the in-memory active session, clear state.
    with backend._lock:
        if backend._session_id == session_id:
            _clear_session_state_locked(backend)
            _clear_active_session_marker(backend)
    logger.info(
        "deleted session %s: %d wavs removed, %d missing",
        session_id, wavs_deleted, wavs_missing,
    )
    return {"wavs_deleted": wavs_deleted, "wavs_missing": wavs_missing}
