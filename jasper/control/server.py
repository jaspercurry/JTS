# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP control surface for external clients (dial, future wall switches,
home automation). Bound to LAN so an ESP32 dial on the household network
can drive volume / transport / session.

Stack: stdlib http.server (bounded ThreadingHTTPServer), pycamilladsp
client, VolumeCoordinator (source-aware dispatch).

The route table is in `_make_handler` below — `do_GET` and `do_POST`
own the dispatch in one place rather than mirroring the list here
(that mirror went stale several times). Highlights:

- Volume + transport + session-bypass: dial-driven actions.
- /state: cross-daemon JSON snapshot — voice / audio / renderers /
  satellites; consumable from the /voice web UI, jasper-doctor, or
  `curl`.
- /cue/play: proxy to voice_daemon's UDS so a cue plays through
  the daemon's already-correctly-gained TtsPlayout.
- /dial/status: focused dial heartbeat (subset of /state.satellites.dial,
  kept because jasper-doctor calls it directly).

Volume dispatch: requests build a fresh VolumeCoordinator per call
(matches the per-request _dispatch_transport pattern). The coordinator
reads the canonical listening_level from /var/lib/jasper/speaker_volume.json,
applies the change, dispatches to the active source (or CamillaDSP
when idle), persists. This daemon doesn't run inbound observers —
that's voice_daemon's job. Both daemons converge through persistence.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import urllib.request
import logging
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from jasper.log_event import log_event

from ..http_security import management_read_allowed, mutating_request_allowed
from ..audio_quality import (
    apply_requested_converter as _apply_audio_quality,
    normalize_converter as _normalize_audio_converter,
)
from . import (
    debug_control,
    grouping_supervisor,
    shairport_supervisor,
    system_supervisor,
)
from ..multiroom.config import (
    CROSSOVER_HZ_HI,
    CROSSOVER_HZ_LO,
    DEFAULT_CROSSOVER_HZ,
    DEFAULT_MAINS_HIGHPASS_ENABLED,
    GROUPING_ENV_FILE,
    BondMember,
    GroupingConfig,
    format_roster,
    load_config as load_grouping_config,
    validate_grouping,
    validate_roster,
)
from ..multiroom.runtime_balance import apply_local_trim as apply_live_grouping_trim
from ..multiroom.state import grouping_response, read_grouping_state
from ..music_sources import MUSIC_SOURCE_SPECS
from ..local_sources import (
    local_source_audio_refresh_units,
    local_source_park_units,
)
from ..transit.state import read_state as read_transit_state
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..audio_profile_state import (
    normalize_audio_input_profile,
)
from ..install_profile import (
    STREAMBOX_INSTALL_PROFILE,
    install_role_for_profile,
    read_install_profile,
    system_capabilities_for_profile,
)
from . import aec_endpoints as _aec_endpoints
from . import control_token
from . import household_credential
from . import restart_broker
from . import dial as _dial
from . import state_aggregate as _state_aggregate
from . import volume_ops as _volume_ops
from .uds import (
    _local_status_json,
    _mux_socket_command,
    _voice_socket_command,
)

logger = logging.getLogger(__name__)
SOURCE_SELECT_IDS = {spec.id.value for spec in MUSIC_SOURCE_SPECS}
_peering_lock = threading.Lock()
_peering_loop: asyncio.AbstractEventLoop | None = None
_peering_stop_requested = threading.Event()
CORE_AUDIO_RESTART_UNITS = ["jasper-camilla.service"]
LOCAL_SOURCE_AUDIO_REFRESH_UNITS = list(local_source_audio_refresh_units())
_DIAGNOSTICS_RESULT_PATH = "/run/jasper-control/doctor-result.json"
_DIAGNOSTICS_CACHE_TTL_SECONDS = 60.0
_DIAGNOSTICS_REFRESH_MIN_INTERVAL_SECONDS = 5.0
_diagnostics_refresh_lock = threading.Lock()
_diagnostics_refresh_requested_at: dict[str, float] = {}


def _diagnostics_result_path() -> str:
    return os.environ.get(
        "JASPER_DIAGNOSTICS_RESULT_PATH",
        _DIAGNOSTICS_RESULT_PATH,
    )


def _diagnostics_cache_ttl_seconds() -> float:
    raw = os.environ.get("JASPER_DIAGNOSTICS_CACHE_TTL_SECONDS", "")
    if not raw:
        return _DIAGNOSTICS_CACHE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DIAGNOSTICS_CACHE_TTL_SECONDS
    if value < 0:
        return _DIAGNOSTICS_CACHE_TTL_SECONDS
    return value


def _diagnostics_refresh_min_interval_seconds() -> float:
    raw = os.environ.get("JASPER_DIAGNOSTICS_REFRESH_MIN_INTERVAL_SECONDS", "")
    if not raw:
        return _DIAGNOSTICS_REFRESH_MIN_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DIAGNOSTICS_REFRESH_MIN_INTERVAL_SECONDS
    if value < 0:
        return _DIAGNOSTICS_REFRESH_MIN_INTERVAL_SECONDS
    return value


