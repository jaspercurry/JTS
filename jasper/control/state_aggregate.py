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
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple, Sequence, TypeVar

from .. import identity_state
from ..accessories import status as accessory_status
from ..memory_policy import disk_usage
from ..fanin.status import (
    FANIN_INPUT_SOURCE_DIRECT,
    fanin_usbsink_input,
)
from ..source_state import (
    usbsink_direct_audible,
    usbsink_direct_muted,
    usbsink_direct_rms_dbfs,
)
from ..usbgadget import (
    DEFAULT_UDC_CLASS_DIR,
    network_wanted,
    udc_host_connected,
)
from ..usb_network import (
    DEFAULT_PENDING_PATH as USB_NETWORK_PENDING_PATH,
    IPv4Observation,
    IPv4ObservationState,
    UsbNetworkPlanError,
    attest_plan as attest_usb_network_plan,
    load_plan as load_usb_network_plan,
    observe_ipv4_cidr,
)
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom import cascade_timeline
from ..multiroom.state import read_grouping_state
from ..transit.state import read_state as read_transit_state
from ..log_event import log_event
from ..speaker_name import read_state as _read_speaker_name_state
from ..route_latency.status_socket import (
    FANIN_STATUS_SOCKET,
    OUTPUTD_STATUS_SOCKET,
)
from .. import outputd_failure_reconcile_state
from ..volume_diagnostics import (
    build_volume_policy_snapshot,
    read_diagnostics as _read_volume_diagnostics,
)
from . import (
    bootloop_guard_state,
    camilla_recover_state,
    debug_control,
    grouping_supervisor,
    measurement_hold,
    shairport_supervisor,
    system_supervisor,
    transport_park,
    usb_gadget_forensics,
)
from .aec_endpoints import _aec_full_status
from .uds import _local_status_json, _mux_socket_command, _voice_socket_command

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

OUTPUTD_BASE_CAMILLA_CONFIG = "/etc/camilladsp/outputd-cutover.yml"

# Per-probe ceiling for the CamillaDSP /state probe: a wedged-but-listening
# DSP (TCP accepted, websocket read stalled) would otherwise hang the whole
# aggregate indefinitely. On timeout the probe fails soft to its all-None
# section, like its self-bounding siblings (voice 2 s, mux 1 s,
# fan-in/outputd 2 s).
_CAMILLA_PROBE_TIMEOUT_SEC = 2.0

# Bump when the key sets pinned in tests/test_wire_contracts.py change shape,
# so a consumer can branch on the number instead of probing for keys.
# See ADR-0233 rule 2.
STATE_SCHEMA_VERSION = 3

# One deadline for the whole payload: the daemon fan-out and every section
# read spend from it. NOT a latency control — the normal path finishes well
# inside it, with HA's cached network probe (~8 s worst case) the slow outlier.
# It converts an unbounded hang into a bounded, logged failure so the
# bounded-worker control plane is never parked on /state.
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
_default_ha_status_cache: Any | None = None

