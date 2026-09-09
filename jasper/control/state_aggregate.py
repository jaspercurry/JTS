# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State aggregation helpers for jasper-control."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from typing import Any, Callable, Sequence, TypeVar

from ..fanin.status import (
    FANIN_INPUT_SOURCE_DIRECT,
    fanin_usbsink_input,
)
from ..source_state import usbsink_direct_audible
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..log_event import log_event
from ..sound.camilla_yaml import BASE_CONFIG_PATH
from ..identity.speaker_name import read_state as _read_speaker_name_state
from ..platform.status_socket import (
    FANIN_STATUS_SOCKET,
    OUTPUTD_STATUS_SOCKET,
)
from ..volume_diagnostics import (
    build_volume_policy_snapshot,
    read_diagnostics as _read_volume_diagnostics,
)
from . import (
    debug_control,
    grouping_supervisor,
    measurement_hold,
    shairport_supervisor,
    system_supervisor,
)
from ..platform.uds import local_status_json, mux_socket_command, voice_socket_command

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

# Per-probe ceiling for the CamillaDSP /state probe: a wedged-but-listening
# DSP (TCP accepted, websocket read stalled) would otherwise hang the whole
# aggregate indefinitely. On timeout the probe fails soft to its all-None
# section, like its self-bounding siblings (voice 2 s, mux 1 s,
# fan-in/outputd 2 s).
_CAMILLA_PROBE_TIMEOUT_SEC = 2.0

# Bump when the key sets pinned in tests/test_wire_contracts.py change shape,
# so a consumer can branch on the number instead of probing for keys.
# See ADR-0270 for the thirteen keys this version names.
STATE_SCHEMA_VERSION = 4

# One deadline for the whole payload: the daemon fan-out and every section
# read spend from it. NOT a latency control — the normal path finishes well
# inside it. It converts an unbounded hang into a bounded, logged failure so
# the bounded-worker control plane is never parked on /state.
_STATE_AGGREGATE_BUDGET_SEC = 20.0

#: Deadline for the payload in flight; None means untimed. Retire it, the
#: pool below and _remaining() together if every section read stops blocking.
_STATE_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "jasper_state_deadline", default=None,
)

#: NOT the loop's default executor: asyncio.run joins that one at teardown, so
#: a wedged section read would hang the compute this deadline bounds. Threads
#: start on demand; max_workers caps what wedged reads can strand.
_STATE_READ_POOL = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="jasper-state-read",
)