def _start_diagnostics_refresh(result_path: str) -> tuple[bool, str]:
    now = time.monotonic()
    min_interval = _diagnostics_refresh_min_interval_seconds()
    with _diagnostics_refresh_lock:
        last = _diagnostics_refresh_requested_at.get(result_path, 0.0)
        if now - last < min_interval:
            return True, ""
        _diagnostics_refresh_requested_at[result_path] = now
    try:
        proc = subprocess.run(
            ["systemctl", "--no-block", "start", "jasper-doctor-json.service"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        with _diagnostics_refresh_lock:
            _diagnostics_refresh_requested_at.pop(result_path, None)
        return False, f"diagnostics refresh failed: {e}"
    if proc.returncode != 0:
        with _diagnostics_refresh_lock:
            _diagnostics_refresh_requested_at.pop(result_path, None)
        return (
            False,
            "diagnostics refresh unavailable: "
            + (proc.stderr or "").strip()[:300],
        )
    return True, ""


def _diagnostics_placeholder_result(
    *,
    detail: str,
    status: str = "warn",
) -> dict[str, Any]:
    return {
        "fails": 1 if status == "fail" else 0,
        "warns": 1 if status == "warn" else 0,
        "generated_at_epoch": None,
        "duration_sec": None,
        "cache_age_seconds": None,
        "stale": True,
        "refreshing": status != "fail",
        "results": [{
            "name": "jasper-doctor",
            "status": status,
            "detail": detail,
        }],
    }


def _append_diagnostics_refresh_failure(
    body: dict[str, Any],
    refresh_error: str,
) -> None:
    row = {
        "name": "jasper-doctor refresh",
        "status": "fail",
        "detail": refresh_error,
    }
    results = body.get("results")
    if isinstance(results, list):
        results.append(row)
    else:
        body["results"] = [row]
    try:
        body["fails"] = int(body.get("fails", 0)) + 1
    except (TypeError, ValueError):
        body["fails"] = 1
    body["refresh_error"] = refresh_error


def _read_diagnostics_snapshot(
    result_path: str,
    *,
    ttl_seconds: float,
) -> tuple[dict[str, Any] | None, str]:
    try:
        stat = os.stat(result_path)
        with open(result_path, encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)
    if not isinstance(body, dict):
        return None, "diagnostics result was not a JSON object"
    now = time.time()
    age = max(0.0, now - stat.st_mtime)
    body.setdefault("generated_at_epoch", stat.st_mtime)
    body["cache_age_seconds"] = round(age, 3)
    body["stale"] = age > ttl_seconds
    body.setdefault("refreshing", False)
    return body, ""


# Streambox is the restricted profile: it runs the local audio graph and
# sources but no voice brain or developer tools, so jasper-control gates
# its route surface to the management + audio actions a streambox box
# actually owns (full speakers allow everything).
_STREAMBOX_ALLOWED_GET_ROUTES = frozenset({
    "/healthz",
    "/volume",
    "/debug",
    "/grouping",
    "/system/snapshot",
    "/system/diagnostics",
    "/source/state",
    "/state",
})
_STREAMBOX_ALLOWED_POST_ROUTES = frozenset({
    "/volume/adjust",
    "/volume/set",
    "/grouping/set",
    "/volume/mute",
    "/debug",
    "/system/reboot",
    "/system/poweroff",
    "/source/select",
    "/system/audio-quality",
    "/system/restart/audio",
    "/transport/next",
    "/transport/previous",
    "/transport/toggle",
})


def _active_speaker_level_match_provisional(
    setup: dict[str, Any] | None,
) -> bool | None:
    """Whether the APPLIED active-speaker baseline's per-driver level match is a
    datasheet estimate rather than a phone measurement.

    Read from the SINGLE active-speaker readiness snapshot (`setup`) that the
    caller already computed via `read_active_speaker_setup_status`, not from a
    second off-disk open. That snapshot's `baseline_profile` summary derives
    `provisional` from the same persisted state file
    (`active_speaker_baseline_profile.json`) — re-reading it here was a duplicate
    source that could drift. The `status == "applied"` gate is preserved: the
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


def _active_speaker_output_safety_snapshot(
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
        # Back-compat for the landing-page field name. This is now driven by the
        # shared setup contract, not by a filename-only heuristic.
        "safety_muted": not bool(setup.get("volume_allowed")),
        "level_match_provisional": _active_speaker_level_match_provisional(setup),
        "source": "active_speaker.setup_status",
    }


def _active_speaker_volume_block() -> dict[str, Any] | None:
    setup = read_active_speaker_setup_status()
    if setup.get("active") and not setup.get("volume_allowed", False):
        return setup
    return None


def _active_speaker_grouping_block() -> dict[str, Any] | None:
    setup = read_active_speaker_setup_status()
    if setup.get("active") and not setup.get("grouping_allowed", False):
        return setup
    return None

# The high-impact mutations the control token gates (SECURITY.md).
# The primitive remains fail-safe-open when no /var/lib/jasper/control_token file
# exists, but jasper-control ensures one at startup so production installs are
# gated automatically.
# Deliberately NOT including /volume*, /transport*, /source* — the dial's
# bread-and-butter low-impact controls stay open (the dial never calls
# these). poweroff/reboot = power loop; mic/mute = defeats the privacy-mic
# promise; grouping/set = hijacks output routing; restart/voice|audio =
# disrupt playback + the assistant; aec/firmware/update downloads and flashes
# microphone firmware. WS1 Phase 2 added the two restart routes and made the
# gate mandatory (control_token.ensure_token() at startup, below).
_TOKEN_GATED_ROUTES = frozenset({
    "/system/poweroff",
    "/system/reboot",
    "/system/restart/voice",
    "/system/restart/audio",
    "/mic/mute",
    "/grouping/set",
    "/aec/firmware/update",
})


def _control_install_profile() -> str:
    try:
        return read_install_profile()
    except ValueError as e:
        log_event(
            logger,
            "install_profile.invalid",
            surface="jasper-control",
            error=repr(str(e)),
            level=logging.WARNING,
        )
        # Fail to the restricted profile so an unparseable marker can't
        # accidentally widen the route surface.
        return STREAMBOX_INSTALL_PROFILE


def _control_route_allowed_for_install_profile(
    profile: str,
    *,
    method: str,
    path: str,
) -> bool:
    role = install_role_for_profile(profile)
    if role == STREAMBOX_INSTALL_PROFILE:
        if method == "GET":
            return path in _STREAMBOX_ALLOWED_GET_ROUTES
        if method == "POST":
            return path in _STREAMBOX_ALLOWED_POST_ROUTES
        return False
    # Full speakers allow every route.
    return True


# _system_capabilities_for_profile was relocated to
# jasper.install_profile.system_capabilities_for_profile so install.sh can
# bake the same map into the static landing page (one source of truth).


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


CONTROL_MAX_POST_BYTES = _env_int("JASPER_CONTROL_MAX_POST_BYTES", 4096)
CONTROL_MAX_WORKERS = 8
CONTROL_REQUEST_QUEUE_SIZE = 16
CONTROL_REQUEST_TIMEOUT_SEC = 5.0
CONTROL_OVERLOAD_LOG_INTERVAL_SEC = 5.0
STATE_RESPONSE_CACHE_TTL_SEC = 1.0


_MISSING = object()


class _SingleFlightTTLCache:
    """Small thread-safe cache for expensive read-only JSON routes."""

    def __init__(
        self,
        ttl_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_sec = float(ttl_sec)
        self._clock = clock
        self._cond = threading.Condition()
        self._value: Any = _MISSING
        self._expires_at = 0.0
        self._inflight = False

    def get_or_compute(self, compute: Callable[[], Any]) -> Any:
        """Return a fresh value, sharing one in-flight computation.

        Only successful computations are cached. If the compute raises,
        waiters are released and the next caller may retry.

        `wait()` is intentionally un-timed: a waiter blocks only for as
        long as the single in-flight `compute()` runs, so the caller is
        responsible for passing a self-bounding `compute` (the /state
        aggregate enforces its own liveness budget). An unbounded compute
        would otherwise park every waiter — and, on the bounded request
        pool, the whole control plane — so keep that contract intact.
        """
        while True:
            with self._cond:
                now = self._clock()
                if self._value is not _MISSING and now < self._expires_at:
                    return self._value
                if not self._inflight:
                    self._inflight = True
                    break
                self._cond.wait()

        computed = False
        try:
            value = compute()
            computed = True
        finally:
            if not computed:
                with self._cond:
                    self._inflight = False
                    self._cond.notify_all()

        with self._cond:
            self._value = value
            self._expires_at = self._clock() + self._ttl_sec
            self._inflight = False
            self._cond.notify_all()
            return value


VOLUME_MIN_DB = _volume_ops.VOLUME_MIN_DB
VOLUME_MAX_DB = _volume_ops.VOLUME_MAX_DB
SPOTIFY_OAUTH_CALLBACK_BASE = _volume_ops.SPOTIFY_OAUTH_CALLBACK_BASE
_outputd_status = _state_aggregate._outputd_status
_clamp_db = _volume_ops._clamp_db
_db_to_percent = _volume_ops._db_to_percent
_percent_to_db = _volume_ops._percent_to_db
_delta_db_to_delta_percent = _volume_ops._delta_db_to_delta_percent
_spotify_redirect_uri = _volume_ops._spotify_redirect_uri


def _safe_audio_quality_state() -> dict[str, Any]:
    return _state_aggregate._safe_audio_quality_state()


def _parse_env_bool(raw: str, default: bool) -> bool:
    return _aec_endpoints._parse_env_bool(raw, default)


def _read_aec_state() -> dict:
    return _aec_endpoints._read_aec_state()


def _read_aec_mode() -> str:
    return _aec_endpoints._read_aec_mode()


def _write_aec_mode(mode: str) -> None:
    _aec_endpoints._write_aec_mode(mode)


def _write_aec_leg(leg: str, enabled: bool) -> None:
    _aec_endpoints._write_aec_leg(leg, enabled)


def _write_audio_input_profile(profile: str) -> None:
    _aec_endpoints._write_audio_input_profile(profile)


def _atomic_rewrite_env(path: str, updates: dict) -> None:
    _aec_endpoints._atomic_rewrite_env(path, updates)


def _read_wake_threshold() -> float:
    return _aec_endpoints._read_wake_threshold()


def _write_wake_threshold(value: float) -> None:
    _aec_endpoints._write_wake_threshold(value)


def _aec_bridge_active() -> bool:
    return _aec_endpoints._aec_bridge_active()


def _kick_aec_reconciler() -> None:
    _aec_endpoints._kick_aec_reconciler()


def _start_xvf_firmware_update() -> None:
    _aec_endpoints._start_xvf_firmware_update()


def _aec_full_status() -> dict:
    return _aec_endpoints._aec_full_status()


def _load_dial_heartbeat() -> dict[str, Any]:
    return _dial._load_dial_heartbeat()


def _persist_dial_heartbeat(snapshot: dict[str, Any]) -> None:
    _dial._persist_dial_heartbeat(snapshot)


async def _probe_dial_reachable(ip: str, *, timeout: float = 0.5) -> bool:
    return await _dial._probe_dial_reachable(ip, timeout=timeout)


def run_dial_log_listener(host: str, port: int) -> threading.Thread:
    return _dial.run_dial_log_listener(host, port)


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _state_aggregate._augment_source_payload(payload)


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    ha_status_snapshot: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return await _state_aggregate._get_state(
        camilla_host=camilla_host,
        camilla_port=camilla_port,
        voice_socket_path=voice_socket_path,
        voice_socket_command=_voice_socket_command,
        mux_socket_command=_mux_socket_command,
        local_status_json=_local_status_json,
        aec_full_status=_aec_full_status,
        dial_heartbeat=_dial._dial_heartbeat,
        dial_probe=_probe_dial_reachable,
        read_transit_state_func=read_transit_state,
        ha_status_snapshot=ha_status_snapshot,
    )


def _build_spotify_router_or_none():
    return _volume_ops._build_spotify_router_or_none()


async def _with_coordinator(
    op: Callable[[Any], Any],
    *,
    camilla_host: str,
    camilla_port: int,
    duck_active_probe: Optional[Callable[[], Awaitable[Optional[bool]]]] = None,
) -> Any:
    return await _volume_ops._with_coordinator(
        op,
        camilla_host=camilla_host,
        camilla_port=camilla_port,
        duck_active_probe=duck_active_probe,
    )


def _make_duck_active_probe(
    voice_socket_path: str,
) -> Callable[[], Awaitable[Optional[bool]]]:
    return _volume_ops._make_duck_active_probe(
        voice_socket_path,
        voice_socket_command=_voice_socket_command,
    )


async def _dispatch_transport(action: str) -> dict:
    return await _volume_ops._dispatch_transport(
        action,
        spotify_router_factory=_build_spotify_router_or_none,
    )


# ---------- peering daemon supervisor ----------

# The peering daemon runs an asyncio event loop; jasper-control is
# stdlib threaded HTTP. Bridge by spawning a single background daemon
# thread that owns the asyncio loop for peering. When peering is OFF
# (the default), the thread is not even created — zero cost on a
# single-Pi household.
_peering_thread: threading.Thread | None = None


def _run_peering_loop() -> None:
    """Background thread target: own an asyncio loop, run the
    PeeringDaemon until the process exits."""
    global _peering_loop, _peering_thread
    # Lazy imports — keep jasper-control's import cost light when
    # peering is OFF and these modules never load.
    from ..peering import load_config
    from ..peering.daemon import PeeringDaemon

    cfg = load_config()
    if not cfg.enabled:
        log_event(
            logger,
            "peering.thread.exit",
            mode=cfg.mode.value,
            note="daemon will not start",
        )
        with _peering_lock:
            if _peering_thread is threading.current_thread():
                _peering_thread = None
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = PeeringDaemon(cfg)
    with _peering_lock:
        _peering_loop = loop
    try:
        loop.run_until_complete(daemon.start())
        if _peering_stop_requested.is_set():
            loop.call_soon(loop.stop)
        loop.run_forever()
    except Exception:  # noqa: BLE001
        logger.exception("peering daemon thread crashed")
    finally:
        try:
            loop.run_until_complete(daemon.stop())
        except Exception:  # noqa: BLE001
            logger.exception("peering daemon stop failed")
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
        with _peering_lock:
            if _peering_loop is loop:
                _peering_loop = None
            if _peering_thread is threading.current_thread():
                _peering_thread = None
            _peering_stop_requested.clear()


def start_peering_daemon_if_enabled() -> None:
    """Start the peering daemon in a background thread iff peering
    is enabled in /var/lib/jasper/peering.env. Idempotent — repeated
    calls are no-ops once the thread exists.

    The check is done in the worker thread (not here) so that even
    when peering is OFF, we don't pay the cost of importing zeroconf.
    """
    global _peering_thread
    if _peering_thread is not None:
        return
    _peering_stop_requested.clear()
    _peering_thread = threading.Thread(
        target=_run_peering_loop,
        name="peering-daemon",
        daemon=True,
    )
    _peering_thread.start()


def stop_peering_daemon(*, timeout: float = 5.0) -> None:
    """Stop the background peering loop so daemon.stop() can unpublish mDNS."""
    with _peering_lock:
        thread = _peering_thread
        loop = _peering_loop
    if thread is None:
        return
    _peering_stop_requested.set()
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    if thread is threading.current_thread():
        return
    thread.join(timeout=timeout)
    if thread.is_alive():
        log_event(
            logger,
            "peering.thread.stop_timeout",
            timeout=f"{timeout:.1f}",
            level=logging.WARNING,
        )


# Forwarded pair action requests carry this header; its presence stops a
# second hop (see _maybe_forward_pair_action_to_leader's loop breaker).
_PAIR_FORWARD_HEADER = "X-JTS-Pair-Forwarded"
_GROUPING_RECONCILE_KICK_HELPER = (
    "/usr/local/sbin/jasper-grouping-reconcile-kick"
)
_GROUPING_RECONCILE_TRAILING_UNIT = "jasper-grouping-reconcile-trailing.service"
_GROUPING_RECONCILE_TRAILING_DELAY_FILE = (
    "/run/jasper-control/grouping-reconcile-trailing-delay"
)
_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS = 60.0
_VOICE_UNIT = "jasper-voice.service"
_VOICE_TRANSIENT_ACTIVE_STATES = frozenset({
    "activating",
    "deactivating",
    "reloading",
})

# Seam for tests: the forward's ONE network call. Patching the stdlib
# urllib.request.urlopen would also intercept the test driver's own HTTP
# client (and anything else in-process); this alias scopes the double to
# the pair forward.
_pair_urlopen = urllib.request.urlopen


def _pair_follower_leader_addr() -> str | None:
    """The leader's handle when THIS speaker is an active bonded follower,
    else None. One tiny env-file read per call (multiroom.config.load_config
    — never the runtime derive with its systemctl/RPC probes: this gates
    every /volume request). The predicate itself is the shared
    effective-role reader, so a refused bond that safely landed solo does not
    forward local controls to the requested leader."""
    from ..multiroom.config import load_config
    from ..multiroom.effective_role import effective_follower_leader_addr

    return effective_follower_leader_addr(load_config())


def _bonded_follower_mic_payload(leader: str) -> dict[str, Any]:
    return {
        "status": "parked",
        "reason": "bonded_follower",
        "available": False,
        "muted": True,
        "pair_leader": leader,
        "message": "Paired — the assistant listens on the pair leader",
    }


def _systemd_show_unit(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = 1.0,
) -> dict[str, str]:
    """Tiny, fail-soft systemd state reader for user-facing liveness labels."""
    try:
        proc = run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--no-page",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _voice_starting_mic_payload(
    *,
    read_unit: Callable[[str], dict[str, str]] = _systemd_show_unit,
) -> dict[str, Any] | None:
    """Return a first-class /mic payload while jasper-voice is in flight.

    The voice daemon creates its UDS socket late in startup. During an intended
    restart/provider switch/unbond, a missing socket means "not ready yet", not
    necessarily "permanently offline". Keep that distinction in the backend so
    the landing page can stay a dumb renderer of /mic state.
    """
    unit = read_unit(_VOICE_UNIT)
    active_state = unit.get("ActiveState", "")
    if active_state not in _VOICE_TRANSIENT_ACTIVE_STATES:
        return None
    return {
        "status": "starting",
        "reason": "voice_daemon_starting",
        "available": False,
        "muted": True,
        "message": "Voice control is restarting",
        "unit": {
            "name": _VOICE_UNIT,
            "active_state": active_state or None,
            "sub_state": unit.get("SubState") or None,
            "result": unit.get("Result") or None,
        },
    }


def _voice_offline_mic_payload(error: str) -> dict[str, Any]:
    return {
        "status": "offline",
        "reason": "voice_daemon_unreachable",
        "available": False,
        "muted": True,
        "message": "Voice control offline",
        "error": error,
    }


def _launch_grouping_reconciler_kick(reason: str) -> None:
    log_event(
        logger,
        "grouping.reconciler_kick",
        reason=reason,
    )
    subprocess.Popen(
        [_GROUPING_RECONCILE_KICK_HELPER],
    )


def _cancel_grouping_reconciler_trailing_service() -> None:
    try:
        subprocess.Popen(
            [
                "systemctl",
                "stop",
                "--no-block",
                _GROUPING_RECONCILE_TRAILING_UNIT,
            ],
        )
    except OSError:
        logger.debug("grouping reconciler trailing service cancel failed", exc_info=True)


def _write_grouping_reconciler_trailing_delay(delay_s: float) -> None:
    delay_seconds = max(
        0,
        min(
            math.ceil(delay_s),
            math.ceil(_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS),
        ),
    )
    path = Path(_GROUPING_RECONCILE_TRAILING_DELAY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{delay_seconds}\n", encoding="ascii")


def _arm_grouping_reconciler_trailing_service(delay_s: float) -> None:
    _write_grouping_reconciler_trailing_delay(delay_s)
    subprocess.run(
        [
            "systemctl",
            "restart",
            "--no-block",
            _GROUPING_RECONCILE_TRAILING_UNIT,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class _ThreadingTrailingKickHandle:
    def __init__(
        self,
        delay_s: float,
        callback: Callable[[], None],
        timer_factory: Callable[[float, Callable[[], None]], Any],
    ) -> None:
        self._timer = timer_factory(delay_s, callback)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()


class _SystemdServiceTrailingKickHandle:
    def __init__(
        self,
        delay_s: float,
        mark_applied: Callable[[], None],
        timer_factory: Callable[[float, Callable[[], None]], Any],
    ) -> None:
        _arm_grouping_reconciler_trailing_service(delay_s)
        mark_timer = timer_factory(delay_s, mark_applied)
        mark_timer.daemon = True
        mark_timer.start()
        self._mark_timer = mark_timer

    def cancel(self) -> None:
        self._mark_timer.cancel()
        _cancel_grouping_reconciler_trailing_service()


def _schedule_grouping_reconciler_trailing_kick(
    delay_s: float,
    run_trailing: Callable[[], None],
    mark_applied: Callable[[], None],
    *,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
) -> _SystemdServiceTrailingKickHandle | _ThreadingTrailingKickHandle:
    try:
        handle = _SystemdServiceTrailingKickHandle(
            delay_s,
            mark_applied,
            timer_factory,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        log_event(
            logger,
            "grouping.reconciler_trailing_schedule_fallback",
            delay_s=f"{delay_s:.3f}",
            scheduler="threading.Timer",
            error=str(exc),
            level=logging.WARNING,
        )
        return _ThreadingTrailingKickHandle(delay_s, run_trailing, timer_factory)

    log_event(
        logger,
        "grouping.reconciler_trailing_scheduled",
        delay_s=f"{delay_s:.3f}",
        scheduler="systemd-service",
        unit=_GROUPING_RECONCILE_TRAILING_UNIT,
    )
    return handle


class _GroupingReconcilerKickCoalescer:
    """Leading-edge rate limit with a trailing guarantee for /grouping/set.

    The HTTP handler writes grouping.env before calling this. If updates arrive
    faster than the minimum interval, one delayed kick is enough. The packaged
    trailing service survives a jasper-control restart and the oneshot reconciler
    re-reads grouping.env when it finally runs, so the last write wins without
    restarting outputd for every trim/delay/crossover sweep step.
    """

    def __init__(
        self,
        *,
        cooldown_s: float,
        launch: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        trailing_scheduler: Callable[
            [float, Callable[[], None], Callable[[], None]],
            Any,
        ] = _schedule_grouping_reconciler_trailing_kick,
        cancel_external_trailing: Callable[
            [], None
        ] = _cancel_grouping_reconciler_trailing_service,
    ) -> None:
        self._cooldown_s = float(cooldown_s)
        self._launch = launch
        self._clock = clock
        self._trailing_scheduler = trailing_scheduler
        self._cancel_external_trailing = cancel_external_trailing
        self._lock = threading.Lock()
        self._last_kick_at: float | None = None
        self._trailing_handle: Any | None = None

    def reset_for_tests(self) -> None:
        with self._lock:
            if self._trailing_handle is not None:
                self._trailing_handle.cancel()
            self._trailing_handle = None
            self._last_kick_at = None

    def kick(self) -> None:
        """Kick now if the cooldown is clear, else arm one trailing kick."""
        reason: str | None = None
        launched_at: float | None = None
        with self._lock:
            now = self._clock()
            elapsed = (
                None if self._last_kick_at is None else now - self._last_kick_at
            )
            if elapsed is None or elapsed >= self._cooldown_s:
                if self._trailing_handle is not None:
                    self._trailing_handle.cancel()
                    self._trailing_handle = None
                else:
                    self._cancel_external_trailing()
                self._last_kick_at = now
                launched_at = now
                reason = "leading"
            else:
                remaining = max(0.0, self._cooldown_s - elapsed)
                if self._trailing_handle is None:
                    self._trailing_handle = self._trailing_scheduler(
                        remaining,
                        self._run_trailing,
                        self._mark_trailing_applied,
                    )
                    log_event(
                        logger,
                        "grouping.reconciler_kick_coalesced",
                        delay_s=f"{remaining:.3f}",
                        cooldown_s=f"{self._cooldown_s:.3f}",
                    )
                else:
                    log_event(
                        logger,
                        "grouping.reconciler_kick_already_pending",
                        cooldown_s=f"{self._cooldown_s:.3f}",
                        level=logging.DEBUG,
                    )
                return
        assert reason is not None
        try:
            self._launch(reason)
        except OSError:
            with self._lock:
                if (
                    launched_at is not None
                    and self._last_kick_at == launched_at
                    and self._trailing_handle is None
                ):
                    self._last_kick_at = None
            raise

    def _run_trailing(self) -> None:
        with self._lock:
            self._trailing_handle = None
            self._last_kick_at = self._clock()
        try:
            self._launch("trailing")
        except OSError:
            logger.exception("grouping reconciler trailing kick failed")

    def _mark_trailing_applied(self) -> None:
        with self._lock:
            self._trailing_handle = None
            self._last_kick_at = self._clock()


_grouping_reconciler_kick_coalescer = _GroupingReconcilerKickCoalescer(
    cooldown_s=_GROUPING_RECONCILE_KICK_MIN_INTERVAL_SECONDS,
    launch=_launch_grouping_reconciler_kick,
)


def _reset_grouping_reconciler_kick_coalescer_for_tests() -> None:
    _grouping_reconciler_kick_coalescer.reset_for_tests()


def _kick_grouping_reconciler() -> None:
    """Apply a persisted grouping change through jasper-grouping-reconcile.

    The reconciler is the single applier of snapcast state and outputd grouping
    env. A fixed helper performs a blocking ``systemctl start`` so an active
    Type=oneshot pass drains before it launches one fresh pass. This caller also
    coalesces rapid /grouping/set bursts so trim/delay/crossover sweeps do not tear
    down outputd on every intermediate value. A skipped kick always arms one
    trailing retry; the final grouping.env write is therefore applied.
    """
    _grouping_reconciler_kick_coalescer.kick()


def _is_trim_only_grouping_change(before: GroupingConfig, after: GroupingConfig) -> bool:
    """True when the persisted grouping diff is only pair-balance trim."""
    return (
        before.enabled
        and after.enabled
        and before.error is None
        and after.error is None
        and before.role == after.role
        and before.channel == after.channel
        and before.bond_id == after.bond_id
        and before.leader_addr == after.leader_addr
        and before.buffer_ms == after.buffer_ms
        and before.codec == after.codec
        and before.client_latency_ms == after.client_latency_ms
        and math.isclose(before.left_delay_ms, after.left_delay_ms, abs_tol=0.0005)
        and math.isclose(before.right_delay_ms, after.right_delay_ms, abs_tol=0.0005)
        and math.isclose(before.crossover_hz, after.crossover_hz, abs_tol=0.0005)
        and before.mains_highpass_enabled == after.mains_highpass_enabled
        and before.subwoofer_present == after.subwoofer_present
        and before.peer_addr == after.peer_addr
        and before.peer_name == after.peer_name
        and before.roster == after.roster
        and not math.isclose(before.trim_db, after.trim_db, abs_tol=0.0005)
    )


@dataclass(frozen=True)
class _GroupingOptionalFields:
    trim_db: float | None
    client_latency_ms: int | None
    left_delay_ms: float | None
    right_delay_ms: float | None
    crossover_hz: float | None
    mains_highpass_enabled: bool | None
    subwoofer_present: bool | None


def _parse_grouping_optional_fields(
    body: dict[str, Any],
) -> tuple[_GroupingOptionalFields | None, str | None]:
    """Parse optional ``/grouping/set`` scalars without HTTP side effects.

    Numeric fields intentionally retain Python ``int``/``float`` coercion;
    the two flags retain their stricter JSON-boolean-only contract.
    """
    parsed: dict[str, Any] = {}
    for key, caster, error in (
        ("trim_db", float, "trim_db must be a number"),
        (
            "client_latency_ms",
            int,
            "client_latency_ms must be an integer",
        ),
        ("left_delay_ms", float, "left_delay_ms must be a number"),
        ("right_delay_ms", float, "right_delay_ms must be a number"),
        ("crossover_hz", float, "crossover_hz must be a number"),
    ):
        if key not in body:
            continue
        try:
            parsed[key] = caster(body[key])
        except (TypeError, ValueError):
            return None, error

    for key in ("mains_highpass_enabled", "subwoofer_present"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, bool):
            return None, f"{key} must be boolean"
        parsed[key] = value

    return _GroupingOptionalFields(
        trim_db=parsed.get("trim_db"),
        client_latency_ms=parsed.get("client_latency_ms"),
        left_delay_ms=parsed.get("left_delay_ms"),
        right_delay_ms=parsed.get("right_delay_ms"),
        crossover_hz=parsed.get("crossover_hz"),
        mains_highpass_enabled=parsed.get("mains_highpass_enabled"),
        subwoofer_present=parsed.get("subwoofer_present"),
    ), None


def _resolve_grouping_crossover_hz_for_write(
    *,
    channel: str,
    subwoofer_present: bool,
    requested: float | None,
) -> float | None:
    """Return the crossover value this /grouping/set write must persist.

    Plain stereo writes retain the historical omitted-means-preserve contract.
    A sub channel or sub-present bond actively consumes the corner, though, so
    an omitted request must validate and write the same value: a valid existing
    operator value when one is present, otherwise the safe default.
    """
    if requested is not None:
        return requested
    if channel != "sub" and not subwoofer_present:
        return None
    existing = load_grouping_config(GROUPING_ENV_FILE).crossover_hz
    if CROSSOVER_HZ_LO <= existing <= CROSSOVER_HZ_HI:
        return existing
    return DEFAULT_CROSSOVER_HZ


def _write_grouping(
    *, enabled: bool, role: str, channel: str, bond_id: str, leader_addr: str,
    trim_db: "float | None" = None,
    client_latency_ms: "int | None" = None,
    left_delay_ms: "float | None" = None,
    right_delay_ms: "float | None" = None,
    crossover_hz: "float | None" = None,
    mains_highpass_enabled: "bool | None" = None,
    subwoofer_present: "bool | None" = None,
    peer_addr: "str | None" = None,
    peer_name: "str | None" = None,
    roster: "str | None" = None,
) -> None:
    """Persist a grouping role into the wizard-owned grouping.env.

    Read-modify-write (via _atomic_rewrite_env) so operator-tuned
    JASPER_GROUPING_BUFFER_MS / _CODEC survive a role change. This is the
    single control-plane WRITER of grouping.env; jasper-grouping-reconcile is
    the single READER->action. The endpoint that calls this (/grouping/set) is
    token-gated (WS1 Phase 2); the cross-device bond-forming flow — one speaker
    POSTing to another's :PORT/grouping/set — authenticates with the household
    credential (docs/HANDOFF-control-plane-auth.md).
    """
    updates = {
        "JASPER_GROUPING": "on" if enabled else "off",
        "JASPER_GROUPING_ROLE": role,
        "JASPER_GROUPING_CHANNEL": channel,
        "JASPER_GROUPING_BOND_ID": bond_id,
        "JASPER_GROUPING_LEADER_ADDR": leader_addr,
    }
    if trim_db is not None:
        # Settable like the role fields, preserved like codec when the
        # caller omits it. Existing-bond structural edits omit trim so a
        # calibrated balance survives role/channel changes; fresh bond and
        # unbond flows send trim=0 to clear stale balance state.
        updates["JASPER_GROUPING_TRIM_DB"] = f"{trim_db:.1f}"
    if client_latency_ms is not None:
        updates["JASPER_GROUPING_CLIENT_LATENCY_MS"] = str(int(client_latency_ms))
    if left_delay_ms is not None:
        updates["JASPER_GROUPING_LEFT_DELAY_MS"] = f"{left_delay_ms:.3f}"
    if right_delay_ms is not None:
        updates["JASPER_GROUPING_RIGHT_DELAY_MS"] = f"{right_delay_ms:.3f}"
    if crossover_hz is not None:
        # Receiver-side wireless-sub corner. Settable like the role fields,
        # preserved like codec when the caller omits it (only meaningful for
        # channel="sub", but persisted regardless so a sub<->non-sub flip
        # keeps the operator's chosen corner).
        updates["JASPER_GROUPING_CROSSOVER_HZ"] = f"{crossover_hz:g}"
    if mains_highpass_enabled is not None:
        updates["JASPER_GROUPING_MAINS_HIGHPASS"] = (
            "on" if mains_highpass_enabled else "off"
        )
    if subwoofer_present is not None:
        updates["JASPER_GROUPING_SUBWOOFER_PRESENT"] = (
            "on" if subwoofer_present else "off"
        )
    # Bond roster (leader only): same preserved-when-omitted contract as
    # trim; an EXPLICIT empty string clears it (the bond flow clears the
    # roster on non-leader members so a role flip can't leave a stale
    # roster behind).
    if peer_addr is not None:
        updates["JASPER_GROUPING_PEER_ADDR"] = peer_addr
    if peer_name is not None:
        updates["JASPER_GROUPING_PEER_NAME"] = peer_name
    # The full bond roster (leader only): same preserved-when-omitted /
    # explicit-empty-clears contract as peer_addr — `roster` is the already
    # SERIALIZED env string (the caller builds it via config.format_roster).
    if roster is not None:
        updates["JASPER_GROUPING_ROSTER"] = roster
    _atomic_rewrite_env(GROUPING_ENV_FILE, updates)







def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
    airplay_health_sampler: Any = None,
    audio_health_sampler: Any = None,
    ha_status_cache: Any = None,
) -> type[BaseHTTPRequestHandler]:

    # One probe instance per handler — it's stateless (just closes
    # over voice_socket_path), so all volume ops share it. Read-only
    # `_get_op` doesn't need it (`get_listening_level` doesn't touch
    # camilla), but passing None there keeps the construction uniform.
    duck_active_probe = _make_duck_active_probe(voice_socket_path)
    state_response_cache = _SingleFlightTTLCache(STATE_RESPONSE_CACHE_TTL_SEC)
    if ha_status_cache is None:
        from .ha_status_cache import HomeAssistantStatusCache

        ha_status_cache = HomeAssistantStatusCache()

    async def _set_op(percent: int):
        async def _op(coord):
            return await coord.set_listening_level(percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _observe_op(source_name: str, percent: int) -> int:
        """Route a source-observed volume change (e.g. host slider on
        the USB gadget) through the coordinator's echo-prevented
        observe path. Unknown source names fall back to the
        authoritative set path so a future client that posts a fresh
        source name doesn't silently no-op.

        Returns the level the coordinator ended up at — equal to
        `percent` on normal observe (no echo) or the prior value if
        the observation was treated as an echo of our own write."""
        # Lazy import to avoid pulling the full volume_coordinator
        # graph into the import path of server.py's module load.
        from ..volume_coordinator import Source
        try:
            source_enum = Source(source_name)
        except ValueError:
            # Unknown source — treat as authoritative.
            return await _set_op(percent)

        async def _op(coord):
            await coord.observe_source_volume(source_enum, percent)
            # The coordinator's level either took our value or stayed
            # put (echo-suppressed). Return whatever's now canonical
            # for the client to render.
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _adjust_op(delta_percent: int):
        async def _op(coord):
            return await coord.adjust_listening_level(delta_percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _get_op():
        async def _op(coord):
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    async def _mute_set_op(want_muted: bool):
        async def _op(coord):
            # Explicit set, idempotent: mute-when-muted stays muted,
            # unmute-when-unmuted returns the current level untouched.
            # Voice has distinct mute/unmute INTENTS, so it needs this
            # rather than the toggle (a toggle would invert a stale
            # intent — "mute" while already muted must not unmute).
            if want_muted:
                if not coord.is_muted():
                    await coord.mute()
                return 0
            if coord.is_muted():
                return await coord.unmute()
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _mute_toggle_op():
        async def _op(coord):
            # If currently muted, unmute and return restored level.
            # Otherwise mute and return 0 (the new actual level).
            if coord.is_muted():
                return await coord.unmute()
            await coord.mute()
            return 0
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            """Return a JSON object body; empty/malformed/non-object => {}.

            The mutating-request guard owns Content-Length validation before
            any POST handler reaches this helper.
            """
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > CONTROL_MAX_POST_BYTES:
                raise ValueError("invalid body length")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        def _voice_cmd_or_error(
            self,
            cmd: str,
            *,
            timeout: float | None = None,
            missing_error: str | None = "voice_daemon not running",
            log_label: str = "voice command",
        ) -> dict[str, Any] | None:
            try:
                kwargs = {} if timeout is None else {"timeout": timeout}
                return asyncio.run(
                    _voice_socket_command(voice_socket_path, cmd, **kwargs),
                )
            except FileNotFoundError as e:
                error = (
                    f"voice_daemon unreachable: {e}"
                    if missing_error is None else missing_error
                )
                self._send_json({"error": error}, status=503)
                return None
            except (OSError, asyncio.TimeoutError) as e:
                self._send_json(
                    {"error": f"voice_daemon unreachable: {e}"},
                    status=503,
                )
                return None
            except Exception as e:  # noqa: BLE001
                logger.exception("%s failed", log_label)
                self._send_json({"error": str(e)}, status=502)
                return None

        def _guard_management_read(self) -> bool:
            if self.path == "/healthz":
                ok, reason = management_read_allowed({
                    "Host": self.headers.get("Host") or "",
                })
            else:
                ok, reason = management_read_allowed(self.headers)
            if ok:
                return True
            log_event(
                logger,
                "http.reject",
                reason=reason,
                host=repr(self.headers.get("Host")),
                sec_fetch_site=repr(self.headers.get("Sec-Fetch-Site")),
                path=self.path,
                client=self.address_string(),
                level=logging.WARNING,
            )
            self._send_json({"error": reason}, status=403)
            return False

        def _guard_mutating_request(self) -> bool:
            ok, reason = mutating_request_allowed(self.headers)
            if not ok:
                log_event(
                    logger,
                    "http.reject",
                    reason=reason,
                    host=repr(self.headers.get("Host")),
                    origin=repr(self.headers.get("Origin")),
                    path=self.path,
                    client=self.address_string(),
                    level=logging.WARNING,
                )
                self._send_json({"error": reason}, status=403)
                return False
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._send_json({"error": "invalid_content_length"}, status=400)
                return False
            if length < 0:
                self._send_json({"error": "invalid_content_length"}, status=400)
                return False
            if length > CONTROL_MAX_POST_BYTES:
                log_event(
                    logger,
                    "http.reject",
                    reason="body_too_large",
                    bytes=length,
                    limit=CONTROL_MAX_POST_BYTES,
                    path=self.path,
                    client=self.address_string(),
                    level=logging.WARNING,
                )
                self._send_json(
                    {
                        "error": "request_body_too_large",
                        "max_bytes": CONTROL_MAX_POST_BYTES,
                    },
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return False
            return True

        def _guard_install_profile_route(self) -> bool:
            profile = _control_install_profile()
            if _control_route_allowed_for_install_profile(
                profile,
                method=self.command,
                path=self.path,
            ):
                return True
            log_event(
                logger,
                "control.route_blocked",
                profile=profile,
                method=self.command,
                path=self.path,
                client=self.address_string(),
                level=logging.WARNING,
            )
            self.send_error(HTTPStatus.NOT_FOUND)
            return False

        def _volume_payload(self, percent: int) -> dict[str, Any]:
            # `db` is computed for back-compat with the dial firmware
            # which reads `percent` but logs `db`. Use the same calibrated
            # curve as the live audio path so diagnostics match what is heard.
            return {"db": round(_percent_to_db(percent), 3), "percent": percent}

        def _maybe_forward_pair_action_to_leader(self) -> bool:
            """Bonded-follower pair-action proxy. Returns True when the request
            was handled (forwarded or rejected) and the caller must stop.

            Used by the four /volume* handlers, /transport/*, and
            /source/select — every surface where a bonded follower's
            local action must target the PAIR. While this speaker is an
            ACTIVE bonded follower,
            its local volume knobs are INERT — bonded content bypasses the local
            CamillaDSP entirely (the leader's one Camilla bakes the
            program; HANDOFF-multiroom.md §2). Without this, the landing
            page slider, a paired dial, and curl all "work" silently
            with no audible effect — the worst UX shape. So the four
            /volume endpoints forward verbatim to the leader's control
            API and relay its answer: every member's volume surface
            controls the PAIR volume, whichever speaker's page you have
            open. Solo and leader requests never enter this path; the
            grouping read is one tiny env-file parse (load_config), NOT
            the heavy runtime derive — this sits on every volume call.
            """
            leader = _pair_follower_leader_addr()
            if leader is None:
                return False
            # Loop breaker: a forwarded request never re-forwards. Two
            # speakers misconfigured as each other's follower would
            # otherwise ping-pong until a timeout stack built up.
            if self.headers.get(_PAIR_FORWARD_HEADER):
                # Drain any request body before responding so the
                # connection state stays sane if keep-alive is ever
                # enabled (HTTP/1.0 today, so this is pure hygiene).
                try:
                    stale = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    stale = 0
                if self.command == "POST" and stale > 0:
                    self.rfile.read(stale)
                self._send_json(
                    {"error": "pair forward loop (both speakers are "
                              "followers?)", "pair_leader": leader},
                    status=502,
                )
                return True
            body: bytes | None = None
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length > 0 else b"{}"
            url = "http://{}:{}{}".format(
                leader, self.server.server_address[1], self.path,
            )
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    _PAIR_FORWARD_HEADER: "1",
                },
                method=self.command,
            )
            try:
                with _pair_urlopen(req, timeout=2.5) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                # The leader ANSWERED — relay its verdict (status + JSON
                # body) verbatim, tagged. Collapsing a 400 invalid-body
                # reject into "unreachable" would tell the household a
                # responding speaker is offline.
                try:
                    relayed = json.loads(e.read().decode())
                except Exception:  # noqa: BLE001 — non-JSON error body
                    relayed = {"error": f"pair leader error: {e}"}
                if isinstance(relayed, dict):
                    relayed.setdefault("pair_leader", leader)
                log_event(
                    logger,
                    "pair.action_forward_rejected",
                    leader=leader,
                    path=self.path,
                    status=e.code,
                    level=logging.WARNING,
                )
                self._send_json(relayed, status=e.code)
                return True
            except Exception as e:  # noqa: BLE001 — transport failure: 502
                log_event(
                    logger,
                    "pair.action_forward_failed",
                    leader=leader,
                    path=self.path,
                    error=str(e),
                    level=logging.WARNING,
                )
                self._send_json(
                    {"error": f"pair leader unreachable: {e}",
                     "pair_leader": leader},
                    status=502,
                )
                return True
            if isinstance(payload, dict):
                # Additive marker so UIs can label the slider "pair
                # volume"; dial firmware reads only db/percent.
                payload.setdefault("pair_leader", leader)
            self._send_json(payload)
            return True

        # --- routes ---
        #
        # do_GET / do_POST own the dispatch via the _GET_ROUTES /
        # _POST_ROUTES tables (path -> handler-method name) defined at the
        # bottom of this class. Each table entry's handler holds the exact
        # body the inlined `if self.path == ...` branch had — moved into a
        # named method, logic unchanged.
        #
        # SECURITY ORDERING IS LOAD-BEARING: the management-read /
        # mutating-request guard runs FIRST, then install-profile route
        # scope, and the ordinary table lookup happens LAST. So an
        # unknown path under a hostile Host/Origin is still rejected by
        # the guard (403/400/413) BEFORE it can 404 — the inverse of the
        # web-wizard "route-check before guard" convention, preserved here
        # on purpose. Do not reorder lookup ahead of the guard.

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard_management_read():
                return
            if not self._guard_install_profile_route():
                return
            handler_name = self._GET_ROUTES.get(self.path)
            if handler_name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            getattr(self, handler_name)()

        def _get_healthz(self) -> None:
            self._send_json({"ok": True})

        def _get_volume(self) -> None:
            if self._maybe_forward_pair_action_to_leader():
                return
            try:
                percent = asyncio.run(_get_op())
            except Exception as e:  # noqa: BLE001
                logger.exception("get volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(self._volume_payload(percent))

        def _get_mic(self) -> None:
            # Read mic mute state from the voice daemon's STATUS
            # response. A bonded follower intentionally parks local
            # voice, and a daemon restart temporarily lacks its UDS socket;
            # report both as first-class states instead of making every
            # client reinterpret a missing UDS as failure.
            leader = _pair_follower_leader_addr()
            if leader:
                self._send_json(_bonded_follower_mic_payload(leader))
                return
            try:
                st = asyncio.run(
                    _voice_socket_command(voice_socket_path, "STATUS", timeout=2.0),
                )
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                starting = _voice_starting_mic_payload()
                if starting is not None:
                    self._send_json(starting)
                    return
                self._send_json(
                    _voice_offline_mic_payload(f"voice_daemon unreachable: {e}"),
                    status=503,
                )
                return
            except (RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.exception("mic STATUS failed")
                self._send_json({"error": str(e)}, status=502)
                return
            muted = bool(st.get("mic_muted", False))
            self._send_json({
                "status": "muted" if muted else "listening",
                "available": True,
                "muted": muted,
            })

        def _get_source_state(self) -> None:
            # Source selection state from jasper-mux. This is
            # separate from the /sources/ wizard (on/off toggles):
            # selecting a source does not enable or disable any
            # renderer, it only chooses which active lane the
            # speaker should pass through.
            try:
                result = asyncio.run(_mux_socket_command("STATUS"))
            except (
                FileNotFoundError,
                ConnectionRefusedError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                self._send_json(
                    {"error": f"jasper-mux unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("source STATUS failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(_augment_source_payload(result))

        def _get_aec(self) -> None:
            # AEC bridge state + per-leg config + wake
            # threshold. Mode and leg booleans are the persisted
            # request (what the operator asked for, via the /wake/
            # page or aec_mode.env directly); bridge_active is the
            # observed truth from systemd. They diverge briefly
            # during a reconciler-driven transition (~10-15 s).
            # Threshold is read from wake_model.env — the same
            # file the /wake/ form save writes the model into, so
            # both controls stay in sync without sharing code.
            #
            # DTLN load failures don't surface in this payload —
            # /system's Diagnostics disclosure runs jasper-doctor
            # which has check_aec_bridge_dtln_engine for the
            # silent-failure case.
            self._send_json(_aec_full_status())

        def _get_debug(self) -> None:
            # Runtime debug-logging state for the /system Debug card:
            # per-subsystem on/off + the shared auto-expiry countdown.
            self._send_json(debug_control.snapshot())

        def _get_state(self) -> None:
            # Cross-daemon snapshot — voice / audio / renderers /
            # satellites. Polled by the /voice web UI for live
            # status, used by jasper-doctor for one-shot health,
            # and consumable from `curl jts.local:8780/state | jq`
            # for ad-hoc debugging. ~200 ms typical (mostly the
            # parallel busctl + camilla WS probes).
            try:
                state = state_response_cache.get_or_compute(
                    lambda: asyncio.run(_get_state(
                        camilla_host=camilla_host,
                        camilla_port=camilla_port,
                        voice_socket_path=voice_socket_path,
                        ha_status_snapshot=ha_status_cache.snapshot,
                    )),
                )
                # The audio-health sampler is the single normalized health
                # contract shared by /state and /system/snapshot. Copy the
                # cached aggregate before attaching it so the cross-request
                # state cache never retains a stale health object.
                if audio_health_sampler is not None:
                    state = dict(state)
                    try:
                        state["audio_health"] = audio_health_sampler.snapshot()
                    except (
                        AttributeError,
                        KeyError,
                        OSError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ):
                        logger.exception("/state audio health snapshot failed")
                        state["audio_health"] = None
            except Exception as e:  # noqa: BLE001
                logger.exception("/state aggregation failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(state)

        def _get_grouping(self) -> None:
            # Multiroom grouping block, nested under "grouping" so a
            # fail-soft read returns {"grouping": null} unambiguously.
            # Read SERVER-SIDE by another speaker's /rooms /unbond
            # fan-out (rooms_setup._get_member_grouping) to discover which
            # siblings share a bond_id, AND by the browser: the landing
            # page's stereo-pair banner polls it every 10 s through
            # nginx's exact-match /grouping proxy.
            # NO CSRF: a read on the same no-auth LAN surface as /state
            # and /healthz. Fail-soft like /state's grouping section —
            # a broken read returns 200 with null rather than 500.
            try:
                grouping = read_grouping_state()
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                logger.exception("grouping state read failed")
                grouping = None
            # grouping_response is the ONE home for the envelope shape; the
            # /rooms /unbond consumer parses it via the paired
            # parse_grouping_response (jasper/multiroom/state.py), so the
            # two daemons can't drift (the C4 regression).
            self._send_json(grouping_response(grouping))

        def _get_dial_status(self) -> None:
            # Heartbeat snapshot — used by jasper-doctor's
            # "is the dial actually talking to us?" check.
            snap = dict(_dial._dial_heartbeat)
            if snap["last_seen_at"] is not None:
                snap["age_seconds"] = round(
                    time.time() - snap["last_seen_at"], 1,
                )
            else:
                snap["age_seconds"] = None
            self._send_json(snap)

        def _get_system_snapshot(self) -> None:
            # Snapshot for the /system dashboard. Current values +
            # 60-min ring buffers for the sparklines + build info +
            # home_assistant connection status.
            # Sampler may be None in tests / direct CLI invocation;
            # surface an empty history rather than 500.
            from .system_metrics import read_build_info
            from ..speaker_name import read_state as _read_speaker_name_state
            from ..voice.provider_state import read_active_provider

            try:
                ha_status = ha_status_cache.snapshot()
            except Exception:  # noqa: BLE001
                # Fail-soft per the existing aggregator convention —
                # never break /system/snapshot because HA is wedged.
                logger.exception("home assistant status snapshot failed")
                ha_status = {
                    "configured": False, "connected": False, "url": "",
                    "instance_name": None, "version": None,
                    "error": "probe failed",
                }

            try:
                if audio_health_sampler is not None:
                    airplay_health = audio_health_sampler.airplay_snapshot()
                else:
                    airplay_health = (
                        airplay_health_sampler.snapshot()
                        if airplay_health_sampler is not None else None
                    )
            except Exception:  # noqa: BLE001
                logger.exception("airplay health snapshot failed")
                airplay_health = {
                    "status": "unknown",
                    "reason": "AirPlay health sampler failed",
                }

            try:
                if audio_health_sampler is not None:
                    outputd_status = audio_health_sampler.outputd_snapshot()
                else:
                    outputd_status = asyncio.run(_outputd_status())
            except Exception:  # noqa: BLE001
                logger.exception("outputd status snapshot failed")
                outputd_status = None

            try:
                audio_health = (
                    audio_health_sampler.snapshot()
                    if audio_health_sampler is not None else None
                )
            except Exception:  # noqa: BLE001
                logger.exception("audio health snapshot failed")
                audio_health = None

            install_profile = _control_install_profile()
            payload: dict[str, Any] = {
                "build": read_build_info(),
                "metrics": (
                    sampler.snapshot() if sampler is not None else None
                ),
                "airplay_health": airplay_health,
                "audio_health": audio_health,
                "active_speaker_output_safety": (
                    _active_speaker_output_safety_snapshot(airplay_health)
                ),
                "outputd": outputd_status,
                "audio_quality": _safe_audio_quality_state(),
                "voice_provider": read_active_provider(),
                "speaker_name": _read_speaker_name_state().__dict__,
                "home_assistant": ha_status,
                "system_capabilities": system_capabilities_for_profile(
                    install_profile,
                ),
            }
            self._send_json(payload)

        # WS1 Phase 3b-2: jasper-doctor is a ROOT tool (audio/mixer/journal/
        # renderer probes + `sudo -u <renderer> aplay`), and jasper-control is
        # now non-root — running the doctor in-process here would make ~7
        # hardware checks fail on permissions (false red on the dashboard). So
        # the report is produced by the root jasper-doctor-json.service oneshot,
        # which jasper-control starts via its polkit manage-units grant (the
        # unit is in MANAGED_UNITS). The HTTP path serves the latest cached
        # report immediately and schedules stale/missing refreshes with
        # `systemctl --no-block`, so the dashboard never waits on a live run.
        def _get_system_diagnostics(self) -> None:
            result_path = _diagnostics_result_path()
            body, read_error = _read_diagnostics_snapshot(
                result_path,
                ttl_seconds=_diagnostics_cache_ttl_seconds(),
            )
            if body is None:
                refresh_started, refresh_error = _start_diagnostics_refresh(
                    result_path,
                )
                if refresh_started:
                    self._send_json(
                        _diagnostics_placeholder_result(
                            detail=(
                                "diagnostics snapshot not ready yet; "
                                "background refresh started"
                            ),
                        ),
                    )
                    return
                self._send_json(
                    _diagnostics_placeholder_result(
                        detail=(
                            f"diagnostics snapshot unavailable ({read_error}); "
                            f"{refresh_error}"
                        ),
                        status="fail",
                    ),
                )
                return

            if body.get("stale"):
                refresh_started, refresh_error = _start_diagnostics_refresh(
                    result_path,
                )
                body["refreshing"] = refresh_started
                if refresh_error:
                    _append_diagnostics_refresh_failure(body, refresh_error)
            else:
                body["refreshing"] = False
            self._send_json(body)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard_mutating_request():
                return
            if not self._guard_install_profile_route():
                return
            if not self._guard_control_token():
                return
            handler_name = self._POST_ROUTES.get(self.path)
            if handler_name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            getattr(self, handler_name)()

        def _guard_control_token(self) -> bool:
            """Opt-in token gate for the high-impact mutations.

            Runs AFTER the browser-origin/install-profile guards so an
            unknown path still 404s as before. Default-off: when no token
            file exists, control_token.verify() returns True and this is a
            pass-through. When the operator has enabled the gate
            (jasper-control-token --enable), a request to one of
            _TOKEN_GATED_ROUTES without a matching X-JTS-Token header is
            rejected 403 with an actionable JSON body and an audit log
            line. The token value is never logged.
            """
            if self.path not in _TOKEN_GATED_ROUTES:
                return True
            if control_token.verify(self.headers.get("X-JTS-Token")):
                return True
            # /grouping/set is the one DEVICE-TO-DEVICE gated route: a peer
            # fan-out (rooms_setup) or autonomous re-group (Phase D) presents the
            # household credential (X-JTS-Household), which each member verifies
            # against its own persisted copy — NOT the per-device CSRF token a
            # leader can't hold for a follower. Accept EITHER on this route only;
            # the other gated routes (poweroff/reboot/restart/mic-mute/firmware
            # update) are browser->own-speaker and stay control-token-only.
            # household_credential is fail-safe (absent => accept) so the first
            # bond, which DISTRIBUTES the secret over this very route, isn't
            # rejected by the gate it installs. See
            # docs/HANDOFF-control-plane-auth.md §6.
            if self.path == "/grouping/set" and household_credential.verify(
                self.headers.get("X-JTS-Household")
            ):
                return True
            log_event(
                logger,
                "control_token.denied",
                path=self.path,
                client=self.address_string(),
                level=logging.WARNING,
            )
            self._send_json(
                {
                    "error": "control_token_required",
                    "detail": "this control action requires X-JTS-Token; "
                    "enable/inspect with jasper-control-token; see "
                    "SECURITY.md",
                },
                status=403,
            )
            return False

        def _post_volume_adjust(self) -> None:
            if self._maybe_forward_pair_action_to_leader():
                return
            blocked = _active_speaker_volume_block()
            if blocked is not None:
                self._send_json(
                    {
                        "error": blocked.get("detail") or "speaker output is not ready",
                        "active_speaker_setup": blocked,
                    },
                    status=409,
                )
                return
            body = self._read_json()
            # Support both legacy delta_db (dial firmware compat,
            # interpreted on the 50 dB camilla scale) and the
            # cleaner delta_percent for newer clients.
            if "delta_percent" in body:
                try:
                    delta_pct = int(body["delta_percent"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "delta_percent must be an integer"},
                        status=400,
                    )
                    return
            elif "delta_db" in body:
                try:
                    delta_pct = _delta_db_to_delta_percent(
                        float(body["delta_db"]),
                    )
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "delta_db must be a number"},
                        status=400,
                    )
                    return
            else:
                self._send_json(
                    {"error": "missing delta_db or delta_percent"},
                    status=400,
                )
                return
            try:
                new_pct = asyncio.run(_adjust_op(delta_pct))
            except Exception as e:  # noqa: BLE001
                logger.exception("adjust volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            log_event(
                logger,
                "volume.adjust",
                delta_pct=delta_pct,
                new_pct=new_pct,
                client=self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))

        def _post_volume_set(self) -> None:
            if self._maybe_forward_pair_action_to_leader():
                return
            blocked = _active_speaker_volume_block()
            if blocked is not None:
                self._send_json(
                    {
                        "error": blocked.get("detail") or "speaker output is not ready",
                        "active_speaker_setup": blocked,
                    },
                    status=409,
                )
                return
            body = self._read_json()
            # Support both legacy `db` (dial / older clients) and
            # the cleaner `percent`. Percent is the canonical unit
            # for listening_level.
            if "percent" in body:
                try:
                    target_pct = int(body["percent"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "percent must be an integer"}, status=400,
                    )
                    return
            elif "db" in body:
                try:
                    target_pct = _db_to_percent(float(body["db"]))
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "db must be a number"}, status=400,
                    )
                    return
            else:
                self._send_json(
                    {"error": "missing db or percent"}, status=400,
                )
                return
            # Optional `source` field marks the caller as an
            # observed source-side change (e.g. host moved its
            # volume slider on the USB gadget). Route through
            # observe_source_volume so the coordinator's echo
            # window and source-active gate apply. Without
            # `source`, the caller is treated as authoritative
            # (dial twist, voice "louder", etc.).
            source_name = body.get("source")
            try:
                if source_name:
                    new_pct = asyncio.run(
                        _observe_op(str(source_name), target_pct),
                    )
                else:
                    new_pct = asyncio.run(_set_op(target_pct))
            except Exception as e:  # noqa: BLE001
                logger.exception("set volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            log_event(
                logger,
                "volume.set",
                new_pct=new_pct,
                source=source_name or "authoritative",
                client=self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))

        def _post_grouping_set(self) -> None:
            # Set this speaker's grouping role. /grouping/set is token-gated
            # (WS1 Phase 2, _TOKEN_GATED_ROUTES); the cross-device bond-forming
            # UI on speaker A configures speaker B by POSTing here on B's port,
            # authenticated by the household credential (Phase C,
            # docs/HANDOFF-control-plane-auth.md). The reconciler (kicked below)
            # is the single applier of the snapcast units + the outputd tap.
            body = self._read_json()
            enabled = bool(body.get("enabled"))
            role = str(body.get("role", "")).strip()
            channel = str(body.get("channel", "")).strip()
            bond_id = str(body.get("bond_id", "")).strip()
            leader_addr = str(body.get("leader_addr", "")).strip()
            optional_fields, parse_error = _parse_grouping_optional_fields(body)
            if parse_error is not None:
                self._send_json({"error": parse_error}, status=400)
                return
            assert optional_fields is not None
            trim_db = optional_fields.trim_db
            client_latency_ms = optional_fields.client_latency_ms
            left_delay_ms = optional_fields.left_delay_ms
            right_delay_ms = optional_fields.right_delay_ms
            crossover_hz = optional_fields.crossover_hz
            mains_highpass_enabled = optional_fields.mains_highpass_enabled
            subwoofer_present = optional_fields.subwoofer_present
            peer_addr: str | None = None
            if "peer_addr" in body:
                peer_addr = str(body.get("peer_addr") or "").strip()
            peer_name: str | None = None
            if "peer_name" in body:
                peer_name = str(body.get("peer_name") or "").strip()
            # Full bond roster (leader only): a list of {addr,name,channel}.
            # Build a BondMember tuple (for the shared validator) and the
            # serialized env string (for the writer). Omitted -> preserve;
            # an explicit [] serializes to "" which clears it (same contract
            # as peer_addr/peer_name).
            roster_members: tuple[BondMember, ...] = ()
            roster_str: str | None = None
            if "roster" in body:
                raw_roster = body.get("roster")
                if not isinstance(raw_roster, list):
                    self._send_json(
                        {"error": "roster must be a list"}, status=400,
                    )
                    return
                roster_members = tuple(
                    BondMember(
                        addr=str((m or {}).get("addr") or ""),
                        name=str((m or {}).get("name") or ""),
                        channel=str((m or {}).get("channel") or ""),
                    )
                    for m in raw_roster
                    if isinstance(m, dict)
                )
                roster_str = format_roster(roster_members)
                # Validate the roster whenever it is present — INCLUDING a
                # disabled request, which skips validate_grouping below. The
                # persisted roster is the _unbond disable list, so a member with
                # an injected foreign addr or a malformed channel must never land
                # on disk (it would become an unbond disable target / orphan).
                # The enabled path re-checks via validate_grouping (idempotent).
                roster_err = validate_roster(roster_members)
                if roster_err:
                    self._send_json({"error": roster_err}, status=400)
                    return
            # Validate an ENABLED request up front via the SHARED
            # validate_grouping (same rule the config loader applies on
            # read) so we never persist a fail-loud config. A disabled
            # request needs no fields.
            if enabled:
                effective_subwoofer_present = (
                    subwoofer_present
                    if subwoofer_present is not None
                    else False
                )
                effective_crossover_hz = _resolve_grouping_crossover_hz_for_write(
                    channel=channel,
                    subwoofer_present=effective_subwoofer_present,
                    requested=crossover_hz,
                )
                err = validate_grouping(
                    role=role, channel=channel,
                    bond_id=bond_id, leader_addr=leader_addr,
                    trim_db=trim_db if trim_db is not None else 0.0,
                    client_latency_ms=(
                        client_latency_ms
                        if client_latency_ms is not None
                        else 0
                    ),
                    left_delay_ms=left_delay_ms if left_delay_ms is not None else 0.0,
                    right_delay_ms=(
                        right_delay_ms if right_delay_ms is not None else 0.0
                    ),
                    crossover_hz=(
                        effective_crossover_hz
                        if effective_crossover_hz is not None
                        else DEFAULT_CROSSOVER_HZ
                    ),
                    mains_highpass_enabled=(
                        mains_highpass_enabled
                        if mains_highpass_enabled is not None
                        else DEFAULT_MAINS_HIGHPASS_ENABLED
                    ),
                    subwoofer_present=effective_subwoofer_present,
                    peer_addr=peer_addr or "",
                    peer_name=peer_name or "",
                    roster=roster_members,
                )
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                blocked = (
                    _active_speaker_grouping_block()
                    if body.get("enabled") else None
                )
                if blocked is not None:
                    self._send_json(
                        {
                            "error": (
                                blocked.get("detail")
                                or "active speaker setup is not ready for grouping"
                            ),
                            "active_speaker_setup": blocked,
                        },
                        status=409,
                    )
                    return
                crossover_hz = effective_crossover_hz
            before_grouping = load_grouping_config(GROUPING_ENV_FILE)
            live_apply_payload: dict[str, Any] | None = None
            reconciler_kicked = False
            try:
                _write_grouping(
                    enabled=enabled, role=role, channel=channel,
                    bond_id=bond_id, leader_addr=leader_addr,
                    trim_db=trim_db,
                    client_latency_ms=client_latency_ms,
                    left_delay_ms=left_delay_ms,
                    right_delay_ms=right_delay_ms,
                    crossover_hz=crossover_hz,
                    mains_highpass_enabled=mains_highpass_enabled,
                    subwoofer_present=subwoofer_present,
                    peer_addr=peer_addr, peer_name=peer_name,
                    roster=roster_str,
                )
                after_grouping = load_grouping_config(GROUPING_ENV_FILE)
                if (
                    enabled
                    and trim_db is not None
                    and before_grouping == after_grouping
                ):
                    live_apply_payload = {
                        "applied": True,
                        "mode": "noop",
                        "trim_db": round(float(after_grouping.trim_db), 1),
                    }
                elif (
                    trim_db is not None
                    and _is_trim_only_grouping_change(before_grouping, after_grouping)
                ):
                    live_apply = asyncio.run(
                        apply_live_grouping_trim(
                            after_grouping.trim_db,
                            cfg=after_grouping,
                        )
                    )
                    live_apply_payload = live_apply.to_dict()
                    if not live_apply.applied:
                        _kick_grouping_reconciler()
                        reconciler_kicked = True
                else:
                    _kick_grouping_reconciler()
                    reconciler_kicked = True
            except Exception as e:  # noqa: BLE001
                logger.exception("grouping set failed")
                self._send_json({"error": str(e)}, status=502)
                return
            # Persist / drop the household credential as the bond forms or
            # dissolves (control-plane-auth §6). A bond fan-out (enabled) carries
            # the leader's X-JTS-Household; an unpaired member adopts it
            # (trust-on-first-use over the trusted LAN) so every subsequent
            # cross-device /grouping/set verifies against it. An unbond
            # (disabled) clears it so the speaker can later re-pair. The leader
            # reads its secret ONCE before the unbond fan-out, so this clear
            # can't race the concurrent peer POSTs out of their credential. The
            # secret value is never logged — only the transition.
            if enabled:
                if household_credential.adopt(self.headers.get("X-JTS-Household")):
                    log_event(
                        logger, "household_credential.adopted",
                        bond=bond_id or "(none)",
                    )
            elif household_credential.is_paired():
                household_credential.clear()
                log_event(logger, "household_credential.cleared")
            log_event(
                logger,
                "grouping.set",
                enabled=enabled,
                role=role or "(none)",
                channel=channel or "(none)",
                bond=bond_id or "(none)",
                live_applied=(
                    None
                    if live_apply_payload is None
                    else live_apply_payload.get("applied")
                ),
                reconciler_kicked=reconciler_kicked,
                client=self.address_string(),
            )
            response = {
                "ok": True, "enabled": enabled, "role": role,
                "channel": channel, "bond_id": bond_id,
                "leader_addr": leader_addr,
                "reconciler_kicked": reconciler_kicked,
            }
            if live_apply_payload is not None:
                response["live_apply"] = live_apply_payload
            self._send_json(response)
            return

        def _post_volume_mute(self) -> None:
            if self._maybe_forward_pair_action_to_leader():
                return
            blocked = _active_speaker_volume_block()
            if blocked is not None:
                self._send_json(
                    {
                        "error": blocked.get("detail") or "speaker output is not ready",
                        "active_speaker_setup": blocked,
                    },
                    status=409,
                )
                return
            # Default is TOGGLE: muted → unmute (restore pre-mute
            # level), unmuted → mute. Used by HID accessory clicks
            # (jasper-input) and other one-shot toggle callers. An
            # optional explicit {"muted": true|false} body sets the
            # state idempotently — the shape voice's distinct
            # mute/unmute intents need (additive; absent = toggle).
            body = self._read_json()
            explicit = body.get("muted")
            if explicit is not None and not isinstance(explicit, bool):
                self._send_json(
                    {"error": "muted must be a boolean"}, status=400,
                )
                return
            try:
                if explicit is None:
                    new_pct = asyncio.run(_mute_toggle_op())
                else:
                    new_pct = asyncio.run(_mute_set_op(explicit))
            except Exception as e:  # noqa: BLE001
                logger.exception("mute failed")
                self._send_json({"error": str(e)}, status=502)
                return
            log_event(
                logger,
                "volume.mute",
                new_pct=new_pct,
                explicit=str(explicit),
                client=self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))
            return

        def _post_transport(self) -> None:
            # Bonded-follower: transport targets the PAIR. A dial paired
            # to the follower sends play/pause here; with the local
            # renderer stack parked (dumb-follower profile) the local
            # mux has nothing to toggle — the leader owns playback, so
            # the request forwards exactly like /volume*.
            if self._maybe_forward_pair_action_to_leader():
                return
            action = self.path.rsplit("/", 1)[1]  # toggle | next | previous
            try:
                result = asyncio.run(_dispatch_transport(action))
            except Exception as e:  # noqa: BLE001
                logger.exception("transport %s failed", action)
                self._send_json({"error": str(e)}, status=502)
                return
            log_event(
                logger,
                "transport.dispatch",
                action=action,
                client=self.address_string(),
            )
            if "error" in result:
                self._send_json(result, status=502)
                return
            self._send_json(result)
            return

        def _post_source_select(self) -> None:
            # POST /source/select body: {"source": "airplay"} or
            # {"source": "auto"}. The mux validates policy and
            # forwards the low-level lane choice to fan-in.
            if self._maybe_forward_pair_action_to_leader():
                return
            body = self._read_json()
            source = str(body.get("source") or "").strip().lower()
            if source == "auto":
                cmd = "AUTO"
            elif source in SOURCE_SELECT_IDS:
                cmd = f"SELECT {source}"
            else:
                choices = ", ".join(sorted(SOURCE_SELECT_IDS))
                self._send_json(
                    {
                        "error": (
                            f"source must be {choices}, or auto"
                        ),
                    },
                    status=400,
                )
                return
            try:
                result = asyncio.run(
                    _mux_socket_command(cmd, timeout=6.0),
                )
            except (
                FileNotFoundError,
                ConnectionRefusedError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                self._send_json(
                    {"error": f"jasper-mux unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("source select failed")
                self._send_json({"error": str(e)}, status=502)
                return
            log_event(
                logger,
                "source.select",
                source=source,
                client=self.address_string(),
            )
            self._send_json(_augment_source_payload(result))
            return

        def _post_session(self) -> None:
            cmd = "START" if self.path.endswith("start") else "END"
            if cmd == "START":
                payload = self._read_json()
                source = payload.get("source")
                if source is not None:
                    if (
                        not isinstance(source, str)
                        or not source.strip()
                        or any(ch.isspace() for ch in source)
                    ):
                        self._send_json(
                            {"error": "source must be a non-empty token"},
                            status=400,
                        )
                        return
                    try:
                        source.encode("ascii")
                    except UnicodeEncodeError:
                        self._send_json(
                            {"error": "source must be ASCII"},
                            status=400,
                        )
                        return
                    cmd = f"START {source.strip()}"
            result = self._voice_cmd_or_error(
                cmd,
                missing_error="voice_daemon not running (socket not found)",
                log_label=f"session {cmd}",
            )
            if result is None:
                return
            # Result codes from voice_daemon's manual_session_*:
            #   OK / BUSY / CAP / PAUSED / MUTED / MEASURING /
            #   NO_SESSION / ALREADY_ENDED / UNKNOWN_SOURCE / ERROR
            # Map non-OK outcomes to non-2xx so the dial's HTTP
            # error path can show the right LED color.
            http_status = 200
            if result.get("result") not in ("OK", "ALREADY_ENDED", None):
                if result.get("result") in ("CAP", "PAUSED", "MUTED", "MEASURING"):
                    http_status = 503
                elif result.get("result") in ("BUSY", "NO_SESSION"):
                    http_status = 409
                elif result.get("result") == "UNKNOWN_SOURCE":
                    http_status = 400
                else:
                    http_status = 502
            self._send_json(result, status=http_status)
            return

        def _post_cue_play(self) -> None:
            # POST /cue/play  body: {"slug": "<cue_slug>"}
            # Routes the request through voice_daemon's control
            # socket so the cue plays through the daemon's
            # already-correctly-gained TtsPlayout. A separate
            # standalone client (e.g., `jasper-cues play <slug>`)
            # would have to recreate the daemon's volume math
            # to match levels, and got it wrong (~20 dB too
            # loud). Centralising here keeps levels consistent.
            body = self._read_json()
            slug = (body.get("slug") or "").strip()
            if not slug:
                self._send_json(
                    {"error": "missing 'slug' in body"}, status=400,
                )
                return
            # Cues run ~5-6s of audio plus duck/restore plus drain.
            # 30s gives generous headroom even for the longest reasonable cue.
            result = self._voice_cmd_or_error(
                f"CUE_PLAY {slug}",
                timeout=30.0,
                missing_error="voice_daemon not running",
                log_label="cue play",
            )
            if result is None:
                return
            http_status = 200
            if result.get("result") == "missing_slug":
                http_status = 400
            elif result.get("result") == "unknown_slug":
                http_status = 404
            elif result.get("result") == "cues_not_configured":
                http_status = 503
            elif result.get("result") == "busy":
                http_status = 409
            elif result.get("result") != "ok":
                http_status = 502
            self._send_json(result, status=http_status)
            return

        def _post_mic_mute(self) -> None:
            # POST /mic/mute  body: {"muted": bool}
            # Idempotent set. Forwards MUTE or UNMUTE to the voice
            # daemon's control socket, which drops mic frames at
            # the wake-loop gate (mute) or resumes (unmute) and
            # plays a short click on either edge for feedback.
            leader = _pair_follower_leader_addr()
            if leader:
                payload = _bonded_follower_mic_payload(leader)
                self._send_json({**payload, "error": payload["message"]},
                                status=409)
                return
            body = self._read_json()
            if "muted" not in body:
                self._send_json(
                    {"error": "missing 'muted' in body"}, status=400,
                )
                return
            cmd = "MUTE" if bool(body["muted"]) else "UNMUTE"
            result = self._voice_cmd_or_error(
                cmd,
                timeout=3.0,
                missing_error="voice_daemon not running",
                log_label=f"mic {cmd}",
            )
            if result is None:
                return
            log_event(
                logger,
                "mic.set",
                muted=bool(body["muted"]),
                client=self.address_string(),
            )
            # Read back the truth from the daemon. STATUS is cheap
            # and the daemon's flag is authoritative.
            try:
                st = asyncio.run(_voice_socket_command(
                    voice_socket_path, "STATUS", timeout=2.0,
                ))
                muted_now = bool(st.get("mic_muted", False))
            except Exception:  # noqa: BLE001
                # If readback fails, trust the set and move on.
                muted_now = bool(body["muted"])
            self._send_json({"muted": muted_now, "result": result.get("result")})
            return

        def _post_aec_toggle(self) -> None:
            # Flip the software-AEC3/direct-mic mode between auto and disabled,
            # then kick the reconciler. Chip-AEC profiles bypass WebRTC AEC3
            # but still need jasper-aec-bridge.service as the chip-beam UDP
            # carrier, so attempts to disable "software AEC3" while chip-AEC is
            # active are rejected below. Called by the /wake/ page's software
            # AEC3 layer toggle (after a current-state read for idempotent
            # set-state semantics). Non-blocking — the wizard polls /aec to
            # see when the transition lands (~10-15 s). The kick uses systemctl
            # restart so rapid toggles cannot be swallowed while the oneshot
            # reconciler is already active.
            #
            # Risk model: LAN-local + browser-origin guard, same
            # as /system/restart/*. This is still not auth; it is
            # the small boundary that blocks cross-site browser
            # POSTs and DNS-rebinding Host headers while keeping
            # curl, local proxies, and accessories working.
            current = _read_aec_mode()
            new_mode = "disabled" if current == "auto" else "auto"
            if new_mode == "disabled":
                status = _aec_full_status()
                if status.get("software_aec3", {}).get("bypassed"):
                    self._send_json(
                        {
                            "error": (
                                "Software AEC3 is already bypassed by the "
                                "chip-AEC profile. Choose Direct mic to stop "
                                "the chip-AEC bridge carrier, or choose XVF "
                                "software AEC3 to use WebRTC AEC3."
                            ),
                            "mode": current,
                            "bridge_role": status.get("bridge_role"),
                        },
                        status=409,
                    )
                    return
            try:
                _write_aec_mode(new_mode)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "aec.toggle",
                **{
                    "from": current,
                    "to": new_mode,
                    "client": self.address_string(),
                },
            )
            self._send_json({
                "mode": new_mode,
                "bridge_active": _aec_bridge_active(),
            })
            return

        def _post_aec_leg(self) -> None:
            # Toggle one of the additive wake-detection legs
            # (raw chip-direct, DTLN neural, or chip-AEC beams). The
            # reconciler maps the boolean back to the underlying env vars
            # the bridge + voice each read at startup, then
            # restarts whichever daemons need to pick up the
            # change. Per-leg sub-toggles are only meaningful
            # when JASPER_AEC_MODE=auto and the bridge is up;
            # the reconciler clears the underlying vars when
            # AEC is disabled so a stale leg config doesn't
            # leave voice listening on a port nobody talks to.
            #
            # Risk model: LAN-local + browser-origin guard, same
            # as /aec/toggle.
            body = self._read_json()
            leg = body.get("leg")
            enabled_val = body.get("enabled")
            if leg not in _aec_endpoints._TOGGLE_TO_TOKEN:
                self._send_json(
                    {"error": "leg must be one of: "
                              + ", ".join(
                                  sorted(_aec_endpoints._TOGGLE_TO_TOKEN)
                              )},
                    status=400,
                )
                return
            if not isinstance(enabled_val, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"}, status=400,
                )
                return
            try:
                _write_aec_leg(leg, enabled_val)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "aec.leg",
                leg=leg,
                enabled=enabled_val,
                client=self.address_string(),
            )
            self._send_json(_aec_full_status())
            return

        def _post_aec_profile(self) -> None:
            # Set the canonical mic/AEC input profile. This is the
            # preferred surface for households and onboarding: it writes
            # one high-level choice plus rollback-safe legacy leg keys.
            # The older /aec/toggle and /aec/leg routes remain as custom
            # expert controls and stamp JASPER_AUDIO_INPUT_PROFILE=custom.
            body = self._read_json()
            profile = body.get("profile")
            if not isinstance(profile, str):
                self._send_json(
                    {"error": "profile must be a string"}, status=400,
                )
                return
            try:
                _write_audio_input_profile(profile)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=400 if isinstance(e, ValueError) else 502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "aec.profile",
                profile=normalize_audio_input_profile(profile, default=""),
                client=self.address_string(),
            )
            self._send_json(_aec_full_status())
            return

        def _post_aec_threshold(self) -> None:
            # Sensitivity slider on the /wake/ page. Writes
            # JASPER_WAKE_THRESHOLD into wake_model.env (same
            # file the /wake/ form save writes the model into)
            # and restarts jasper-voice — the openWakeWord
            # detector reads the threshold at startup, so a hot
            # config change without a restart wouldn't take
            # effect on the next wake.
            #
            # AEC-mode and leg toggles share the reconciler
            # which already restarts voice; threshold-only
            # changes bypass the reconciler since they don't
            # need the bridge to restart. Non-blocking — the
            # slider's "Applying…" state is just UX.
            body = self._read_json()
            try:
                threshold = float(body.get("threshold"))
            except (TypeError, ValueError):
                self._send_json(
                    {"error": "threshold must be a number"}, status=400,
                )
                return
            if not 0.0 <= threshold <= 1.0:
                self._send_json(
                    {"error": "threshold must be between 0 and 1"},
                    status=400,
                )
                return
            try:
                _write_wake_threshold(threshold)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write wake_model.env failed: {e}"},
                    status=502,
                )
                return
            try:
                subprocess.Popen(
                    ["systemctl", "restart", "--no-block",
                     "jasper-voice.service"],
                )
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"voice restart failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "wake.threshold",
                value=f"{threshold:.2f}",
                client=self.address_string(),
            )
            self._send_json({"threshold": threshold})
            return

        def _post_aec_firmware_update(self) -> None:
            status = _aec_full_status()
            firmware = status.get("firmware_update")
            action = firmware.get("action") if isinstance(firmware, dict) else {}
            if not isinstance(action, dict) or not action.get("enabled"):
                detail = (
                    firmware.get("detail")
                    if isinstance(firmware, dict) else
                    "microphone firmware update is not available"
                )
                self._send_json({"error": detail}, status=409)
                return
            try:
                _start_xvf_firmware_update()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"firmware update start failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "aec.firmware_update.start",
                client=self.address_string(),
                target=(firmware.get("target") or {}).get("id")
                if isinstance(firmware, dict) else "",
            )
            self._send_json(_aec_full_status())
            return

        def _post_debug(self) -> None:
            # /system Debug card: raise one subsystem to DEBUG
            # logging. Additive-only + auto-expiring (jasper/
            # debug_mode.py). Daemon subsystems restart to apply;
            # control applies in-process. Non-blocking — the card's
            # "Applying…" state is just UX.
            body = self._read_json()
            subsystem = str(body.get("subsystem") or "")
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"}, status=400,
                )
                return
            try:
                debug_control.set_debug(subsystem, enabled)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"debug toggle failed: {e}"}, status=502,
                )
                return
            log_event(
                logger,
                "debug.toggle",
                subsystem=subsystem,
                enabled=enabled,
                client=self.address_string(),
            )
            self._send_json(debug_control.snapshot())
            return

        def _post_system_audio_quality(self) -> None:
            body = self._read_json()
            if not isinstance(body, dict):
                self._send_json(
                    {"error": "invalid request body: expected JSON object"},
                    status=400,
                )
                return
            raw_converter = body.get("converter")
            if not isinstance(raw_converter, str) or not raw_converter.strip():
                self._send_json(
                    {"error": "converter is required"},
                    status=400,
                )
                return
            try:
                converter = _normalize_audio_converter(raw_converter)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            try:
                state = _apply_audio_quality(converter)
            except (OSError, subprocess.SubprocessError) as e:
                logger.exception("audio quality apply failed")
                self._send_json(
                    {"error": f"audio quality apply failed: {e}"},
                    status=502,
                )
                return
            try:
                # Refresh active renderers without resurrecting sources the
                # household explicitly disabled in /sources/.
                subprocess.Popen(
                    [
                        "systemctl",
                        "try-restart",
                        *LOCAL_SOURCE_AUDIO_REFRESH_UNITS,
                    ],
                )
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"renderer restart failed: {e}"},
                    status=502,
                )
                return
            log_event(
                logger,
                "audio_quality.set",
                converter=converter,
                client=self.address_string(),
            )
            self._send_json({
                "ok": True,
                "action": "audio-quality",
                "try_restart_units": LOCAL_SOURCE_AUDIO_REFRESH_UNITS,
                "audio_quality": state,
            })
            return

        def _post_system_action(self) -> None:
            # Action endpoints for the /system dashboard. All
            # shell out to systemctl; jasper-control already runs
            # as root so no sudo needed. Returns immediately —
            # the restart is async on systemd's side and the
            # dashboard polls /system/snapshot to know when
            # things are back up.
            #
            # Risk model: LAN-local + browser-origin guard
            # (consistent with the wizards). Anyone already on the
            # trusted WiFi can trigger these; the dashboard's
            # confirm dialogs are UX, not security.
            parked = _pair_follower_leader_addr() is not None
            restart_units: list[str] = []
            try_restart_units: list[str] = []
            if self.path == "/system/restart/voice":
                if parked:
                    # The dumb-follower profile keeps voice disabled
                    # while paired — a dashboard restart would boot
                    # 240 MB of models that jasper-aec-reconcile
                    # re-parks. Refuse with the story, never silently.
                    self._send_json(
                        {"error": "voice is parked while this speaker "
                                  "is in a stereo pair — the assistant "
                                  "runs on the pair leader"},
                        status=409,
                    )
                    return
                units = ["jasper-voice.service"]
                restart_units = units
                action = "restart-voice"
            elif self.path == "/system/restart/audio":
                restart_units = list(CORE_AUDIO_RESTART_UNITS)
                try_restart_units = list(LOCAL_SOURCE_AUDIO_REFRESH_UNITS)
                if parked:
                    # Restart only the units the follower profile keeps
                    # alive — derived from the local-source lifecycle
                    # registry so it cannot drift from follower parking.
                    parked_units = set(local_source_park_units())
                    try_restart_units = [
                        u for u in try_restart_units if u not in parked_units
                    ]
                units = restart_units + try_restart_units
                action = "restart-audio"
            elif self.path == "/system/reboot":
                units = []  # systemctl reboot — no units
                action = "reboot"
            else:
                # poweroff is reboot's terminal sibling: the speaker
                # stays off until someone physically re-plugs power.
                # The "graceful" part matters more than usual here —
                # this endpoint exists *specifically* to give the
                # household a non-power-yank way to shut down before
                # hardware changes, after 2026-05-23's dirty-shutdown
                # incident wiped the NetworkManager keyfile.
                units = []  # systemctl poweroff — no units
                action = "poweroff"
            # Audit BEFORE the action: reboot/poweroff take the system down, so
            # a log-after might never flush. This is the line that
            # distinguishes a dashboard-triggered restart/reboot/poweroff from a
            # watchdog or crash reset when debugging "the speaker restarted on
            # its own" (see AGENTS.md). No secrets — action + units + requester.
            log_event(
                logger,
                "system.action",
                action=action,
                units=",".join(units) or "-",
                client=self.address_string(),
            )
            try:
                if action == "reboot":
                    subprocess.Popen(["systemctl", "reboot"])
                elif action == "poweroff":
                    subprocess.Popen(["systemctl", "poweroff"])
                else:
                    # Use start-after-stop semantics for core services. Local
                    # source daemons use try-restart so dashboard audio restart
                    # never turns on a source the household disabled in
                    # /sources/ (USB would otherwise re-advertise its gadget).
                    if restart_units:
                        subprocess.Popen(["systemctl", "restart", *restart_units])
                    if try_restart_units:
                        subprocess.Popen([
                            "systemctl",
                            "try-restart",
                            *try_restart_units,
                        ])
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"systemctl invocation failed: {e}"},
                    status=502,
                )
                return
            self._send_json({
                "ok": True,
                "action": action,
                "units": units,
                "restart_units": restart_units,
                "try_restart_units": try_restart_units,
            })
            return

        # --- route tables (path -> handler-method name) ---
        # Keyed by exact path. Method dispatch (do_GET vs do_POST)
        # disambiguates the two '/debug' handlers; tuple routes map
        # each member path to the one method that re-discriminates
        # self.path internally (transport action, system action).
        # The string keys keep the route literals greppable for the
        # client/server contract test (tests/test_control_client.py).
        _GET_ROUTES = {
            "/healthz": "_get_healthz",
            "/volume": "_get_volume",
            "/mic": "_get_mic",
            "/source/state": "_get_source_state",
            "/aec": "_get_aec",
            "/debug": "_get_debug",
            "/state": "_get_state",
            "/grouping": "_get_grouping",
            "/dial/status": "_get_dial_status",
            "/system/snapshot": "_get_system_snapshot",
            "/system/diagnostics": "_get_system_diagnostics",
        }
        _POST_ROUTES = {
            "/volume/adjust": "_post_volume_adjust",
            "/volume/set": "_post_volume_set",
            "/grouping/set": "_post_grouping_set",
            "/volume/mute": "_post_volume_mute",
            "/transport/toggle": "_post_transport",
            "/transport/next": "_post_transport",
            "/transport/previous": "_post_transport",
            "/source/select": "_post_source_select",
            "/session/start": "_post_session",
            "/session/end": "_post_session",
            "/cue/play": "_post_cue_play",
            "/mic/mute": "_post_mic_mute",
            "/aec/toggle": "_post_aec_toggle",
            "/aec/leg": "_post_aec_leg",
            "/aec/profile": "_post_aec_profile",
            "/aec/threshold": "_post_aec_threshold",
            "/aec/firmware/update": "_post_aec_firmware_update",
            "/debug": "_post_debug",
            "/system/audio-quality": "_post_system_audio_quality",
            "/system/restart/voice": "_post_system_action",
            "/system/restart/audio": "_post_system_action",
            "/system/reboot": "_post_system_action",
            "/system/poweroff": "_post_system_action",
        }

    return Handler


class ControlHTTPServer(ThreadingHTTPServer):
    """Bounded ThreadingHTTPServer whose accept loop drives the watchdog.

    `service_actions()` runs on every `serve_forever()` poll iteration
    (~0.5 s cadence) **in the accept-loop thread itself**, so bumping the
    heartbeat here ties `WATCHDOG=1` to the loop actually spinning: if
    the accept loop wedges (blocked selector, interpreter deadlock), the
    bumps stop, `jasper.watchdog.Heartbeat`'s progress sentinel goes
    stale, pats stop, and systemd's `WatchdogSec=` revives us with a
    fresh process. Request handlers run on worker threads and
    intentionally don't gate the heartbeat — a slow probe must not look
    like a dead daemon. Same Tier 1 mechanism as jasper-voice
    (Type=notify + sentinel-guarded `WATCHDOG=1`).

    `heartbeat` stays None in tests/dev so the server runs standalone.
    """

    daemon_threads = True
    heartbeat: Any = None
    request_queue_size = CONTROL_REQUEST_QUEUE_SIZE

    def __init__(
        self,
        *args: Any,
        max_workers: int = CONTROL_MAX_WORKERS,
        request_timeout_sec: float = CONTROL_REQUEST_TIMEOUT_SEC,
        overload_log_interval_sec: float = CONTROL_OVERLOAD_LOG_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._request_timeout_sec = float(request_timeout_sec)
        self._overload_log_interval_sec = max(0.0, float(overload_log_interval_sec))
        self._clock = clock
        self._overload_log_lock = threading.Lock()
        self._overload_next_log_at = 0.0
        self._overload_suppressed = 0
        self._admission = threading.BoundedSemaphore(self._max_workers)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="jasper-control-http",
        )
        try:
            super().__init__(*args, **kwargs)
        except OSError:
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise

    def service_actions(self) -> None:
        super().service_actions()
        hb = self.heartbeat
        if hb is not None:
            hb.bump()

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(self._request_timeout_sec)
        except OSError:
            pass
        if not self._admission.acquire(blocking=False):
            self._send_overloaded(request, client_address)
            return
        try:
            self._executor.submit(self._handle_in_pool, request, client_address)
        except RuntimeError:
            self._admission.release()
            self.shutdown_request(request)
            raise

    def _handle_in_pool(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._admission.release()

    def _send_overloaded(self, request: Any, client_address: Any) -> None:
        payload = {
            "error": "server_overloaded",
            "retry_after": 1,
        }
        body = json.dumps(payload).encode("utf-8")
        response = (
            b"HTTP/1.1 429 Too Many Requests\r\n"
            b"Content-Type: application/json\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n"
            b"Retry-After: 1\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"\r\n"
            + body
        )
        try:
            request.sendall(response)
        except OSError:
            pass
        finally:
            self._log_overloaded(client_address)
            self.shutdown_request(request)

    def _log_overloaded(self, client_address: Any) -> None:
        now = self._clock()
        with self._overload_log_lock:
            if now < self._overload_next_log_at:
                self._overload_suppressed += 1
                return
            suppressed = self._overload_suppressed
            self._overload_suppressed = 0
            self._overload_next_log_at = now + self._overload_log_interval_sec
        log_event(
            logger,
            "control.overloaded",
            client=repr(client_address),
            max_workers=self._max_workers,
            suppressed=suppressed,
            level=logging.WARNING,
        )

    def server_close(self) -> None:
        super().server_close()
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False, cancel_futures=True)


def build_server(
    host: str,
    port: int,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str = "/run/jasper/voice.sock",
    sampler: Any = None,
    airplay_health_sampler: Any = None,
    audio_health_sampler: Any = None,
) -> ControlHTTPServer:
    return ControlHTTPServer(
        (host, port),
        _make_handler(
            camilla_host,
            camilla_port,
            voice_socket_path,
            sampler,
            airplay_health_sampler,
            audio_health_sampler,
        ),
    )



def _install_sigterm_shutdown(server: ThreadingHTTPServer) -> Callable[[], None]:
    previous = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(signum: int, _frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        log_event(logger, "control.shutdown", signal=sig_name)
        threading.Thread(
            target=server.shutdown,
            name="control-sigterm-shutdown",
            daemon=True,
        ).start()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    def _restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return _restore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-control",
        description="HTTP control surface for the JTS speaker (dial, automation, etc.)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_CONTROL_HOST", "0.0.0.0"),
        help="bind host (default 0.0.0.0 — LAN-reachable)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CONTROL_PORT", "8780")),
    )
    parser.add_argument(
        "--camilla-host",
        default=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--camilla-port", type=int,
        default=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )
    parser.add_argument(
        "--dial-log-host",
        default=os.environ.get("JASPER_DIAL_LOG_HOST", "0.0.0.0"),
        help="bind host for the dial UDP log listener",
    )
    parser.add_argument(
        "--dial-log-port", type=int,
        default=int(os.environ.get("JASPER_DIAL_LOG_PORT", "5514")),
        help="UDP port for dial log datagrams (default 5514)",
    )
    parser.add_argument(
        "--voice-socket",
        default=os.environ.get(
            "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
        ),
        help="path to voice_daemon's control UDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Log flight recorder + runtime debug toggle (/system Debug card).
    # install() holds the jasper logger at DEBUG for the in-RAM ring,
    # keeps the journal at INFO, applies the debug toggle, and wires
    # SIGUSR1 -> dump. See jasper/flight_recorder.py.
    from .. import flight_recorder
    flight_recorder.install("control")

    # System metrics sampler — 5 s ring buffer for the /system dashboard.
    # Daemon thread, exits with the process.
    from .system_metrics import SystemSampler
    sampler = SystemSampler()
    sampler.start()
    # One audio-health sampler — composes the existing AirPlay probes with
    # cheap outputd state + slow route certification reads. It is the only
    # resident audio-monitor thread.
    from .audio_health import AudioHealthSampler
    audio_health_sampler = AudioHealthSampler(
        camilla_host=args.camilla_host,
        camilla_port=args.camilla_port,
        service_probe=sampler.service_states_snapshot,
    )
    audio_health_sampler.start()

    server = build_server(
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
        sampler=sampler,
        audio_health_sampler=audio_health_sampler,
    )
    # WS1 Phase 2: arm the control-token gate before serving. ensure_token()
    # auto-generates the token (0640 group jasper) if absent, so the destructive
    # routes are always gated with no operator action; canonical_page
    # auto-delivers it to the dashboard (invisible to the household). Idempotent — never rotates an
    # existing token. Failure is non-fatal (the gate fail-safes to off) so a
    # transient write error can't keep the recovery surface from starting.
    try:
        control_token.ensure_token()
    except OSError as exc:
        log_event(logger, "control_token.ensure_failed", error=str(exc),
                  level=logging.WARNING)
    # WS1 Phase 3: the privileged restart broker. jasper-control is the single
    # mediated systemctl boundary — jasper-web's wizard restarts, jasper-mux's
    # librespot recovery, and the room-correction renderer pause ask it to run
    # an allowlisted, closed-vocabulary restart over a SO_PEERCRED'd UNIX socket
    # so those daemons need no privilege of their own once dropped to non-root
    # service users. Bind failure is non-fatal (logged): the wizards fall back
    # to their existing fail-soft "restart didn't happen, logged" behaviour.
    restart_broker_server = restart_broker.start_broker()
    run_dial_log_listener(args.dial_log_host, args.dial_log_port)
    # Multi-device peering daemon. No-op (no thread, no asyncio loop,
    # no zeroconf import) when /var/lib/jasper/peering.env has
    # JASPER_PEERING=off — the default. The user enables it via the
    # /rooms/ Speakers page, which writes the env file and restarts
    # jasper-control to pick up the new mode.
    start_peering_daemon_if_enabled()
    # Tier 3 resilience: protocol-level liveness probe for shairport-sync
    # so a wedged AP2 control plane recovers without manual intervention.
    # docs/HANDOFF-resilience.md (Tier 3). Off via
    # JASPER_SHAIRPORT_SUPERVISOR=disabled in /etc/jasper/jasper.env.
    shairport_supervisor.start_supervisor()
    # T5.2 — userspace-liveness supervisor closing the gap exposed
    # by the 2026-05-23 incident (PID 1 alive enough to pat the
    # kernel watchdog but sshd / userspace effectively dead). Probes
    # sshd banner + our own HTTP /healthz + /proc/loadavg; clean
    # `systemctl reboot` after 3 consecutive failures, rate-limited
    # to 1 reboot per 24 hours. docs/HANDOFF-tier5-watchdog-liveness.md.
    # Off via JASPER_SYSTEM_SUPERVISOR=disabled.
    system_supervisor.start_supervisor()
    # Bonded-member runtime liveness: closes the gap between grouping
    # reconciles — sustained dac_content starvation kicks the
    # reconciler (rate-limited), and the leader's snapcast group→stream
    # bindings are read-repaired every poll (the 2026-06-11 silent-bond
    # class). Costs one grouping.env read per 30 s when solo. Off via
    # JASPER_GROUPING_SUPERVISOR=disabled.
    grouping_supervisor.start_supervisor()
    # After-the-fact multiroom cascade timeline: scans existing structured
    # journal events into a small /state ring so restart chains are
    # reconstructable without fetching raw logs first. Solo-gated (skips the
    # journalctl scan when no bond is configured) and off via
    # JASPER_MULTIROOM_CASCADE_TIMELINE=disabled.
    from ..multiroom import cascade_timeline
    cascade_timeline.start_sampler()
    # Runtime debug toggle: clear an expired session left on disk, or
    # re-arm the auto-quiet timer if a debug session is still active
    # across this control restart. See jasper/control/debug_control.py.
    debug_control.reconcile_on_startup()
    logger.info(
        "jasper-control listening on http://%s:%d "
        "(camilla=%s:%d, dial-log=%s:%d/udp, voice=%s)",
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.dial_log_host, args.dial_log_port,
        args.voice_socket,
    )
    # Tier 1 — systemd watchdog (Type=notify + WatchdogSec in the unit).
    # READY=1 goes out here; serve_forever()'s poll loop bumps the
    # progress sentinel via ControlHTTPServer.service_actions, so a
    # wedged accept loop stops the WATCHDOG=1 pats and systemd restarts
    # us. No-ops outside systemd (NOTIFY_SOCKET unset). Same Heartbeat
    # helper jasper-voice uses (jasper/watchdog.py).
    from ..watchdog import Heartbeat
    heartbeat = Heartbeat()
    server.heartbeat = heartbeat
    heartbeat.start()
    restore_sigterm = _install_sigterm_shutdown(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        restore_sigterm()
        stop_peering_daemon()
        # Stop the restart broker's accept loop + close its socket, like the
        # HTTP server / peering / heartbeat above — so SIGTERM tears down every
        # background server explicitly rather than leaving the broker's daemon
        # thread to die with the process. No-op if the broker failed to bind.
        if restart_broker_server is not None:
            restart_broker_server.shutdown()
            restart_broker_server.server_close()
        heartbeat.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
