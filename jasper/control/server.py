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
import os
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from ..multiroom.config import GROUPING_ENV_FILE, validate_grouping
from ..multiroom.state import grouping_response, read_grouping_state
from ..music_sources import MUSIC_SOURCE_SPECS
from ..transit.state import read_state as read_transit_state
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
AUDIO_QUALITY_RENDERER_UNITS = [
    "shairport-sync.service",
    "librespot.service",
    "bluealsa-aplay.service",
    "jasper-usbsink.service",
]
ACTIVE_SPEAKER_STAGED_STARTUP_BASENAME = "active_speaker_staged_startup.yml"
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


def _active_speaker_output_safety_snapshot(
    airplay_health: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the landing-page speaker-output safety state.

    `/system/snapshot` already carries the CamillaDSP config path via the
    AirPlay health sampler. Keep the browser dumb: classify that path here and
    let the page render a boolean instead of knowing commissioning filenames.
    """

    current = airplay_health.get("current") if isinstance(airplay_health, dict) else {}
    camilla = current.get("camilla") if isinstance(current, dict) else {}
    raw_path = camilla.get("config_path") if isinstance(camilla, dict) else None
    config_path = str(raw_path or "")
    safety_muted = (
        os.path.basename(config_path) == ACTIVE_SPEAKER_STAGED_STARTUP_BASENAME
    )
    return {
        "safety_muted": safety_muted,
        "reason": "active_speaker_staged_startup" if safety_muted else None,
        "active_config_path": config_path or None,
        "source": "airplay_health.camilla_config_path",
    }

# The high-impact mutations the control token gates (SECURITY.md).
# The primitive remains fail-safe-open when no /var/lib/jasper/control_token file
# exists, but jasper-control ensures one at startup so production installs are
# gated automatically.
# Deliberately NOT including /volume*, /transport*, /source* — the dial's
# bread-and-butter low-impact controls stay open (the dial never calls
# these). poweroff/reboot = power loop; mic/mute = defeats the privacy-mic
# promise; grouping/set = hijacks output routing; restart/voice|audio =
# disrupt playback + the assistant. WS1 Phase 2 added the two restart routes
# and made the gate mandatory (control_token.ensure_token() at startup, below).
_TOKEN_GATED_ROUTES = frozenset({
    "/system/poweroff",
    "/system/reboot",
    "/system/restart/voice",
    "/system/restart/audio",
    "/mic/mute",
    "/grouping/set",
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
DIAGNOSTICS_RESPONSE_CACHE_TTL_SEC = 10.0


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

        try:
            value = compute()
        except Exception:
            with self._cond:
                self._inflight = False
                self._cond.notify_all()
            raise

        with self._cond:
            self._value = value
            self._expires_at = self._clock() + self._ttl_sec
            self._inflight = False
            self._cond.notify_all()
            return value


class _NonBlockingTTLCache:
    """Cache with a one-caller refresh lane for long read-only routes."""

    def __init__(
        self,
        ttl_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_sec = float(ttl_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Any = _MISSING
        self._expires_at = 0.0
        self._inflight = False

    def get_fresh(self) -> Any:
        with self._lock:
            if self._value is not _MISSING and self._clock() < self._expires_at:
                return self._value
            return _MISSING

    def try_begin_refresh(self) -> bool:
        with self._lock:
            if self._inflight:
                return False
            self._inflight = True
            return True

    def finish_refresh(self, value: Any = _MISSING, *, cache: bool = False) -> None:
        with self._lock:
            if cache:
                self._value = value
                self._expires_at = self._clock() + self._ttl_sec
            self._inflight = False

VOLUME_MIN_DB = _volume_ops.VOLUME_MIN_DB
VOLUME_MAX_DB = _volume_ops.VOLUME_MAX_DB
SPOTIFY_OAUTH_CALLBACK_BASE = _volume_ops.SPOTIFY_OAUTH_CALLBACK_BASE
DIAL_HEARTBEAT_PATH = _dial.DIAL_HEARTBEAT_PATH
_dial_heartbeat = _dial._dial_heartbeat
SOURCE_AVAILABILITY_TTL_SEC = _state_aggregate.SOURCE_AVAILABILITY_TTL_SEC
_source_availability_cache = _state_aggregate._source_availability_cache
_source_availability_lock = _state_aggregate._source_availability_lock
OUTPUTD_BASE_CAMILLA_CONFIG = _state_aggregate.OUTPUTD_BASE_CAMILLA_CONFIG
_AEC_MODE_FILE = _aec_endpoints._AEC_MODE_FILE
_WAKE_MODEL_FILE = _aec_endpoints._WAKE_MODEL_FILE
_JASPER_ENV_FILE = _aec_endpoints._JASPER_ENV_FILE
_TOGGLE_TO_TOKEN = _aec_endpoints._TOGGLE_TO_TOKEN

_same_config_path = _state_aggregate._same_config_path
_sound_apply_target = _state_aggregate._sound_apply_target
_sound_runtime_status = _state_aggregate._sound_runtime_status
_outputd_status = _state_aggregate._outputd_status
_read_audio_quality_state = _state_aggregate._read_audio_quality_state
_read_active_audio_converter = _state_aggregate._read_active_audio_converter
_clamp_db = _volume_ops._clamp_db
_db_to_percent = _volume_ops._db_to_percent
_percent_to_db = _volume_ops._percent_to_db
_delta_db_to_delta_percent = _volume_ops._delta_db_to_delta_percent
_spotify_redirect_uri = _volume_ops._spotify_redirect_uri
_audio_validation_summary = _aec_endpoints._audio_validation_summary


def _safe_audio_quality_state() -> dict[str, Any]:
    previous_state = _state_aggregate._read_audio_quality_state
    previous_active = _state_aggregate._read_active_audio_converter
    _state_aggregate._read_audio_quality_state = _read_audio_quality_state
    _state_aggregate._read_active_audio_converter = _read_active_audio_converter
    try:
        return _state_aggregate._safe_audio_quality_state()
    finally:
        _state_aggregate._read_audio_quality_state = previous_state
        _state_aggregate._read_active_audio_converter = previous_active


def _sync_aec_module() -> None:
    _aec_endpoints._AEC_MODE_FILE = _AEC_MODE_FILE
    _aec_endpoints._WAKE_MODEL_FILE = _WAKE_MODEL_FILE
    _aec_endpoints._JASPER_ENV_FILE = _JASPER_ENV_FILE


def _parse_env_bool(raw: str, default: bool) -> bool:
    return _aec_endpoints._parse_env_bool(raw, default)


def _read_aec_state() -> dict:
    _sync_aec_module()
    return _aec_endpoints._read_aec_state()


def _read_aec_mode() -> str:
    _sync_aec_module()
    return _aec_endpoints._read_aec_mode()


def _write_aec_mode(mode: str) -> None:
    _sync_aec_module()
    _aec_endpoints._write_aec_mode(mode)


def _write_aec_leg(leg: str, enabled: bool) -> None:
    _sync_aec_module()
    _aec_endpoints._write_aec_leg(leg, enabled)


def _write_audio_input_profile(profile: str) -> None:
    _sync_aec_module()
    _aec_endpoints._write_audio_input_profile(profile)


def _atomic_rewrite_env(path: str, updates: dict) -> None:
    _aec_endpoints._atomic_rewrite_env(path, updates)


def _read_wake_threshold() -> float:
    _sync_aec_module()
    return _aec_endpoints._read_wake_threshold()


def _write_wake_threshold(value: float) -> None:
    _sync_aec_module()
    _aec_endpoints._write_wake_threshold(value)


_aec_bridge_active_impl = _aec_endpoints._aec_bridge_active


def _aec_bridge_active() -> bool:
    return _aec_bridge_active_impl()


_server_aec_bridge_active_wrapper = _aec_bridge_active


def _kick_aec_reconciler() -> None:
    _aec_endpoints._kick_aec_reconciler()


_aec_fresh_jasper_env_impl = _aec_endpoints._fresh_jasper_env


def _fresh_jasper_env() -> dict[str, str]:
    _sync_aec_module()
    return _aec_fresh_jasper_env_impl()


_server_fresh_jasper_env_wrapper = _fresh_jasper_env


def _read_wake_word_status() -> dict[str, Any]:
    _sync_aec_module()
    return _aec_endpoints._read_wake_word_status()


def _audio_profile_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    _sync_aec_module()
    return _aec_endpoints._audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )


def _chip_aec_available() -> bool:
    return _aec_endpoints._chip_aec_available()


def _mic_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    _sync_aec_module()
    return _aec_endpoints._mic_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )


def _aec_full_status() -> dict:
    _sync_aec_module()
    previous_fresh_env = _aec_endpoints._fresh_jasper_env
    previous_bridge_active = _aec_endpoints._aec_bridge_active
    previous_validation_summary = _aec_endpoints._audio_validation_summary
    fresh_env_replacement = _fresh_jasper_env
    if fresh_env_replacement is _server_fresh_jasper_env_wrapper:
        fresh_env_replacement = _aec_fresh_jasper_env_impl
    bridge_replacement = _aec_bridge_active
    if bridge_replacement is _server_aec_bridge_active_wrapper:
        bridge_replacement = _aec_bridge_active_impl
    _aec_endpoints._fresh_jasper_env = fresh_env_replacement
    _aec_endpoints._aec_bridge_active = bridge_replacement
    _aec_endpoints._audio_validation_summary = _audio_validation_summary
    try:
        return _aec_endpoints._aec_full_status()
    finally:
        _aec_endpoints._fresh_jasper_env = previous_fresh_env
        _aec_endpoints._aec_bridge_active = previous_bridge_active
        _aec_endpoints._audio_validation_summary = previous_validation_summary


def _sync_dial_module() -> None:
    _dial.DIAL_HEARTBEAT_PATH = DIAL_HEARTBEAT_PATH
    _dial._dial_heartbeat = _dial_heartbeat


def _load_dial_heartbeat() -> dict[str, Any]:
    _sync_dial_module()
    return _dial._load_dial_heartbeat()


def _persist_dial_heartbeat(snapshot: dict[str, Any]) -> None:
    _sync_dial_module()
    _dial._persist_dial_heartbeat(snapshot)


async def _probe_dial_reachable(ip: str, *, timeout: float = 0.5) -> bool:
    return await _dial._probe_dial_reachable(ip, timeout=timeout)


def run_dial_log_listener(host: str, port: int) -> threading.Thread:
    _sync_dial_module()
    return _dial.run_dial_log_listener(host, port)


def _sync_source_availability_module() -> None:
    _state_aggregate._source_availability_cache = _source_availability_cache
    _state_aggregate._source_availability_lock = _source_availability_lock


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    global _source_availability_cache
    _sync_source_availability_module()
    result = _state_aggregate._augment_source_payload(payload)
    _source_availability_cache = _state_aggregate._source_availability_cache
    return result


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
) -> dict[str, Any]:
    return await _state_aggregate._get_state(
        camilla_host=camilla_host,
        camilla_port=camilla_port,
        voice_socket_path=voice_socket_path,
        voice_socket_command=_voice_socket_command,
        mux_socket_command=_mux_socket_command,
        local_status_json=_local_status_json,
        aec_full_status=_aec_full_status,
        dial_heartbeat=_dial_heartbeat,
        dial_probe=_probe_dial_reachable,
        read_transit_state_func=read_transit_state,
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


def _capture_system_diagnostics() -> tuple[HTTPStatus, dict[str, Any]]:
    """Run the root-fidelity doctor oneshot and read its JSON result."""
    result_path = os.environ.get(
        "JASPER_DIAGNOSTICS_RESULT_PATH",
        "/run/jasper-control/doctor-result.json",
    )
    try:
        proc = subprocess.run(
            ["systemctl", "start", "jasper-doctor-json.service"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return (
            HTTPStatus.BAD_GATEWAY,
            {"error": f"diagnostics capture failed: {e}"},
        )
    if proc.returncode != 0:
        # The oneshot couldn't run — polkit denial (rule missing) or a
        # hard start failure. The doctor itself exits 0 via --out even
        # when checks fail, so a non-zero here is never "report has
        # failures"; it's a genuine capture failure.
        return (
            HTTPStatus.BAD_GATEWAY,
            {
                "error": "diagnostics capture unavailable",
                "detail": (proc.stderr or "").strip()[:300],
            },
        )
    try:
        with open(result_path, encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return (
            HTTPStatus.BAD_GATEWAY,
            {"error": f"diagnostics result unreadable: {e}"},
        )
    if not isinstance(body, dict):
        return (
            HTTPStatus.BAD_GATEWAY,
            {"error": "diagnostics result unreadable: JSON root was not object"},
        )
    return HTTPStatus.OK, body

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


# Forwarded pair-volume requests carry this header; its presence stops a
# second hop (see _maybe_forward_volume_to_leader's loop breaker).
_PAIR_FORWARD_HEADER = "X-JTS-Pair-Forwarded"

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
    follower_leader_addr, so bond-validity semantics live in one place."""
    from ..multiroom.config import follower_leader_addr, load_config

    return follower_leader_addr(load_config())


def _kick_grouping_reconciler() -> None:
    """Apply a persisted grouping change through jasper-grouping-reconcile.

    Mirror of _kick_aec_reconciler: `restart` (not `start`) the Type=oneshot
    reconciler so a change written while a previous reconcile is still active
    is not a no-op. The reconciler is the single writer of the snapcast unit
    state + the outputd tap; this just nudges it to re-read grouping.env.
    """
    subprocess.Popen(
        ["systemctl", "restart", "--no-block",
         "jasper-grouping-reconcile.service"],
    )


def _write_grouping(
    *, enabled: bool, role: str, channel: str, bond_id: str, leader_addr: str,
    trim_db: "float | None" = None,
    client_latency_ms: "int | None" = None,
    left_delay_ms: "float | None" = None,
    right_delay_ms: "float | None" = None,
    peer_addr: "str | None" = None,
    peer_name: "str | None" = None,
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
        # caller omits it (bond/unbond/swap fan-outs never send trim, so
        # a calibrated balance survives role/channel changes).
        updates["JASPER_GROUPING_TRIM_DB"] = f"{trim_db:.1f}"
    if client_latency_ms is not None:
        updates["JASPER_GROUPING_CLIENT_LATENCY_MS"] = str(int(client_latency_ms))
    if left_delay_ms is not None:
        updates["JASPER_GROUPING_LEFT_DELAY_MS"] = f"{left_delay_ms:.3f}"
    if right_delay_ms is not None:
        updates["JASPER_GROUPING_RIGHT_DELAY_MS"] = f"{right_delay_ms:.3f}"
    # Bond roster (leader only): same preserved-when-omitted contract as
    # trim; an EXPLICIT empty string clears it (the bond flow clears the
    # roster on non-leader members so a role flip can't leave a stale
    # roster behind).
    if peer_addr is not None:
        updates["JASPER_GROUPING_PEER_ADDR"] = peer_addr
    if peer_name is not None:
        updates["JASPER_GROUPING_PEER_NAME"] = peer_name
    _atomic_rewrite_env(GROUPING_ENV_FILE, updates)







def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
    airplay_health_sampler: Any = None,
) -> type[BaseHTTPRequestHandler]:

    # One probe instance per handler — it's stateless (just closes
    # over voice_socket_path), so all volume ops share it. Read-only
    # `_get_op` doesn't need it (`get_listening_level` doesn't touch
    # camilla), but passing None there keeps the construction uniform.
    duck_active_probe = _make_duck_active_probe(voice_socket_path)
    state_response_cache = _SingleFlightTTLCache(STATE_RESPONSE_CACHE_TTL_SEC)
    diagnostics_response_cache = _NonBlockingTTLCache(
        DIAGNOSTICS_RESPONSE_CACHE_TTL_SEC,
    )

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
            # which reads `percent` but logs `db`. The legacy 50 dB
            # scale is still the lingua franca for clients that haven't
            # been updated.
            return {"db": round(_percent_to_db(percent), 3), "percent": percent}

        def _maybe_forward_volume_to_leader(self) -> bool:
            """Bonded-follower volume proxy. Returns True when the request
            was handled (forwarded or rejected) and the caller must stop.

            Used by the four /volume* handlers AND /transport/* — every
            surface where a bonded follower's local action must target
            the PAIR. While this speaker is an ACTIVE bonded follower,
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
                    "volume.pair_forward_rejected",
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
                    "volume.pair_forward_failed",
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
            if self._maybe_forward_volume_to_leader():
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
            # response. If the daemon isn't reachable, surface that
            # explicitly so the UI can grey out the toggle instead
            # of pretending we know the state.
            st = self._voice_cmd_or_error(
                "STATUS",
                timeout=2.0,
                missing_error=None,
                log_label="mic STATUS",
            )
            if st is None:
                return
            self._send_json({"muted": bool(st.get("mic_muted", False))})

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
            # Software AEC bridge state + per-leg config + wake
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
                    )),
                )
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
            except Exception:  # noqa: BLE001
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
            snap = dict(_dial_heartbeat)
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
            from .. import home_assistant as _ha_mod
            from ..speaker_name import read_state as _read_speaker_name_state
            from ..voice.provider_state import read_active_provider

            # HA probe is async + slow-ish (~50-200 ms typical against
            # a healthy local HA, fails fast on unreachable). Run it
            # via asyncio.run so the rest of /system/snapshot stays
            # synchronous like the existing handler.
            try:
                # Same env-file-direct read as /state.home_assistant
                # above — wizard saves must reflect immediately in the
                # dashboard without restarting jasper-control.
                ha_status = asyncio.run(_ha_mod.probe_status_from_env())
            except Exception:  # noqa: BLE001
                # Fail-soft per the existing aggregator convention —
                # never break /system/snapshot because HA is wedged.
                ha_status = {
                    "configured": False, "connected": False, "url": "",
                    "instance_name": None, "version": None,
                    "error": "probe failed",
                }

            try:
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
                outputd_status = asyncio.run(_outputd_status())
            except Exception:  # noqa: BLE001
                logger.exception("outputd status snapshot failed")
                outputd_status = None

            install_profile = _control_install_profile()
            payload: dict[str, Any] = {
                "build": read_build_info(),
                "metrics": (
                    sampler.snapshot() if sampler is not None else None
                ),
                "airplay_health": airplay_health,
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
        # unit is in MANAGED_UNITS). `systemctl start` of a Type=oneshot blocks
        # until the doctor finishes and writes the group-readable result file.
        def _get_system_diagnostics(self) -> None:
            # ~3-5 s on a Pi 5; the dashboard shows a spinner and single-flights
            # the button. A root caller (pre-drop / rollback) authorizes the
            # start directly; the non-root jasper-control via the polkit rule.
            # The result path matches jasper-doctor-json.service's --out target;
            # env-overridable for tests (never set in production).
            cached = diagnostics_response_cache.get_fresh()
            if cached is not _MISSING:
                status, body = cached
                self._send_json(body, status=status)
                return
            if not diagnostics_response_cache.try_begin_refresh():
                self._send_json(
                    {
                        "error": "diagnostics capture already running",
                        "pending": True,
                        "retry_after": 2,
                    },
                    status=HTTPStatus.ACCEPTED,
                )
                return
            try:
                status, body = _capture_system_diagnostics()
            except Exception as e:  # noqa: BLE001
                status = HTTPStatus.BAD_GATEWAY
                body = {"error": str(e)}
                diagnostics_response_cache.finish_refresh((status, body), cache=True)
                logger.exception("diagnostics capture crashed")
                self._send_json(body, status=status)
                return
            diagnostics_response_cache.finish_refresh(
                (status, body),
                cache=True,
            )
            self._send_json(body, status=status)

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
            # the other gated routes (poweroff/reboot/restart/mic-mute) are
            # browser->own-speaker and stay control-token-only. household_credential
            # is fail-safe (absent => accept) so the first bond, which DISTRIBUTES
            # the secret over this very route, isn't rejected by the gate it
            # installs. See docs/HANDOFF-control-plane-auth.md §6.
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
            if self._maybe_forward_volume_to_leader():
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
            if self._maybe_forward_volume_to_leader():
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
            trim_db: float | None = None
            if "trim_db" in body:
                try:
                    trim_db = float(body["trim_db"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "trim_db must be a number"}, status=400,
                    )
                    return
            client_latency_ms: int | None = None
            if "client_latency_ms" in body:
                try:
                    client_latency_ms = int(body["client_latency_ms"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "client_latency_ms must be an integer"},
                        status=400,
                    )
                    return
            left_delay_ms: float | None = None
            if "left_delay_ms" in body:
                try:
                    left_delay_ms = float(body["left_delay_ms"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "left_delay_ms must be a number"},
                        status=400,
                    )
                    return
            right_delay_ms: float | None = None
            if "right_delay_ms" in body:
                try:
                    right_delay_ms = float(body["right_delay_ms"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "right_delay_ms must be a number"},
                        status=400,
                    )
                    return
            peer_addr: str | None = None
            if "peer_addr" in body:
                peer_addr = str(body.get("peer_addr") or "").strip()
            peer_name: str | None = None
            if "peer_name" in body:
                peer_name = str(body.get("peer_name") or "").strip()
            # Validate an ENABLED request up front via the SHARED
            # validate_grouping (same rule the config loader applies on
            # read) so we never persist a fail-loud config. A disabled
            # request needs no fields.
            if enabled:
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
                    peer_addr=peer_addr or "",
                    peer_name=peer_name or "",
                )
                if err:
                    self._send_json({"error": err}, status=400)
                    return
            try:
                _write_grouping(
                    enabled=enabled, role=role, channel=channel,
                    bond_id=bond_id, leader_addr=leader_addr,
                    trim_db=trim_db,
                    client_latency_ms=client_latency_ms,
                    left_delay_ms=left_delay_ms,
                    right_delay_ms=right_delay_ms,
                    peer_addr=peer_addr, peer_name=peer_name,
                )
                _kick_grouping_reconciler()
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
                client=self.address_string(),
            )
            self._send_json({
                "ok": True, "enabled": enabled, "role": role,
                "channel": channel, "bond_id": bond_id,
                "leader_addr": leader_addr,
            })
            return

        def _post_volume_mute(self) -> None:
            if self._maybe_forward_volume_to_leader():
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
            if self._maybe_forward_volume_to_leader():
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
            result = self._voice_cmd_or_error(
                cmd,
                missing_error="voice_daemon not running (socket not found)",
                log_label=f"session {cmd}",
            )
            if result is None:
                return
            # Result codes from voice_daemon's manual_session_*:
            #   OK / BUSY / CAP / PAUSED / MUTED / MEASURING /
            #   NO_SESSION / ALREADY_ENDED / ERROR
            # Map non-OK outcomes to non-2xx so the dial's HTTP
            # error path can show the right LED color.
            http_status = 200
            if result.get("result") not in ("OK", None):
                if result.get("result") in ("CAP", "PAUSED", "MUTED", "MEASURING"):
                    http_status = 503
                elif result.get("result") in ("BUSY", "NO_SESSION", "ALREADY_ENDED"):
                    http_status = 409
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
            # Flip JASPER_AEC_MODE between auto and disabled, then
            # kick the reconciler. The reconciler stops/starts
            # jasper-aec-bridge.service and restarts jasper-voice
            # with the new JASPER_MIC_DEVICE (udp:9876 vs chip
            # direct). Called by the /wake/ page's AEC layer toggle
            # (after a current-state read for idempotent set-state
            # semantics). Non-blocking — the wizard polls /aec to
            # see when the transition lands (~10-15 s). The kick
            # uses systemctl restart so rapid toggles cannot be
            # swallowed while the oneshot reconciler is already active.
            #
            # Risk model: LAN-local + browser-origin guard, same
            # as /system/restart/*. This is still not auth; it is
            # the small boundary that blocks cross-site browser
            # POSTs and DNS-rebinding Host headers while keeping
            # curl, local proxies, and accessories working.
            current = _read_aec_mode()
            new_mode = "disabled" if current == "auto" else "auto"
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
            if leg not in _TOGGLE_TO_TOKEN:
                self._send_json(
                    {"error": "leg must be one of: "
                              + ", ".join(sorted(_TOGGLE_TO_TOKEN))},
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
                        *AUDIO_QUALITY_RENDERER_UNITS,
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
                "try_restart_units": AUDIO_QUALITY_RENDERER_UNITS,
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
                action = "restart-voice"
            elif self.path == "/system/restart/audio":
                units = [
                    "jasper-camilla.service",
                    "librespot.service",
                    "shairport-sync.service",
                    "bluealsa-aplay.service",
                ]
                if parked:
                    # Restart only the units the follower profile keeps
                    # alive — derived from the parked set so the two
                    # can never drift (FOLLOWER_PARKED_UNITS is the one
                    # source of truth for what a follower parks).
                    from ..multiroom.reconcile import FOLLOWER_PARKED_UNITS

                    units = [
                        u for u in units if u not in FOLLOWER_PARKED_UNITS
                    ]
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
                    # Use start-after-stop semantics. Don't block
                    # on the systemctl call (jasper-aec-bridge +
                    # jasper-voice both take up to 90s to stop
                    # cleanly under the SIGTERM timeout).
                    subprocess.Popen(["systemctl", "restart", *units])
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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            self._admission.release()
            self.shutdown_request(request)
            raise

    def _handle_in_pool(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: BLE001
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
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
) -> ControlHTTPServer:
    return ControlHTTPServer(
        (host, port),
        _make_handler(
            camilla_host,
            camilla_port,
            voice_socket_path,
            sampler,
            airplay_health_sampler,
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
    # AirPlay health sampler — cheap fan-in counters at 5 s, slower
    # journal/MPRIS/Camilla probes for the /system AirPlay card.
    from .airplay_health import AirPlayHealthSampler
    airplay_health_sampler = AirPlayHealthSampler(
        camilla_host=args.camilla_host,
        camilla_port=args.camilla_port,
    )
    airplay_health_sampler.start()

    server = build_server(
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
        sampler=sampler,
        airplay_health_sampler=airplay_health_sampler,
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