def _remaining(deadline: float | None) -> float | None:
    """Seconds left on `deadline`, floored at zero; None when untimed."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


_VOICE_STATUS_DIRECT_KEYS = (
    "endpointer",
    "last_turn_ms",
    "turn_event_id",
    "wake_event_store",
    "input_audio",
    "spend_allowed",
    "usage_tracking_degraded",
    "connection_paused",
    "connection_error",
    "mic_muted",
    "measurement_active",
    "duck_active",
    "camilla_volume_locked",
    "music_dbfs",
    "last_wake_at",
    "idle_rms_dbfs",
    "input_last_above_floor_at",
    "wake_legs",
    "wake_legs_dead",
    "push_to_talk_only",
    "tool_packs",
    "silent_responses_session",
)
_VOICE_STATUS_NESTED_FIELDS = {
    "last_at": "barge_in_last_at",
    "count_session": "barge_in_count_session",
    "last_leg": "barge_in_last_leg",
}
_VOICE_STATUS_PUBLISHED_KEYS = (
    frozenset(_VOICE_STATUS_DIRECT_KEYS)
    | frozenset(_VOICE_STATUS_NESTED_FIELDS.values())
)
#: Not pulled through into `/state.voice`: either internal to the daemon, or
#: published at the TOP level of `/state` instead (`cues`).
_VOICE_STATUS_WITHHELD_KEYS = frozenset({
    "state",
    "input_ended",
    "assistant_output",
    "manual_mic_sources",
    "active_manual_mic_source",
    "barge_in_reconcile",
    "research",
    "cues",
})


def _ha_failed_status(error: str = "probe failed") -> dict[str, Any]:
    return {
        "configured": False,
        "connected": False,
        "url": "",
        "instance_name": None,
        "version": None,
        "error": error,
    }


def _usbsink_renderer_playing(fanin_status: dict[str, Any] | None) -> bool:
    """Whether the USB-sink DIRECT lane is audible. Feeds the ``usbsink``
    rung of :func:`_active_source`; false when fan-in exposes no DIRECT lane.
    """

    input_state = fanin_usbsink_input(fanin_status)
    if not input_state or input_state.get("source") != FANIN_INPUT_SOURCE_DIRECT:
        return False
    return bool(usbsink_direct_audible(fanin_status))


def _active_speaker_level_match_provisional(
    setup: dict[str, Any] | None,
) -> bool | None:
    """Whether the APPLIED active-speaker baseline's per-driver level match is a
    datasheet estimate rather than a phone measurement.

    Read from the readiness snapshot (`setup`) the caller already computed via
    `read_active_speaker_setup_status`, so `active_speaker_baseline_profile.json`
    has one reader here. The `status == "applied"` gate is load-bearing: the
    candidate only carries that status when it returns the persisted applied
    profile verbatim (see `build_baseline_profile_candidate`), so `provisional`
    then equals the on-disk value. Fail-soft: None when there is no applied
    active baseline (passive speaker, unreadable topology, or a superseded /
    not-yet-applied profile).
    """
    if not isinstance(setup, dict):
        return None
    profile = setup.get("baseline_profile")
    if not isinstance(profile, dict) or profile.get("status") != "applied":
        return None
    return bool(profile.get("provisional"))


def active_speaker_output_safety_snapshot(
    airplay_health: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the landing-page speaker-output safety state."""

    current = airplay_health.get("current") if isinstance(airplay_health, dict) else {}
    camilla = current.get("camilla") if isinstance(current, dict) else {}
    raw_path = camilla.get("config_path") if isinstance(camilla, dict) else None
    config_path = str(raw_path or "")
    setup = read_active_speaker_setup_status(
        active_config_path=config_path or None,
    )
    return {
        **setup,
        # Back-compat alias for the landing page's field name.
        "safety_muted": not bool(setup.get("volume_allowed")),
        "level_match_provisional": _active_speaker_level_match_provisional(setup),
        "source": "active_speaker.setup_status",
    }


def _same_config_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _sound_apply_target(last_apply: Any) -> str | None:
    if not isinstance(last_apply, dict):
        return None
    for key in ("active_config_path", "candidate_config_path"):
        value = last_apply.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _sound_runtime_status(
    sound_profile: dict[str, Any],
    active_config_path: str | None,
) -> dict[str, Any]:
    """Describe whether the desired sound profile is actually loaded.

    ``sound_profile["enabled"]`` is the persisted preference. The
    runtime truth is CamillaDSP's active config path, which can differ
    after rollback, install repair, or a manual Camilla reload. Keep the
    distinction explicit so status surfaces do not imply EQ is active
    when the daemon is running the flat outputd base config.
    """

    last_apply_path = _sound_apply_target(sound_profile.get("last_dsp_apply"))
    try:
        filter_count = int(sound_profile.get("filter_count") or 0)
    except (TypeError, ValueError):
        filter_count = 0
    desired_has_filters = bool(sound_profile.get("enabled")) and filter_count > 0
    runtime = {
        "active_config_path": active_config_path,
        "last_apply_config_path": last_apply_path,
        "matches_last_apply": None,
        "state": "unknown",
        "active": None,
        "warning": None,
    }
    if not active_config_path:
        return runtime

    if last_apply_path:
        runtime["matches_last_apply"] = _same_config_path(
            active_config_path,
            last_apply_path,
        )

    if _same_config_path(active_config_path, BASE_CONFIG_PATH):
        runtime["state"] = "base"
        runtime["active"] = not desired_has_filters
    elif runtime["matches_last_apply"] is True:
        runtime["state"] = "applied"
        runtime["active"] = True
    elif last_apply_path:
        runtime["state"] = "mismatch"
        runtime["active"] = False
    else:
        runtime["state"] = "custom"
        runtime["active"] = None

    if desired_has_filters and runtime["active"] is not True:
        runtime["warning"] = (
            "Desired sound profile is not the active CamillaDSP config."
        )
    return runtime