_VOICE_STATUS_DIRECT_KEYS = (
    "endpointer",
    "last_turn_ms",
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
_VOICE_STATUS_WITHHELD_KEYS = frozenset({
    "state",
    "input_ended",
    "assistant_output",
    "manual_mic_sources",
    "active_manual_mic_source",
    "barge_in_reconcile",
    "research",
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


def _default_ha_status_snapshot() -> dict[str, Any]:
    """Child-process HA status snapshot for direct state-aggregate callers."""

    global _default_ha_status_cache
    if _default_ha_status_cache is None:
        from .ha_status_cache import HomeAssistantStatusCache

        _default_ha_status_cache = HomeAssistantStatusCache()
    return _default_ha_status_cache.snapshot()


def _build_usbsink_renderer_state(
    fanin_status: dict[str, Any] | None,
    *,
    host_connected: bool,
) -> dict[str, Any] | None:
    """Build ``/state.renderers.usbsink`` from its two live owners.

    Fan-in's identity-bound DIRECT lane owns activity, level, and mix-mute.
    ConfigFS/UDC sysfs owns host connection.  The section is absent when fan-in
    does not expose the DIRECT lane, preserving ``null == off/unavailable``
    without a copied state file or a resident compatibility daemon.
    """

    input_state = fanin_usbsink_input(fanin_status)
    if not input_state or input_state.get("source") != FANIN_INPUT_SOURCE_DIRECT:
        return None
    return {
        "combo": True,
        "playing": usbsink_direct_audible(fanin_status),
        # Compatibility fields retained for lightweight dashboard consumers.
        # Fan-in's `muted` is the actual arbitration state; there is no second
        # bridge process with an independent preemption state or timestamp.
        "preempted": False,
        "muted": usbsink_direct_muted(fanin_status),
        "host_connected": bool(host_connected),
        "rms_dbfs": usbsink_direct_rms_dbfs(fanin_status),
        "updated_at": None,
    }


def _conversation_history_state() -> dict[str, Any] | None:
    """Project conversation_history.health() onto /state.chat's wire shape.

    ``None`` when capture is on but the store could not be read at all —
    the household expects data and none can be shown, distinct from the
    zeroed dict below for capture never having been turned on.
    """
    from ..conversation_history import health

    info = health()
    if info["available"] and info["turn_count"] is not None:
        return {
            "capture_enabled": info["capture_enabled"],
            "turn_count": info["turn_count"],
            "last_write_age_seconds": info["last_write_age_seconds"],
            "retention": info["retention"],
        }
    if info["capture_enabled"]:
        return None
    return {
        "capture_enabled": False,
        "turn_count": None,
        "last_write_age_seconds": None,
        "retention": info["retention"],
    }


def _research_state(
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read privacy-safe async-research state."""
    from ..research.state import snapshot

    return snapshot(runtime=runtime)


def _active_speaker_parked_snapshot() -> dict[str, Any]:
    """Whether the speaker is PARKED silent for incomplete speaker setup.

    An unconfigured topology, or a roleful/protected topology without its
    startup graph, is seeded a proven-silent parked graph so the deploy can
    complete. Nothing is audible until the household saves the next valid
    layout. Two keys only — the config path is already in ``/state.audio``.

    Keyed on the persisted STATEFILE, not on the live CamillaDSP path, so this
    agrees with the two other surfaces that report the state
    (``jasper-doctor``'s ``active speaker runtime graph`` and
    ``audio_health._parked_graph_transport``): with CamillaDSP down the live
    path is empty, and a parked box would read unparked. ``detail`` names only
    the exits reachable on this DAC.

    Needs no guard of its own: ``read_camilla_statefile_config_path`` returns
    None on any read problem and ``active_graph_is_parked`` is total.
    """
    from ..active_speaker.environment import read_camilla_statefile_config_path
    from ..active_speaker.runtime_contract import (
        active_graph_is_parked,
        parked_muted_exits,
    )
    from ..audio_runtime_plan import DEFAULT_CAMILLA_STATEFILE_PATH
    from ..output_topology import OutputTopologyError, load_output_topology_strict

    config_path = read_camilla_statefile_config_path(DEFAULT_CAMILLA_STATEFILE_PATH)
    parked = active_graph_is_parked(config_path)
    if not parked:
        return {"parked": False, "detail": None}
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        return {
            "parked": True,
            "detail": "saved speaker layout is unavailable or invalid; run jasper-doctor",
        }
    return {"parked": True, "detail": parked_muted_exits(topology)}


def _disk_snapshot(path: str = "/") -> dict[str, Any] | None:
    """Root-filesystem fullness for /state.resilience — fail-soft.

    Returns ``{path, percent_used, free_gib, total_gib}``, or ``None`` when the
    filesystem cannot be measured (non-POSIX dev host, statvfs failure,
    zero-sized). jasper-doctor's ``check_disk_space`` owns the actionable
    warn/fail thresholds."""
    try:
        usage = disk_usage(path)
    except Exception:  # noqa: BLE001
        logger.debug("disk snapshot read failed", exc_info=True)
        return None
    if usage is None or usage.total_bytes <= 0:
        return None
    gib = 1024 ** 3
    return {
        "path": usage.path,
        "percent_used": int(usage.percent_used),
        "free_gib": round(usage.free_bytes / gib, 1),
        "total_gib": round(usage.total_bytes / gib, 1),
    }


USB_NETWORK_IFACE = "usb0"


def _usb_network_snapshot() -> dict[str, Any]:
    """USB management-network summary for /state — fail-soft, uncached.

    ``enabled`` reflects the kill-switch intent, not composition —
    jasper-doctor's check_usbgadget_composition/check_usbnet_* own the
    composed-vs-intent mismatch story. ``iface_present``/``carrier`` are read
    fresh from ``/sys/class/net/usb0`` every call, never cached;
    ``carrier=False`` (or ``iface_present=False``) is the normal "nothing
    plugged in" state, not an error. The observed address is read from the
    interface while desired address/subnet/version/fingerprint come only from
    the validated installer-owned plan; a missing or corrupt plan reports those
    as null rather than fabricating an address."""
    enabled = network_wanted()
    iface_root = Path("/sys/class/net") / USB_NETWORK_IFACE
    iface_present = False
    carrier = False
    try:
        iface_present = iface_root.is_dir()
        if iface_present:
            carrier = (iface_root / "carrier").read_text().strip() == "1"
    except OSError:
        logger.debug("usb_network sysfs read failed", exc_info=True)
    observation = (
        observe_ipv4_cidr(USB_NETWORK_IFACE)
        if iface_present
        else IPv4Observation(IPv4ObservationState.ABSENT)
    )
    observed_cidr = observation.cidr
    observed_address = observed_cidr.split("/", 1)[0] if observed_cidr else None
    try:
        plan = attest_usb_network_plan(load_usb_network_plan())
    except UsbNetworkPlanError:
        plan = None
        logger.debug("usb_network plan read failed", exc_info=True)
    try:
        migration_pending = USB_NETWORK_PENDING_PATH.exists()
    except OSError:
        migration_pending = False
    return {
        "enabled": enabled,
        "iface_present": iface_present,
        "carrier": carrier,
        "address": observed_address,
        "cidr": observed_cidr,
        "observation_status": observation.state.value,
        "observation_error": observation.error,
        "desired_address": plan.device_address if plan else None,
        "subnet": plan.subnet if plan else None,
        "plan_version": plan.version if plan else None,
        "identity_fingerprint": plan.identity_fingerprint if plan else None,
        "migration_pending": migration_pending,
    }


def _multiroom_cascade_snapshot() -> dict[str, Any] | None:
    try:
        return cascade_timeline.snapshot()
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.debug("multiroom cascade timeline snapshot failed", exc_info=True)
        return None


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

    if _same_config_path(active_config_path, OUTPUTD_BASE_CAMILLA_CONFIG):
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
    local_status_json: Callable[..., Any] = _local_status_json,
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


def _spotify_state() -> dict[str, Any]:
    from .. import librespot_state

    blob = librespot_state.read(librespot_state.configured_path())
    return {
        "playing": bool(blob.get("playing", False)),
        "track_id": blob.get("track_id"),
        "uri": blob.get("uri"),
        "session_active": bool(blob.get("session_active", False)),
    }


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


def _read_audition_state() -> dict[str, Any] | None:
    """Reduced-graph audition record for ``/state.audition``.

    Non-null means somebody is auditioning a reduced graph, so every other
    reading of this speaker's sound is about THAT graph. ``stale`` marks a
    record whose owner died without restoring the applied graph.
    """
    from ..active_speaker.audition import read_audition_state

    state = read_audition_state()
    if state is None:
        return None
    state = dict(state)
    state["stale"] = float(state.get("deadline_at") or 0.0) <= time.time()
    return state


def _read_bass_extension() -> dict[str, Any] | None:
    from ..bass_extension.profile import bass_extension_state_summary

    return bass_extension_state_summary()


def _read_output_hardware() -> dict[str, Any] | None:
    from ..output_hardware import load_state

    hardware = load_state()
    return hardware.to_dict() if hardware is not None else None


def _read_tool_catalog() -> dict[str, Any]:
    from ..tool_catalog_view import summary

    return summary()


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


def _ha_status(snapshot: Callable[[], dict[str, Any]] | None) -> dict:
    """HA status for /state via the child-process cache boundary.

    The cache reads the wizard env-file signature fresh, so saves are
    reflected without restarting jasper-control, while HA/httpx imports
    stay in the short-lived probe child instead of the control daemon.
    """
    read = snapshot or _default_ha_status_snapshot
    try:
        return read()
    except Exception:  # noqa: BLE001
        logger.exception("home assistant state snapshot failed")
        return _ha_failed_status()


async def _mux_status(cmd: Callable[..., Any]) -> dict | None:
    try:
        return await cmd("STATUS", timeout=1.0)
    except (OSError, RuntimeError, ValueError):
        return None


async def _aec_status(full_status: Callable[[], dict]) -> dict | None:
    """Additive mirror of GET /aec for one-shot /state consumers."""
    return await _soft_read("aec", full_status)


class _Probes(NamedTuple):
    """Field order IS the daemon gather's order; the last two are passed by
    name — ``aec`` reads a file and forks, so it runs with the section reads.
    """

    camilla: dict[str, Any]
    voice: dict | None
    fanin: dict | None
    outputd: dict | None
    mux: dict | None
    aec: dict | None
    ha_status: dict[str, Any]


def _speaker_name_section() -> dict[str, Any]:
    """The display-name record every operator surface publishes.

    Named fields rather than the dataclass's ``__dict__``, so a new field
    reaches a surface by decision (ADR-0233 rule 1).
    """
    state = _read_speaker_name_state()
    return {"name": state.name, "room": state.room, "source": state.source}


def _stamp_observed_at(
    payload: dict[str, Any], *, read_at: float,
) -> dict[str, Any]:
    """Say when each section's facts were observed. Epoch seconds (#4197).

    A section a sampler produced carries that sampler's own ``sampled_at`` —
    what a consumer must age is the observation, not this response. Everything
    else carries the time this response read it.

    Nested ``audio.output_hardware.observed_at`` is an ISO string owned by the
    hardware lane (#4027); this top-level stamp is epoch seconds.
    """
    return {
        key: (
            section if not isinstance(section, dict) else {
                **section,
                # Epoch magnitude: a monotonic clock or a bool never reaches
                # it, so a daemon body cannot retarget the stamp.
                "observed_at": (
                    section["sampled_at"]
                    if isinstance(section.get("sampled_at"), float)
                    and section["sampled_at"] > 1e9
                    else read_at
                ),
            }
        )
        for key, section in payload.items()
    }


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    voice_socket_command: Callable[..., Any] = _voice_socket_command,
    mux_socket_command: Callable[..., Any] = _mux_socket_command,
    local_status_json: Callable[..., Any] = _local_status_json,
    aec_full_status: Callable[[], dict] = _aec_full_status,
    read_transit_state_func: Callable[[], dict] = read_transit_state,
    ha_status_snapshot: Callable[[], dict[str, Any]] | None = None,
    airplay_playing_snapshot: Callable[[], bool | None] | None = None,
    transport_park_snapshot: Callable[[], dict[str, Any]] = transport_park.snapshot,
    service_states_snapshot: (
        Callable[[], dict[str, dict[str, Any]]] | None
    ) = None,
    audio_health_snapshot: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build the whole GET /state payload — every key a client receives.

    Each section fails soft: voice unreachable, Camilla restarting or a read
    that outlives the deadline reports null in that section instead of erroring
    out the whole response. Slow probes fan out in parallel.
    """
    from datetime import datetime, timezone

    from ..voice.provider_state import (
        read_active_model_from_env_files,
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

    ha_status = _ha_status(ha_status_snapshot)
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

    # One pass over every section read that depends on nothing else, so they
    # share the pool instead of each waiting out the one before it — serial
    # submission spends one worker and the deadline a read at a time.
    (
        volume_state, sound_profile, airplay_playing, aec_status, audio_health,
        usb_forensics, audition_state, bass_extension_state, transit_state,
        output_hardware_state, service_states, tools_state, chat_state,
    ) = await asyncio.gather(
        _soft_read("volume", _read_persisted_volume, exc=(OSError, ValueError)),
        _soft_read("sound_profile", _read_sound_profile),
        # The AirPlay health sampler's held MPRIS PlaybackStatus, so no
        # `busctl` runs per request (ADR-0233 rule 2). None when the sampler
        # is absent or has no sample yet; its interval bounds freshness.
        _soft_read_optional("airplay_playing", airplay_playing_snapshot),
        _aec_status(aec_full_status),
        # The sampler's normalized health contract, read into the payload
        # rather than bolted on above it: `active_source` below comes out of
        # THIS object, so the two cannot drift.
        _soft_read_optional("audio_health", audio_health_snapshot),
        _soft_read("usb_gadget_forensics", usb_gadget_forensics.snapshot),
        _soft_read(
            "audition", _read_audition_state,
            exc=(ImportError, OSError, RuntimeError, TypeError, ValueError),
        ),
        _soft_read(
            "bass_extension", _read_bass_extension,
            exc=(
                ImportError, OSError, RuntimeError, TypeError, ValueError,
                KeyError, AttributeError,
            ),
        ),
        _soft_read("transit", read_transit_state_func),
        _soft_read("output_hardware", _read_output_hardware),
        _soft_read_optional("service_states", service_states_snapshot),
        _soft_read("tools", _read_tool_catalog),
        # Conversation history is a read-only Feature surface. Settings are
        # wizard-owned and read fresh; the SQLite store is opened read-only so
        # jasper-control cannot create or mutate jasper-voice's DB.
        _soft_read(
            "chat", _conversation_history_state,
            exc=(ImportError, OSError, RuntimeError, ValueError),
        ),
    )
    listening_level, persisted_main_volume_db = volume_state or (None, None)
    probes = _Probes(*gathered, aec=aec_status, ha_status=ha_status)

    spotify = _spotify_state()
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            probes.camilla.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Top-level aliases for consumers that need only the running truth
        # and do not want to parse the nested runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]

    # USB Audio Input — fourth renderer. Fan-in owns the live DIRECT lane;
    # kernel UDC state owns host connection.
    usbsink_state = _build_usbsink_renderer_state(
        probes.fanin,
        host_connected=udc_host_connected(
            os.environ.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR),
        ),
    )

    voice_status = probes.voice or {}
    voice_session = bool(probes.voice) and voice_status.get("state") == "SESSION"
    active_source = _active_source(
        voice_session=voice_session,
        audio_health=audio_health,
        mux_status=probes.mux,
        spotify_playing=spotify["playing"],
        airplay_playing=airplay_playing,
        usbsink_playing=bool(usbsink_state and usbsink_state.get("playing")),
    )

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=probes.camilla["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=probes.mux,
        diagnostics=_read_volume_diagnostics(),
    )

    grouping_state = with_airplay_latency_fit(await _soft_read(
        "grouping",
        lambda: read_grouping_state(local_outputd_reader=lambda: probes.outputd),
    ))
    active_speaker_setup = await _soft_read(
        "active_speaker_setup",
        lambda: read_active_speaker_setup_status(
            active_config_path=probes.camilla.get("active_config_path"),
        ),
        exc=(OSError, RuntimeError, TypeError, ValueError, KeyError),
    )
    research_state = await _soft_read(
        "research",
        lambda: _research_state(voice_status.get("research")),
        exc=(ImportError, OSError, RuntimeError, ValueError),
    )

    # Lazy import (mirrors read_active_provider_state above) so jasper-control
    # doesn't pull jasper.voice.* at module load.
    from ..mic_presence import read_mic_presence
    mic_presence = read_mic_presence()

    payload: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            # active_provider.model sees only the wizard file; the merged
            # reader adds jasper.env, the same set jasper-voice sources.
            # See issue #3133.
            "model": (
                read_active_model_from_env_files(active_provider.provider)
                if active_provider.configured
                else None
            ),
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
            "reachable": probes.voice is not None,
            # Disambiguates reachable:false: true means the AEC reconciler
            # parked voice for a missing microphone ("intentionally idle, no
            # mic", NOT "crashed"). Same read as the `microphone` block below,
            # so the boolean and the rich record cannot disagree.
            "parked_no_mic": mic_presence.parked,
        },
        # The reconciler's one canonical mic record (jasper.mic_presence), so a
        # client renders "no microphone" as a fact rather than inferring it
        # from voice.reachable:false.
        "microphone": mic_presence.as_dict(),
        "audio": {
            "main_volume_db": probes.camilla["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": probes.camilla["playback_rms_dbfs"],
            "playback_peak_dbfs": probes.camilla["playback_peak_dbfs"],
            "clipped_samples": probes.camilla["clipped_samples"],
            "camilla_active_config_path": probes.camilla["active_config_path"],
            "sound": sound_profile,
            "output_hardware": output_hardware_state,
        },
        "active_speaker_setup": active_speaker_setup,
        "audition": audition_state,
        "bass_extension": bass_extension_state,
        "renderers": {
            "spotify": spotify,
            "airplay": (
                None if airplay_playing is None else {"playing": airplay_playing}
            ),
            # null when the feature is disabled (no state file), so a
            # consumer can show "off" as distinct from "idle".
            "usbsink": usbsink_state,
        },
        "speaker_name": _speaker_name_section(),
        "active_source": active_source,
        # Fan-in's UDS STATUS snapshot, flat and unwrapped. null only when
        # the daemon/socket is unavailable.
        "fanin": probes.fanin,
        # Final-output owner; jasper-doctor owns the actionable failure.
        "outputd": probes.outputd,
        # Additive mirror of GET /aec, so a one-shot /state consumer sees
        # requested intent vs observed runtime without a second request.
        "aec": probes.aec,
        "source_selection": probes.mux,
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
            # Cross-boot circuit breaker for the StartLimitAction=reboot
            # ladder. {"ran": false} when the oneshot hasn't run this boot;
            # tripped=true means reboot escalation is disarmed for this boot —
            # fix the failing daemon, then reboot to re-arm.
            "bootloop_guard": bootloop_guard_state.snapshot(),
            # jasper-camilla-recover's core-graph park record (ADR-0175).
            # parked=true means CamillaDSP was stopped out-of-band after a
            # failed recovery pass: the speaker emits NOTHING and nothing
            # re-arms it — the record's own `action`/`re_arm` are the remedy.
            # {"status": "absent"} on a healthy boot. Same reader
            # jasper-doctor's check_camilla_recover_park uses.
            "camilla_recover": camilla_recover_state.snapshot(),
            # jasper-outputd's ExecStopPost park record. parked=true means the
            # stop helper judged the failure terminal: outputd owns the DAC
            # write loop, so the speaker emits NOTHING until the output env is
            # fixed and the unit restarted. Same reader jasper-doctor's
            # check_outputd_failure_reconcile_park uses.
            "outputd_failure_reconcile": outputd_failure_reconcile_state.snapshot(
                (service_states or {}).get(outputd_failure_reconcile_state.UNIT),
            ),
            # The four named parks of the one-audio-transport rule (ADR-0178).
            # Read from the audio-health sampler's cached verdict, and by the
            # same reader jasper-doctor's check_ring_transport_park uses, so
            # the three surfaces cannot disagree.
            "transport_park": transport_park_snapshot(),
            # Bounded after-the-fact timeline for multiroom restart cascades,
            # scanned from event=multiroom.reconcile.*, restart_broker.* and
            # grouping_supervisor.* journal lines.
            "multiroom_cascade": _multiroom_cascade_snapshot(),
            # Effective mDNS identity (jasper-identity-reconcile, boot + 5-min
            # timer). status=collision means Avahi renamed us because another
            # device owns our hostname; the household should pick a unique
            # name. {"status": "absent"} pre-first-run.
            "identity": identity_state.snapshot(),
            # Root-filesystem fullness; jasper-doctor's check_disk_space owns
            # the warn(>=85%)/fail(>=95%) thresholds.
            "disk": _disk_snapshot(),
            # Speaker-setup PARKED state (#2135): an unconfigured or
            # declared-but-uncommissioned topology holds silence rather than
            # allow an inferred flat graph.
            "active_speaker_parked": _active_speaker_parked_snapshot(),
            # jasper-input stays `active` while one bridge loops in restart
            # backoff (ADR-0225); this is the only non-journal sign of it.
            "accessory_bridges": accessory_status.snapshot(),
        },
        "home_assistant": probes.ha_status,
        # Snapshot of the wizard-owned grouping.env plus airplay_latency_fit
        # ({applicable: false} unless this speaker is an active bonded leader).
        # enabled=True with a non-null error is the fail-LOUD "configured but
        # broken" state. See jasper/multiroom/state.py + airplay_latency.py.
        "grouping": grouping_state,
        # {packs: [{id, label, enabled}]} read fresh from the wizard-owned
        # transit.env. Mirrors the daemon's enabled_pack_ids on both absent
        # (all) and present-empty (none). See jasper/transit/state.py.
        "transit": transit_state,
        # Which subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
        # Read fresh from /run/jasper/tools.json + the wizard-owned
        # tool_state.env by jasper.tool_catalog_view (never os.environ).
        # jasper-doctor's check_tool_catalog owns the actionable warn.
        "tools": tools_state,
        # null when the read-side store is unavailable while capture is
        # enabled, or the read itself failed. See jasper.conversation_history.
        "chat": chat_state,
        # Async research summary. Counts and timestamps only; no prompt or
        # answer text leaves the local store through /state.
        "research": research_state,
        # The open measurement window as this process sees it — an in-memory
        # read of its own self-expiring copy, not a probe. `held_for_s` is
        # what jasper-doctor's check_measurement_hold reads: `expires_in_s`
        # resets on every renewal and so can never reveal a stuck hold.
        "measurement": measurement_hold.snapshot(),
        # The default-on, hardware-gated NCM link on usb0 that keeps
        # http://<JASPER_HOSTNAME>/ reachable with WiFi off when the resolved
        # USB role permits gadget mode.
        "usb_network": _usb_network_snapshot(),
        # The normalized health contract /system/snapshot renders. null when
        # this daemon runs no sampler, so the key set is the same either way.
        "audio_health": audio_health,
        "usb_gadget_forensics": usb_forensics,
    }
    return _stamp_observed_at(payload, read_at=time.time())
