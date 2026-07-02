# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State aggregation helpers for jasper-control."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from typing import Any, Callable

from .. import identity_state
from ..audio_quality import (
    DEFAULT_CONVERTER as _default_audio_converter,
    converter_options as _audio_converter_options,
    read_active_converter as _read_active_audio_converter,
    read_state as _read_audio_quality_state,
)
from ..music_sources import MUSIC_SOURCE_SPECS
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..multiroom.airplay_latency import with_airplay_latency_fit
from ..multiroom import cascade_timeline
from ..multiroom.state import read_grouping_state
from ..transit.state import read_state as read_transit_state
from ..log_event import log_event
from ..volume_diagnostics import (
    build_volume_policy_snapshot,
    read_diagnostics as _read_volume_diagnostics,
)
from . import (
    bootloop_guard_state,
    debug_control,
    grouping_supervisor,
    mpris,
    shairport_supervisor,
    system_supervisor,
    wifi_guardian_state,
)
from .aec_endpoints import _aec_full_status
from .dial import _dial_heartbeat, _probe_dial_reachable
from .uds import _local_status_json, _mux_socket_command, _voice_socket_command

logger = logging.getLogger(__name__)

SOURCE_AVAILABILITY_TTL_SEC = 10.0
_source_availability_cache: tuple[float, dict[str, Any]] | None = None
_source_availability_lock = threading.Lock()
OUTPUTD_BASE_CAMILLA_CONFIG = "/etc/camilladsp/outputd-cutover.yml"

# Per-probe ceiling for the CamillaDSP /state probe. Every other probe in
# _get_state already self-bounds (voice/mpris 2 s, mux 1 s, dial 0.5 s,
# fan-in/outputd 2 s); the CamillaDSP probe did not, so a wedged-but-
# listening DSP — TCP accepted, websocket read stalled — could hang the
# whole aggregate indefinitely. On timeout the probe fails soft to its
# all-None section, exactly like its siblings.
_CAMILLA_PROBE_TIMEOUT_SEC = 2.0

# Liveness backstop for the entire cross-daemon fan-out. This is NOT a
# latency control — the normal path completes in ~200 ms, with HA's cached
# network probe (~8 s worst case) the slow outlier. It only fires if a
# probe blows past its own ceiling (e.g. a future probe added without
# one), converting an unbounded hang into a logged, bounded failure so the
# bounded-worker control plane can never be parked indefinitely on /state.
_STATE_AGGREGATE_BUDGET_SEC = 20.0
_default_ha_status_cache: Any | None = None


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


def _safe_audio_quality_state() -> dict[str, Any]:
    try:
        return _read_audio_quality_state()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio quality state read failed")
        converter = _default_audio_converter
        options = _audio_converter_options()
        meta = next(
            option for option in options if option["converter"] == converter
        )
        try:
            active = _read_active_audio_converter()
        except Exception:  # noqa: BLE001
            active = None
        return {
            "converter": converter,
            "active_converter": active,
            "label": meta["label"],
            "summary": meta["summary"],
            "options": options,
            "error": str(e),
        }


def _fanin_input_status(
    fanin_status: dict[str, Any] | None,
    label: str,
) -> dict[str, Any] | None:
    if not isinstance(fanin_status, dict):
        return None
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return None
    for entry in inputs:
        if isinstance(entry, dict) and entry.get("label") == label:
            return entry
    return None