def _outputd_section(status: dict | None) -> dict | None:
    """jasper-outputd's STATUS body as every operator surface publishes it.

    The chip-reference writer's per-write ring is dropped (~25 KB of every
    response); its one consumer, jasper-aec-init, reads it off the socket
    directly. One shaper for /state and /system/snapshot (ADR-0233 rule 1).
    """
    if isinstance(status, dict):
        writer = status.get("reference_outputs", {})
        writer = writer.get("chip_ref_writer") if isinstance(writer, dict) else None
        if isinstance(writer, dict):
            writer.pop("recent_writes", None)
    return status


async def _outputd_status(
    *,
    local_status_json: Callable[..., Any] = local_status_json,
) -> dict | None:
    """Probe jasper-outputd's STATUS endpoint.

    Missing socket is fail-soft here so /state remains available while
    jasper-doctor owns the actionable cutover failure.
    """
    return _outputd_section(await local_status_json(OUTPUTD_STATUS_SOCKET))


async def _soft_read(
    section: str,
    reader: Callable[[], _T],
    *,
    exc: tuple[type[BaseException], ...] = (Exception,),
) -> _T | None:
    """One /state section read, off the loop and inside the payload deadline.

    None is the section's "unavailable": the reader raised, or the deadline
    passed first. A timed-out reader keeps running in its worker — nothing can
    stop a blocking call — but the response stops waiting on it. The timeout
    clause must precede ``exc``: TimeoutError is an OSError subclass, which
    several callers pass.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_STATE_READ_POOL, reader),
            _remaining(_STATE_DEADLINE.get()),
        )
    except asyncio.TimeoutError:
        log_event(
            logger, "state.section_timeout", section=section,
            level=logging.WARNING,
        )
        return None
    except exc:
        logger.exception("/state %s section read failed", section)
        return None


async def _soft_read_optional(
    section: str, snapshot: Callable[[], _T] | None,
) -> _T | None:
    """A section whose reader is absent when this daemon runs no sampler."""
    return None if snapshot is None else await _soft_read(section, snapshot)


def _read_persisted_volume() -> tuple[int | None, float | None]:
    """The persisted listening level and main volume, in that order."""
    from ..volume_coordinator import VolumeState
    from ..volume_persistence import VolumePersistence
    from ..volume_persistence import configured_path as volume_state_path

    record = VolumePersistence(volume_state_path()).load()
    if record is None:
        return None, None
    return (
        VolumeState.from_record(record).effective_percent,
        round(record.main_volume_db, 2)
        if math.isfinite(record.main_volume_db)
        else None,
    )


def _read_sound_profile() -> dict[str, Any]:
    from ..dsp_apply import last_dsp_apply_state
    from ..sound.profile import (
        build_sound_filters,
        estimate_headroom_db,
        load_profile,
    )
    from ..sound.settings import load_sound_settings, output_trim_db

    profile = load_profile()
    sound_settings = load_sound_settings()
    return {
        "enabled": profile.enabled,
        "curve_id": profile.curve_id,
        "simple_eq": profile.simple_eq.to_dict(),
        "parametric_band_count": len(profile.parametric_bands),
        "filter_count": len(build_sound_filters(profile)),
        "headroom_db": estimate_headroom_db(profile),
        "match_loudness": sound_settings.match_loudness,
        "headroom_trim_db": sound_settings.headroom_trim_db,
        "output_trim_db": output_trim_db(profile, sound_settings),
        "updated_at": profile.updated_at or None,
        "last_dsp_apply": last_dsp_apply_state(),
    }


def _spotify_playing() -> bool:
    from .. import librespot_state

    blob = librespot_state.read(librespot_state.configured_path())
    return bool(blob.get("playing", False))


def _active_source(
    *,
    voice_session: bool,
    audio_health: Mapping[str, Any] | None,
    mux_status: dict | None,
    spotify_playing: bool,
    airplay_playing: bool | None,
    usbsink_playing: bool,
) -> str:
    """Pick ``/state.active_source`` — the only derivation on the wire.

    The audio-health sampler's verdict wins whenever it has one, so it and
    ``audio_health.overall.active_source`` cannot name different sources in one
    response. It models music lanes only and answers None for "cannot confirm",
    so a voice session still leads.
    """
    overall = audio_health.get("overall") if isinstance(audio_health, Mapping) else None
    overall = overall if isinstance(overall, Mapping) else {}
    # The sampler keeps the last lane verbatim once its own sample goes stale
    # and says so with status `unknown`; that must not outrank a live mux.
    health_source = (
        None if overall.get("status") == "unknown"
        else overall.get("active_source")
    )

    mux_effective_source = None
    if isinstance(mux_status, dict):
        raw_selected = mux_status.get("selected_source")
        if isinstance(raw_selected, str):
            mux_effective_source = raw_selected
        else:
            raw_winner = mux_status.get("winner")
            if isinstance(raw_winner, str):
                mux_effective_source = raw_winner

    if voice_session:
        return "voice"
    if isinstance(health_source, str) and health_source:
        return health_source
    if mux_effective_source:
        return mux_effective_source
    if spotify_playing:
        return "spotify"
    if airplay_playing:
        return "airplay"
    if usbsink_playing:
        # `playing` is authoritative on both box shapes: solo reads the
        # bridge's RMS-gated flag, combo derives it from the fan-in DIRECT
        # lane's level (audible above the shared -60 dBFS gate), so a combo
        # box streaming silence reads false exactly like solo.
        return "usbsink"
    return "idle"


def _read_output_hardware() -> dict[str, Any] | None:
    from ..output_hardware import load_state

    hardware = load_state()
    return hardware.to_dict() if hardware is not None else None


def _round_db(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, 2)


def _round_levels(levels: Sequence[float] | None) -> list[float | None] | None:
    """Every channel the running graph carries, not just the front pair.

    An active-crossover box plays four or more physical outputs, and a
    stereo readout would hide entire drivers. The width comes from
    CamillaDSP.
    """
    if levels is None:
        return None
    return [_round_db(v) for v in levels]


async def _camilla_status(*, host: str, port: int) -> dict[str, Any]:
    from ..camilla import CamillaController

    status: dict[str, Any] = {
        "main_volume_db": None,
        "playback_rms_dbfs": None,
        "playback_peak_dbfs": None,
        "clipped_samples": None,
        "active_config_path": None,
    }

    async def _no_config_path() -> None:
        return None

    try:
        cam = CamillaController(host=host, port=port)
        config_path_probe = (
            cam.get_config_file_path(best_effort=True)
            if hasattr(cam, "get_config_file_path")
            else _no_config_path()
        )
        vol, rms, peak, clipped, active_config_path = await asyncio.wait_for(
            asyncio.gather(
                cam.get_volume_db(best_effort=True),
                cam.get_playback_rms_all(best_effort=True),
                cam.get_playback_peak_all(best_effort=True),
                cam.get_clipped_samples(best_effort=True),
                config_path_probe,
            ),
            timeout=_CAMILLA_PROBE_TIMEOUT_SEC,
        )
        status["main_volume_db"] = _round_db(vol)
        status["playback_rms_dbfs"] = _round_levels(rms)
        status["playback_peak_dbfs"] = _round_levels(peak)
        status["clipped_samples"] = clipped
        status["active_config_path"] = active_config_path
        return status
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "state.camilla_probe_failed",
            error=exc,
            level=logging.DEBUG,
        )
        return status


async def _voice_status(cmd: Callable[..., Any], socket_path: str) -> dict | None:
    try:
        return await cmd(socket_path, "STATUS", timeout=2.0)
    except (OSError, RuntimeError):
        return None


def _ha_status(snapshot: Callable[[], dict[str, Any]]) -> dict:
    """HA status for /system/snapshot via the child-process cache boundary.

    The cache reads the wizard env-file signature fresh, so saves are
    reflected without restarting jasper-control, while HA/httpx imports
    stay in the short-lived probe child instead of the control daemon.
    """
    try:
        return snapshot()
    except Exception:  # noqa: BLE001
        logger.exception("home assistant state snapshot failed")
        return _ha_failed_status()


async def _mux_status(cmd: Callable[..., Any]) -> dict | None:
    try:
        return await cmd("STATUS", timeout=1.0)
    except (OSError, RuntimeError, ValueError):
        return None


def _speaker_name_section() -> dict[str, Any]:
    """The display-name record every operator surface publishes.

    Named fields rather than the dataclass's ``__dict__``, so a new field
    reaches a surface by decision (ADR-0233 rule 1).
    """
    state = _read_speaker_name_state()
    return {"name": state.name, "room": state.room, "source": state.source}


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    voice_socket_command: Callable[..., Any] = voice_socket_command,
    mux_socket_command: Callable[..., Any] = mux_socket_command,
    local_status_json: Callable[..., Any] = local_status_json,
    airplay_playing_snapshot: Callable[[], bool | None] | None = None,
    audio_health_snapshot: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build the whole GET /state payload — every key a client receives.

    Thirteen keys, all of them this daemon's own posture (ADR-0270): its
    in-process holds, its live CamillaDSP probe, and the daemon STATUS bodies
    it passes through. Every other fact a consumer wants it reads from the
    module that owns it.

    Each section fails soft: voice unreachable, Camilla restarting or a read
    that outlives the deadline reports null in that section instead of erroring
    out the whole response. Slow probes fan out in parallel.
    """
    from datetime import datetime, timezone

    from ..voice.provider_state import (
        read_active_provider_state,
        read_barge_in_enabled,
    )

    # Re-read the wizard-owned SSOT file fresh on every call: jasper-control
    # is NOT restarted on a provider switch (only jasper-voice is), so
    # os.environ here would pin the value to this daemon's start and show a
    # stale provider. ("", None) when unconfigured; never a guessed default.
    active_provider = read_active_provider_state()

    deadline = time.monotonic() + _STATE_AGGREGATE_BUDGET_SEC
    _STATE_DEADLINE.set(deadline)

    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(
                _camilla_status(host=camilla_host, port=camilla_port),
                _voice_status(voice_socket_command, voice_socket_path),
                local_status_json(FANIN_STATUS_SOCKET),
                _outputd_status(local_status_json=local_status_json),
                _mux_status(mux_socket_command),
            ),
            timeout=_remaining(deadline),
        )
    except asyncio.TimeoutError:
        # A probe blew past its own ceiling. Fail loud (the handler turns this
        # into a 502) rather than hang a bounded worker forever; the cheap
        # /healthz probe stays answerable so this can't manufacture a reboot.
        log_event(
            logger,
            "state.aggregate_timeout",
            budget_sec=_STATE_AGGREGATE_BUDGET_SEC,
            level=logging.WARNING,
        )
        raise
    camilla, voice, fanin, outputd, mux = gathered
    voice_status = voice or {}

    # One pass over every section read that depends on nothing still pending,
    # so they share the pool instead of each waiting out the one before it —
    # serial submission spends one worker and the deadline a read at a time.
    (
        volume_state, sound_profile, airplay_playing, audio_health,
        output_hardware_state,
    ) = await asyncio.gather(
        _soft_read("volume", _read_persisted_volume, exc=(OSError, ValueError)),
        _soft_read("sound_profile", _read_sound_profile),
        # The AirPlay health sampler's held MPRIS PlaybackStatus, so no
        # `busctl` runs per request (ADR-0233 rule 2). None when the sampler
        # is absent or has no sample yet; its interval bounds freshness.
        _soft_read_optional("airplay_playing", airplay_playing_snapshot),
        # The sampler's normalized health contract, read into the payload
        # rather than bolted on above it: `active_source` below comes out of
        # THIS object, so the two cannot drift.
        _soft_read_optional("audio_health", audio_health_snapshot),
        _soft_read("output_hardware", _read_output_hardware),
    )
    listening_level, persisted_main_volume_db = volume_state or (None, None)

    spotify_playing = _spotify_playing()
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            camilla.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Top-level aliases for consumers that need only the running truth
        # and do not want to parse the nested runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]

    voice_session = bool(voice_status) and voice_status.get("state") == "SESSION"
    active_source = _active_source(
        voice_session=voice_session,
        audio_health=audio_health,
        mux_status=mux,
        spotify_playing=spotify_playing,
        airplay_playing=airplay_playing,
        usbsink_playing=_usbsink_renderer_playing(fanin),
    )

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=camilla["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=mux,
        diagnostics=_read_volume_diagnostics(),
    )

    # Lazy import (mirrors read_active_provider_state above) so jasper-control
    # doesn't pull jasper.voice.* at module load.
    from ..mic_presence import read_mic_presence

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            "provider_status": active_provider.status,
            "provider_error": active_provider.detail or None,
            "session_active": voice_session,
            **{key: voice_status.get(key) for key in _VOICE_STATUS_DIRECT_KEYS},
            "barge_in": {
                "enabled": (
                    read_barge_in_enabled(active_provider.provider)
                    if active_provider.provider else False
                ),
                **{
                    field: voice_status.get(status_key)
                    for field, status_key in _VOICE_STATUS_NESTED_FIELDS.items()
                },
            },
            "reachable": voice is not None,
            # Disambiguates reachable:false: true means the AEC reconciler
            # parked voice for a missing microphone ("intentionally idle, no
            # mic", NOT "crashed"). jasper.mic_presence owns the rich record;
            # a consumer that wants it reads that module (ADR-0270).
            "parked_no_mic": read_mic_presence().parked,
        },
        "audio": {
            "main_volume_db": camilla["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": camilla["playback_rms_dbfs"],
            "playback_peak_dbfs": camilla["playback_peak_dbfs"],
            "clipped_samples": camilla["clipped_samples"],
            "camilla_active_config_path": camilla["active_config_path"],
            "sound": sound_profile,
            "output_hardware": output_hardware_state,
        },
        "active_source": active_source,
        # Fan-in's UDS STATUS snapshot, flat and unwrapped. null only when
        # the daemon/socket is unavailable.
        "fanin": fanin,
        # Final-output owner; jasper-doctor owns the actionable failure.
        "outputd": outputd,
        "source_selection": mux,
        # The three supervisors this process runs, and nothing else: every
        # other resilience fact has a module of its own that jasper-doctor
        # reads directly (ADR-0270).
        "resilience": {
            "shairport": shairport_supervisor.snapshot(),
            # Bonded-member runtime liveness: dac_content starvation watch
            # + snapcast binding read-repair. Off via
            # JASPER_GROUPING_SUPERVISOR=disabled.
            "grouping_supervisor": grouping_supervisor.snapshot(),
            # Userspace-liveness supervisor: probes sshd / our own HTTP /
            # /proc/loadavg every 30 s and clean-reboots after 3 consecutive
            # failures (rate-limited 1/24h). Off via
            # JASPER_SYSTEM_SUPERVISOR=disabled.
            "system_supervisor": system_supervisor.snapshot(),
        },
        # Which subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
        # AudioCueManager.snapshot() verbatim; never the cue/dynamic text.
        "cues": voice_status.get("cues"),
        # The open measurement window as this process sees it — an in-memory
        # read of its own self-expiring copy, not a probe. `held_for_s` is
        # what jasper-doctor's check_measurement_hold reads: `expires_in_s`
        # resets on every renewal and so can never reveal a stuck hold.
        "measurement": measurement_hold.snapshot(),
        # The normalized health contract /system/snapshot renders. null when
        # this daemon runs no sampler, so the key set is the same either way.
        # Its own `sampled_at` is what a consumer ages, not this response.
        "audio_health": audio_health,
    }