def _route_latency_artifact_state(plan: Any) -> dict[str, Any] | None:
    if not getattr(plan.route_profile, "low_latency_claim", False):
        return None
    try:
        from ..audio_validation import (
            ROUTE_LATENCY_MIC_ID,
            ROUTE_LATENCY_PROFILE,
            ROUTE_LATENCY_STALE_AFTER,
            artifact_directory,
            assess_route_latency_artifact,
            load_latest_artifact,
        )

        dac_id = None if plan.profile_id == "unknown" else plan.profile_id
        result = load_latest_artifact(
            artifact_directory(),
            mic_id=ROUTE_LATENCY_MIC_ID,
            dac_id=dac_id,
            profile=ROUTE_LATENCY_PROFILE,
            max_age=ROUTE_LATENCY_STALE_AFTER,
        )
        return assess_route_latency_artifact(
            result,
            route_config_hash=plan.route_config_hash,
            expected_identity=plan.route_latency_identity(),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("route latency artifact state read failed")
        return {"status": "fail", "reason": str(e)}


def _audio_graph_state(
    *,
    usbsink_raw: dict[str, Any] | None,
    fanin_status: dict[str, Any] | None,
    outputd_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    try:
        from ..audio_runtime_plan import build_audio_runtime_plan_from_system

        plan = build_audio_runtime_plan_from_system()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio graph route plan read failed")
        return {"route": {"status": "unavailable", "error": str(e)}}

    usbsink_counters = None
    usbsink_ring = None
    if isinstance(usbsink_raw, dict):
        counters = usbsink_raw.get("counters")
        ring = usbsink_raw.get("ring")
        usbsink_counters = counters if isinstance(counters, dict) else None
        usbsink_ring = ring if isinstance(ring, dict) else None
    fanin_usbsink = _fanin_input_status(fanin_status, "usbsink")
    outputd_dac = (
        outputd_status.get("dac")
        if isinstance(outputd_status, dict)
        and isinstance(outputd_status.get("dac"), dict)
        else None
    )
    outputd_aec_clock = (
        outputd_status.get("aec_clock")
        if isinstance(outputd_status, dict)
        and isinstance(outputd_status.get("aec_clock"), dict)
        else None
    )
    outputd_latency = (
        outputd_aec_clock.get("latency")
        if isinstance(outputd_aec_clock, dict)
        and isinstance(outputd_aec_clock.get("latency"), dict)
        else None
    )
    artifact = _route_latency_artifact_state(plan)
    route_status = "unclaimed"
    if plan.route_profile.low_latency_claim:
        route_status = (
            str(artifact.get("status"))
            if isinstance(artifact, dict) and artifact.get("status")
            else "fail"
        )
    return {
        "route": {
            "id": plan.route_profile.route_id,
            "source_id": plan.route_profile.source_id,
            "claim_status": route_status,
            "low_latency_claim": plan.route_profile.low_latency_claim,
            "route_config_hash": plan.route_config_hash,
            "p95_budget_ms": plan.route_profile.p95_budget_ms,
            "p99_budget_ms": plan.route_profile.p99_budget_ms,
            "contract": plan.route_profile.to_dict(),
        },
        "artifact": artifact,
        "rust_bridge": {
            "implementation": (
                usbsink_raw.get("implementation")
                if isinstance(usbsink_raw, dict)
                else None
            ),
            "ring": usbsink_ring,
            "counters": usbsink_counters,
            "period_frames": (
                usbsink_raw.get("period_frames")
                if isinstance(usbsink_raw, dict)
                else None
            ),
            # Stage 1 host-slaved USB clock (default-OFF). The Rust bridge
            # emits this block unconditionally (also when the feature is
            # disabled), so pre-Stage-1 builds and a missing/unreadable
            # state file are the only ways this comes through as None — a
            # definite "no evidence yet" rather than a guessed default.
            # See docs/HANDOFF-usb-low-latency.md "Host-slaved USB clock
            # (Stage 1)" for field semantics.
            "host_clock": (
                usbsink_raw.get("host_clock")
                if isinstance(usbsink_raw, dict)
                else None
            ),
        },
        "fanin": {
            "usbsink_input": fanin_usbsink,
            "resampler": (
                fanin_usbsink.get("resampler")
                if isinstance(fanin_usbsink, dict)
                else None
            ),
        },
        "outputd": {
            "dac_delay_ms": (
                outputd_dac.get("snd_pcm_delay_ms")
                if isinstance(outputd_dac, dict)
                else None
            ),
            "dac_delay_frames": (
                outputd_dac.get("snd_pcm_delay_frames")
                if isinstance(outputd_dac, dict)
                else None
            ),
            "final_reference_health": outputd_aec_clock,
            "route_latency_components": outputd_latency,
        },
    }


def _conversation_history_state() -> dict[str, Any] | None:
    """Read /state.chat fresh from the conversation-history SSOT + store."""
    from datetime import datetime, timezone

    from ..conversation_history import ConversationStore, read_settings

    settings = read_settings()
    store = ConversationStore(
        settings.db_path,
        read_only=True,
        warn_unavailable=False,
    )
    try:
        stats = store.stats()
        if stats is None:
            if settings.capture_enabled:
                return None
            return {
                "capture_enabled": False,
                "turn_count": None,
                "last_write_age_seconds": None,
                "retention": settings.retention,
            }
        age_seconds = None
        if stats.last_write_ts_utc:
            raw = stats.last_write_ts_utc.strip()
            parse_value = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
            try:
                ts = datetime.fromisoformat(parse_value)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, round(time.time() - ts.timestamp(), 1))
            except ValueError:
                age_seconds = None
        return {
            "capture_enabled": settings.capture_enabled,
            "turn_count": stats.turn_count,
            "last_write_age_seconds": age_seconds,
            "retention": settings.retention,
        }
    finally:
        store.close()


def _research_state(
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read privacy-safe async-research state."""
    from ..research.state import snapshot

    return snapshot(runtime=runtime)


def _disk_snapshot(path: str = "/") -> dict[str, Any] | None:
    """Root-filesystem fullness for /state.resilience — fail-soft.

    Returns ``{path, percent_used, free_gib, total_gib}`` or ``None`` on
    any error (non-POSIX dev host, statvfs failure), mirroring the
    fail-soft contract every other resilience-block section follows: a
    broken read leaves this section null and the rest of /state intact.
    jasper-doctor's ``check_disk_space`` owns the actionable warn/fail
    thresholds; this is the always-visible dashboard number that makes a
    filling SD card observable before the doctor is run. Uses f_bavail
    (non-root-available blocks) for free space so the figure matches what
    the daemons can actually write, but derives percent-used from
    total-vs-free so reserved blocks don't read as headroom."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        st = statvfs(path)
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        free = st.f_bavail * st.f_frsize
        gib = 1024 ** 3
        return {
            "path": path,
            "percent_used": ((total - free) * 100) // total,
            "free_gib": round(free / gib, 1),
            "total_gib": round(total / gib, 1),
        }
    except Exception:  # noqa: BLE001
        logger.debug("disk snapshot read failed", exc_info=True)
        return None


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


async def _outputd_status(
    *,
    local_status_json: Callable[..., Any] = _local_status_json,
) -> dict | None:
    """Probe jasper-outputd's STATUS endpoint.

    Missing socket is fail-soft here so /state remains available while
    jasper-doctor owns the actionable cutover failure.
    """
    return await local_status_json("/run/jasper-outputd/control.sock")


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add on/off wizard availability to mux source status.

    Mux knows audio policy; `/sources/` knows whether each renderer is
    enabled/available. The landing selector needs both, but keeping the
    merge here avoids teaching mux about systemd/DBus source toggles.
    """
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return payload
    global _source_availability_cache
    now = time.monotonic()
    with _source_availability_lock:
        cached = _source_availability_cache
        if cached is not None and now - cached[0] < SOURCE_AVAILABILITY_TTL_SEC:
            wizard_state = cached[1]
        else:
            wizard_state = None
    if wizard_state is None:
        try:
            from ..web.sources_setup import _gather_state as _sources_state
            fresh_state = _sources_state()
        except Exception as e:  # noqa: BLE001
            logger.debug("source availability read failed: %s", e)
            return payload
        with _source_availability_lock:
            _source_availability_cache = (now, fresh_state)
        wizard_state = fresh_state
    for spec in MUSIC_SOURCE_SPECS:
        wizard_key = spec.wizard_key
        mux_key = spec.id.value
        state = wizard_state.get(wizard_key)
        if not isinstance(state, dict):
            continue
        slot = sources.setdefault(mux_key, {})
        if isinstance(slot, dict):
            slot["available"] = bool(state.get("available", True))
            slot["enabled"] = bool(state.get("enabled", False))
    return payload


def _capture_relay_config() -> dict[str, Any]:
    """Network-free phone-mic-relay config snapshot for `/state.capture_relay`.

    Reads relay env from os.environ DIRECTLY (deploy-time values) so
    jasper-control never imports the capture_relay package's numpy/scipy deps
    just for a config field. The doctor (on-demand) imports
    capture_relay.health to actively probe reachability. This MUST stay in
    lockstep with capture_relay.health.relay_config_from_env — pinned by
    tests/test_capture_relay_health.py.
    """
    base = (os.environ.get("JASPER_CAPTURE_RELAY_BASE") or "").strip().rstrip("/")
    registration_token = (
        os.environ.get("JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN") or ""
    ).strip()
    return {
        "configured": bool(base),
        "relay_base": base or None,
        "registration_secret_configured": bool(registration_token),
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
    dial_heartbeat: dict[str, Any] = _dial_heartbeat,
    dial_probe: Callable[..., Any] = _probe_dial_reachable,
    read_transit_state_func: Callable[[], dict] = read_transit_state,
    ha_status_snapshot: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate state across daemons for GET /state. Each section
    fails soft — voice unreachable / camilla restarting / dial never
    connected → that section reports null instead of erroring out
    the whole response. Slow probes fan out in parallel so the call
    completes in ~200 ms typical."""
    from datetime import datetime, timezone

    from .. import librespot_state
    from ..camilla import CamillaController
    from ..output_hardware import load_state as _load_output_hardware_state
    from ..speaker_name import read_state as _read_speaker_name_state
    from ..voice.provider_state import (
        read_active_provider_state,
        read_barge_in_enabled,
    )

    # Provider + model: re-read the wizard-owned SSOT file fresh on every
    # call. jasper-control is NOT restarted on a provider switch (only
    # jasper-voice is), so reading os.environ here pins the value to
    # whatever it was at this daemon's start and shows a stale provider
    # after every switch — the /system/ bug this fixes. Same fresh-read
    # rationale as the home_assistant block in /system/snapshot below.
    # ("", None) when unconfigured; never a guessed default.
    active_provider = read_active_provider_state()

    listening_level: int | None = None
    persisted_main_volume_db: float | None = None
    try:
        path = os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        )
        with open(path) as f:
            blob = json.load(f)
        raw_level = blob.get("listening_level")
        if isinstance(raw_level, (int, float)) and 0 <= raw_level <= 100:
            listening_level = int(raw_level)
        raw_db = blob.get("main_volume_db")
        if isinstance(raw_db, (int, float)) and math.isfinite(float(raw_db)):
            persisted_main_volume_db = round(float(raw_db), 2)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    sound_profile: dict[str, Any] | None
    try:
        from ..dsp_apply import last_dsp_apply_state
        from ..sound.profile import (
            build_sound_filters,
            estimate_headroom_db,
            load_profile,
        )
        from ..sound.settings import load_sound_settings, output_trim_db

        profile = load_profile()
        sound_settings = load_sound_settings()
        sound_profile = {
            "enabled": profile.enabled,
            "curve_id": profile.curve_id,
            "simple_eq": profile.simple_eq.to_dict(),
            "parametric_band_count": len(profile.parametric_bands),
            "filter_count": len(build_sound_filters(profile)),
            "headroom_db": estimate_headroom_db(profile),
            # Global output settings + the effective trim they apply, so the
            # dashboard can explain why a profile sounds quieter/level-matched.
            "match_loudness": sound_settings.match_loudness,
            "headroom_trim_db": sound_settings.headroom_trim_db,
            "output_trim_db": output_trim_db(profile, sound_settings),
            "updated_at": profile.updated_at or None,
            "last_dsp_apply": last_dsp_apply_state(),
        }
    except Exception:  # noqa: BLE001
        logger.exception("sound profile state probe failed")
        sound_profile = None

    # Slow probes — fan out in parallel.
    def _round_db(value: float | None) -> float | None:
        if value is None:
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, 2)

    def _round_pair(
        pair: tuple[float, float] | None,
    ) -> list[float | None] | None:
        if pair is None:
            return None
        return [_round_db(pair[0]), _round_db(pair[1])]

    def _finite_float_or_none(raw: Any) -> float | None:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        value = float(raw)
        if not math.isfinite(value):
            return None
        return value

    async def _camilla_status() -> dict[str, Any]:
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
            cam = CamillaController(host=camilla_host, port=camilla_port)
            config_path_probe = (
                cam.get_config_file_path(best_effort=True)
                if hasattr(cam, "get_config_file_path")
                else _no_config_path()
            )
            vol, rms, peak, clipped, active_config_path = await asyncio.wait_for(
                asyncio.gather(
                    cam.get_volume_db(best_effort=True),
                    cam.get_playback_rms(best_effort=True),
                    cam.get_playback_peak(best_effort=True),
                    cam.get_clipped_samples(best_effort=True),
                    config_path_probe,
                ),
                timeout=_CAMILLA_PROBE_TIMEOUT_SEC,
            )
            status["main_volume_db"] = _round_db(vol)
            status["playback_rms_dbfs"] = _round_pair(rms)
            status["playback_peak_dbfs"] = _round_pair(peak)
            status["clipped_samples"] = clipped
            status["active_config_path"] = active_config_path
            return status
        except Exception:  # noqa: BLE001
            return status

    async def _airplay_playing() -> bool | None:
        # Shared probe owns the subprocess hygiene (kill-on-timeout so a
        # DBus stall can't leak one busctl per /state poll; spawn OSError
        # → None instead of 500ing the whole fail-soft aggregate).
        return await mpris.shairport_playing(timeout=2.0)

    async def _voice_status() -> dict | None:
        try:
            return await voice_socket_command(
                voice_socket_path, "STATUS", timeout=2.0,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError, RuntimeError):
            return None

    async def _ha_status() -> dict:
        """HA status for /state via the child-process cache boundary.

        The cache reads the wizard env-file signature fresh, so saves are
        reflected without restarting jasper-control, while HA/httpx imports
        stay in the short-lived probe child instead of the control daemon.
        """
        snapshot = ha_status_snapshot or _default_ha_status_snapshot
        try:
            return snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("home assistant state snapshot failed")
            return _ha_failed_status()

    # Snapshot dial heartbeat early so the parallel reachability probe
    # has a stable IP target even if the UDP listener mutates the dict
    # mid-call. last_seen_ip is None until the dial has dlogged at
    # least once — without an IP we can't probe, so online stays false.
    dial_snapshot = dict(dial_heartbeat)
    dial_ip = dial_snapshot.get("last_seen_ip")

    async def _dial_online() -> bool:
        if not dial_ip:
            return False
        return await dial_probe(dial_ip)

    async def _fanin_status() -> dict | None:
        """Probe the jasper-fanin daemon's UDS STATUS endpoint.

        Returns None when:
          - the daemon isn't running yet or is unhealthy
          - the socket doesn't exist (daemon not yet bound)
          - the probe times out (work loop wedged, ALSA blocked)
          - the response isn't valid JSON

        Fan-in is mandatory for renderer audio, but /state is fail-soft
        like _voice_status. jasper-doctor owns the actionable failure.
        See docs/HANDOFF-fan-in-daemon.md for the daemon design.
        """
        return await local_status_json("/run/jasper-fanin/control.sock")

    async def _mux_status() -> dict | None:
        try:
            return await mux_socket_command("STATUS", timeout=1.0)
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    async def _aec_status() -> dict | None:
        """Additive mirror of GET /aec for one-shot /state consumers."""
        try:
            return await asyncio.to_thread(aec_full_status)
        except Exception:  # noqa: BLE001
            logger.exception("AEC/profile state probe failed")
            return None

    try:
        (
            camilla_st,
            airplay,
            voice_st,
            ha_status,
            dial_online,
            fanin_st,
            outputd_st,
            mux_st,
            aec_status,
        ) = await asyncio.wait_for(
            asyncio.gather(
                _camilla_status(),
                _airplay_playing(),
                _voice_status(),
                _ha_status(),
                _dial_online(),
                _fanin_status(),
                _outputd_status(local_status_json=local_status_json),
                _mux_status(),
                _aec_status(),
            ),
            timeout=_STATE_AGGREGATE_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        # A probe blew past its own ceiling. Fail loud (the handler turns
        # this into a 502) rather than hang a bounded worker forever; the
        # cheap /healthz probe stays answerable so this can't manufacture a
        # T5.2 reboot. Greppable so the offending probe is diagnosable.
        log_event(
            logger,
            "state.aggregate_timeout",
            budget_sec=_STATE_AGGREGATE_BUDGET_SEC,
            level=logging.WARNING,
        )
        raise

    spotify_blob = librespot_state.read(
        os.environ.get("JASPER_LIBRESPOT_STATE", librespot_state.DEFAULT_PATH),
    )
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            camilla_st.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Keep these top-level aliases for lightweight consumers that
        # only need the running truth and do not want to parse the nested
        # runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]
    speaker_name_state = _read_speaker_name_state()
    spotify = {
        "playing": bool(spotify_blob.get("playing", False)),
        "track_id": spotify_blob.get("track_id"),
        "uri": spotify_blob.get("uri"),
        "session_active": bool(spotify_blob.get("session_active", False)),
    }

    # USB sink — fourth renderer. Reads the state file the daemon
    # publishes. Section reports None when the feature is disabled
    # (no state file) so consumers can distinguish "off" from
    # "on but idle".
    usbsink_state: dict | None = None
    usbsink_raw: dict[str, Any] | None = None
    try:
        with open(
            os.environ.get(
                "JASPER_USBSINK_STATE_PATH",
                "/run/jasper-usbsink/state.json",
            ),
        ) as f:
            usbsink_blob = json.load(f)
        if isinstance(usbsink_blob, dict):
            usbsink_raw = usbsink_blob
        usbsink_state = {
            "playing": bool(usbsink_blob.get("playing", False)),
            "preempted": bool(usbsink_blob.get("preempted", False)),
            "host_connected": bool(
                usbsink_blob.get("host_connected", False),
            ),
            "rms_dbfs": _finite_float_or_none(usbsink_blob.get("rms_dbfs")),
            "updated_at": usbsink_blob.get("updated_at"),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    voice_session = bool(voice_st) and voice_st.get("state") == "SESSION"
    # Active-source picks. Mux owns the effective audible source in
    # both manual and auto mode. Fall back to raw renderer probes only
    # when mux is unavailable or has no selected winner yet.
    mux_effective_source = None
    if isinstance(mux_st, dict):
        raw_selected = mux_st.get("selected_source")
        if isinstance(raw_selected, str):
            mux_effective_source = raw_selected
        else:
            raw_winner = mux_st.get("winner")
            if isinstance(raw_winner, str):
                mux_effective_source = raw_winner

    if voice_session:
        active_source: str = "voice"
    elif mux_effective_source:
        active_source = mux_effective_source
    elif spotify["playing"]:
        active_source = "spotify"
    elif airplay:
        active_source = "airplay"
    elif usbsink_state is not None and usbsink_state.get("playing"):
        active_source = "usbsink"
    else:
        active_source = "idle"

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=camilla_st["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=mux_st,
        diagnostics=_read_volume_diagnostics(),
    )

    # Build the dial section from the snapshot taken before the gather
    # so age_seconds is consistent with whatever IP the probe targeted.
    # `online` reflects real TCP reachability (see _probe_dial_reachable),
    # not UDP-dlog freshness — an idle dial is now correctly online
    # rather than mislabelled offline after 30 s of no encoder activity.
    dial = dial_snapshot
    if dial.get("last_seen_at") is not None:
        dial["age_seconds"] = round(time.time() - dial["last_seen_at"], 1)
    else:
        dial["age_seconds"] = None
    dial["online"] = dial_online

    # Multiroom grouping. Re-reads /var/lib/jasper/grouping.env fresh
    # (never os.environ — jasper-control isn't restarted on a wizard
    # save). read_grouping_state is itself total, but guard the section
    # so any future read change can't take the whole /state down: a
    # broken read leaves grouping null and the rest of /state intact.
    # enabled=False means grouping is off (solo); enabled=True with a
    # non-null error is the fail-LOUD "configured but broken" state.
    try:
        grouping_state: dict | None = read_grouping_state(
            local_outputd_reader=lambda: outputd_st,
        )
    except Exception:  # noqa: BLE001
        logger.exception("grouping state read failed")
        grouping_state = None

    # Bonded-leader AirPlay latency fit (Stage D observability — see
    # jasper/multiroom/airplay_latency.py). The shared composer (also used by
    # /rooms.json) attaches it non-mutatingly; read_grouping_state stays a pure
    # config projection and the gated, cached journal read lives behind the
    # helper. Total (returns {"applicable": False} on solo without touching the
    # journal), so the grouping section survives a broken read.
    grouping_state = with_airplay_latency_fit(grouping_state)

    try:
        active_speaker_setup = read_active_speaker_setup_status(
            active_config_path=camilla_st.get("active_config_path"),
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        logger.exception("active speaker setup status read failed")
        active_speaker_setup = None

    # Transit city packs. Re-reads /var/lib/jasper/transit.env fresh (never
    # os.environ — jasper-control isn't restarted on a /transit/ save, only
    # jasper-voice is). read_transit_state is itself total, but guard the
    # section so a future read change can't take the whole /state down: a
    # broken read leaves transit null and the rest of /state intact.
    try:
        transit_state: dict | None = read_transit_state_func()
    except Exception:  # noqa: BLE001
        logger.exception("transit state read failed")
        transit_state = None
    try:
        output_hardware = _load_output_hardware_state()
        output_hardware_state = (
            output_hardware.to_dict()
            if output_hardware is not None
            else None
        )
    except Exception:  # noqa: BLE001
        logger.exception("output hardware state read failed")
        output_hardware_state = None

    audio_graph_state = _audio_graph_state(
        usbsink_raw=usbsink_raw,
        fanin_status=fanin_st,
        outputd_status=outputd_st,
    )

    # Tool catalog summary. Fresh read of /run/jasper/tools.json (written by
    # jasper-voice) + the wizard-owned disabled-set — never os.environ, since
    # jasper-control isn't restarted on a /tools/ toggle. Light view module
    # (json + tool_state only). Guarded so a read change can't take /state down.
    try:
        from ..tool_catalog_view import summary as _tool_summary
        tools_state: dict | None = _tool_summary()
    except Exception:  # noqa: BLE001
        logger.exception("tool catalog state read failed")
        tools_state = None

    # Conversation history is a read-only Feature surface. Settings are
    # wizard-owned and read fresh; the SQLite store is opened read-only so
    # jasper-control cannot create or mutate jasper-voice's DB.
    try:
        chat_state = _conversation_history_state()
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.exception("conversation history state read failed")
        chat_state = None

    try:
        research_state = _research_state((voice_st or {}).get("research"))
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.exception("research state read failed")
        research_state = None

    # Lazy import (mirrors read_active_provider_state above) so jasper-control
    # doesn't pull jasper.voice.* at module load. mic_presence reads the
    # reconciler's SSOT (one JSON read + a marker stat per /state) — cheap, and
    # it composes voice.input_presence's tiny marker reader.
    from ..mic_presence import read_mic_presence
    mic_presence = read_mic_presence()

    capture_relay_state = _capture_relay_config()

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            "model": active_provider.model,
            "provider_status": active_provider.status,
            "provider_error": active_provider.detail or None,
            "session_active": voice_session,
            "spend_allowed": (voice_st or {}).get("spend_allowed"),
            # usage.db writes failing -> recorded spend goes stale and the cap
            # can't enforce. Curated explicitly like the other voice fields
            # (see the wake_legs note below); a new session_status field must be
            # pulled through here too.
            "usage_tracking_degraded": (voice_st or {}).get("usage_tracking_degraded"),
            "connection_paused": (voice_st or {}).get("connection_paused"),
            "mic_muted": (voice_st or {}).get("mic_muted"),
            "music_dbfs": (voice_st or {}).get("music_dbfs"),
            # Runtime-armed wake-leg tokens from jasper-voice's
            # session_status. jasper-doctor's check_wake_legs cross-checks
            # this against the configured intent in aec_mode.env to surface
            # a startup leg-skip; the /state aggregator curates voice
            # fields explicitly, so a new session_status field must be
            # pulled through here too.
            "wake_legs": (voice_st or {}).get("wake_legs"),
            # Per-pack tool-registration outcomes from jasper-voice's
            # session_status (added with the data-driven tool-pack
            # registry). jasper-doctor's check_tool_packs reads this to
            # flag a tool family that silently failed to build. Curated
            # explicitly here like the other voice fields, so a new
            # session_status field must be pulled through.
            "tool_packs": (voice_st or {}).get("tool_packs"),
            # In-session barge-in (full-duplex). `enabled` is read FRESH
            # per active provider here (same rationale as provider/model
            # above): jasper-control is NOT restarted on a barge-in toggle,
            # so an os.environ/Config cache would show a stale value. The
            # firing stats are curated pull-through from jasper-voice's
            # session_status (like wake_legs / tool_packs) — null when
            # voice is unreachable.
            "barge_in": {
                "enabled": (
                    read_barge_in_enabled(active_provider.provider)
                    if active_provider.provider else False
                ),
                "last_at": (voice_st or {}).get("barge_in_last_at"),
                "count_session": (voice_st or {}).get("barge_in_count_session"),
                "last_leg": (voice_st or {}).get("barge_in_last_leg"),
            },
            "reachable": voice_st is not None,
            # Disambiguates reachable:false. True when the AEC reconciler
            # parked voice for a missing microphone (its ConditionPathExists
            # marker is present) — i.e. "intentionally idle, no mic", NOT
            # "crashed". Read fresh from the marker each call (jasper-control
            # isn't restarted on a mic plug/unplug). See
            # docs/HANDOFF-hotplug-resilience.md "Layer 3".
            # Derived from the same read as the top-level `microphone` block
            # below, so the boolean and the rich record can never disagree.
            "parked_no_mic": mic_presence.parked,
        },
        # Single source of truth for mic presence (jasper.mic_presence): the
        # reconciler's one canonical record, surfaced so the dashboard / any
        # client renders "no microphone" as one fact (present + reason + card +
        # variant + channels + a ready-made `summary`) instead of inferring it
        # from voice.reachable:false.
        "microphone": mic_presence.as_dict(),
        "audio": {
            "main_volume_db": camilla_st["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": camilla_st["playback_rms_dbfs"],
            "playback_peak_dbfs": camilla_st["playback_peak_dbfs"],
            "clipped_samples": camilla_st["clipped_samples"],
            "camilla_active_config_path": camilla_st["active_config_path"],
            "sound": sound_profile,
            "output_hardware": output_hardware_state,
        },
        "audio_graph": audio_graph_state,
        "active_speaker_setup": active_speaker_setup,
        "renderers": {
            "spotify": spotify,
            "airplay": (
                None if airplay is None else {"playing": airplay}
            ),
            # null when the feature is disabled (no state file). The
            # /system dashboard and any other consumer can show
            # "off" vs "idle" based on this.
            "usbsink": usbsink_state,
        },
        "speaker_name": {
            "name": speaker_name_state.name,
            "source": speaker_name_state.source,
        },
        "active_source": active_source,
        # Fan-in daemon. null only when the daemon/socket is unavailable.
        # When running, the UDS STATUS endpoint emits a JSON snapshot
        # with per-input frame counts, output xrun counts, and watchdog
        # metrics — surfaced verbatim here. See
        # docs/HANDOFF-fan-in-daemon.md.
        "fanin": fanin_st,
        # Final-output owner on current main. null when the daemon/socket
        # is unavailable; jasper-doctor owns the actionable failure.
        "outputd": outputd_st,
        # Additive mirror of GET /aec so one-shot /state consumers can see
        # requested intent vs observed mic/profile runtime truth without a
        # second control-plane request. null only when the probe itself fails.
        "aec": aec_status,
        "source_selection": mux_st,
        "satellites": {
            "dial": dial,
        },
        "resilience": {
            "shairport": shairport_supervisor.snapshot(),
            # Bonded-member runtime liveness: dac_content starvation
            # watch (kicks the grouping reconciler, rate-limited) +
            # continuous snapcast binding read-repair on the leader.
            # Off via JASPER_GROUPING_SUPERVISOR=disabled.
            "grouping_supervisor": grouping_supervisor.snapshot(),
            # T5.2 — userspace-liveness supervisor. Probes sshd / our
            # own HTTP / /proc/loadavg every 30 s; clean-reboots after
            # 3 consecutive failures (rate-limited 1/24h). Off via
            # JASPER_SYSTEM_SUPERVISOR=disabled.
            "system_supervisor": system_supervisor.snapshot(),
            # WiFi profile guardian: self-heal of the NM keyfile after
            # dirty shutdown. Synthesised from the on-disk stash + the
            # most recent `event=wifi_guardian.*` journal line — there's
            # no resident daemon to ask (the guardian is Type=oneshot).
            # Fail-soft inside the snapshot itself; never raises.
            "wifi_guardian": wifi_guardian_state.snapshot(),
            # Boot-loop guard (cross-boot circuit breaker for the T5.1
            # StartLimitAction=reboot ladder). Fresh marker read per
            # call; {"ran": false} when the oneshot hasn't run this
            # boot. tripped=true means reboot escalation is disarmed
            # for this boot via runtime drop-ins — fix the failing
            # daemon, then reboot to re-arm.
            "bootloop_guard": bootloop_guard_state.snapshot(),
            # Bounded after-the-fact timeline for multiroom restart cascades:
            # existing event=multiroom.reconcile.*, restart_broker.*, and
            # grouping_supervisor.* journal lines, scanned into a tiny ring so
            # /state can answer "what kicked what recently?" without a raw log
            # bundle.
            "multiroom_cascade": _multiroom_cascade_snapshot(),
            # Effective mDNS identity (jasper-identity-reconcile, boot
            # + 5-min timer). status=collision means Avahi renamed us —
            # another device owns our hostname; the management
            # allowlist self-heals from the same file, but the
            # household should pick a unique name. Fresh file read per
            # call (reconciler-owned, this daemon is never restarted on
            # identity changes); {"status": "absent"} pre-first-run.
            "identity": identity_state.snapshot(),
            # Root-filesystem fullness ({path, percent_used, free_gib,
            # total_gib}). A full SD card is the corruption hazard the
            # whole resilience ladder exists to survive, yet nothing made
            # it observable until writes failed. Fail-soft: null on a
            # non-POSIX host or statvfs error. jasper-doctor's
            # check_disk_space owns the warn(≥85%)/fail(≥95%) thresholds.
            "disk": _disk_snapshot(),
        },
        "home_assistant": ha_status,
        # Multiroom grouping (off by default). null only if the fresh
        # read itself errored; otherwise a JSON-able snapshot of the
        # wizard-owned grouping.env (enabled / role / channel / bond_id /
        # leader_addr / buffer_ms / codec / error), PLUS airplay_latency_fit:
        # the bonded-leader AirPlay tight-regime observability ({applicable:
        # false} unless this speaker is an active bonded leader). See
        # jasper/multiroom/state.py + jasper/multiroom/airplay_latency.py +
        # docs/HANDOFF-multiroom.md / docs/HANDOFF-airplay.md.
        "grouping": grouping_state,
        # Transit city packs (which cities' transit is enabled). null only
        # if the fresh read itself errored; otherwise {packs: [{id, label,
        # enabled}]} read fresh from the wizard-owned transit.env. Mirrors
        # the daemon's enabled_pack_ids on both absent (all) and
        # present-empty (none). See jasper/transit/state.py.
        "transit": transit_state,
        # Runtime debug-logging toggle (the /system Debug card): which
        # subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
        # Tool catalog summary ({catalog_present, count, disabled,
        # disabled_count, pending}). null only if the fresh read itself
        # errored. Read fresh from /run/jasper/tools.json + the wizard-owned
        # tool_state.env by jasper.tool_catalog_view (never os.environ).
        # jasper-doctor's check_tool_catalog owns the actionable warn.
        "tools": tools_state,
        # Conversation-history summary. null only if the read-side store
        # is unavailable while capture is enabled, or if the state read
        # itself fails. See jasper.conversation_history.
        "chat": chat_state,
        # Async research summary. Counts and timestamps only; no prompt or
        # answer text leaves the local store through /state.
        "research": research_state,
        # Phone-mic capture relay config snapshot (network-free; the doctor
        # probes reachability on demand). {configured, relay_base}.
        "capture_relay": capture_relay_state,
    }
