# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP control surface for local and household-network clients.

Stack: stdlib http.server (bounded ThreadingHTTPServer), pycamilladsp
client, VolumeCoordinator (source-aware dispatch). The route tables live
in `_make_handler`; `do_GET`/`do_POST` own dispatch in one place.

- /state: cross-daemon JSON snapshot — voice / audio / renderers;
  consumable from the management UI, jasper-doctor, or `curl`.
- /cue/play: proxy to voice_daemon's UDS so a cue plays through
  the daemon's already-correctly-gained TtsPlayout.

Volume dispatch builds a fresh VolumeCoordinator per call: it reads the
canonical volume state (`volume_persistence.configured_path()`), applies
the change, dispatches the derived effective level to the active source
(or CamillaDSP when idle), and persists it. This daemon runs no inbound
observers — that's voice_daemon's job; both converge through persistence
and share the same VolumeState interpretation.
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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from jasper.log_event import log_event

if TYPE_CHECKING:
    from ..volume_coordinator import VolumeState

from ..camilla_config_contract import DEFAULT_CAMILLA_PORT
from ..http_security import management_read_allowed, mutating_request_allowed
from .client import CONTROL_PORT
from ..atomic_io import locked_update_env_file
from ..fanin.latency_mode import (
    options as _usb_latency_options,
    read_state as _read_usb_latency_state,
)
from . import (
    debug_control,
    grouping_supervisor,
    shairport_supervisor,
    system_supervisor,
)
from ..multiroom.config import GROUPING_ENV_FILE, GroupingConfig
from ..music_sources import MUSIC_SOURCE_SPECS
from ..local_sources import local_source_audio_refresh_units
from ..transit.state import read_state as read_transit_state
from ..active_speaker.setup_status import read_active_speaker_setup_status
from ..doctor_contract import (
    REASON_REFRESH_FAILED,
    CheckResult,
    check_row,
    summarize,
)
from ..install_profile import (
    STREAMBOX_INSTALL_PROFILE,
    install_profile_allows_voice_brain,
    install_role_for_profile,
    read_install_profile,
)
from . import aec_endpoints as _aec_endpoints
from . import control_token
from . import household_credential
from . import restart_broker
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
# Ceiling on how long a start can be treated as in flight. Sized to
# `TimeoutStartSec=600` in deploy/systemd/jasper-doctor-json.service: systemd
# cannot leave the oneshot activating longer, so an older start is over
# whatever became of it. Within the ceiling the authority is systemd itself
# (`_diagnostics_unit_in_flight`), and a run that lands clears the window
# early — see `_start_diagnostics_refresh`.
_DIAGNOSTICS_REFRESH_WINDOW_SECONDS = 600.0
_diagnostics_refresh_lock = threading.Lock()
_diagnostics_refresh_started_at: float | None = None
_USB_MIC_APPLY_UNIT = "jasper-usbmic-apply.service"
_AEC_BRIDGE_UNIT = "jasper-aec-bridge.service"
_USB_MIC_LEG_APPLY_COALESCE_SECONDS = 5.0
_usb_mic_leg_apply_lock = threading.Lock()
_usb_mic_leg_apply_pending: tuple[str, float] | None = None
# Serializes POST /aec/commission's check-then-start across
# ThreadingHTTPServer workers, so two clicks cannot both pass the is-active
# probe before either start lands.
_aec_commission_start_lock = threading.Lock()


def _diagnostics_unit_in_flight() -> bool:
    """Is the doctor oneshot still running? Asked of systemd, not inferred from
    the elapsed time, so a run that died WITHOUT writing a report (OOM-killed,
    crashed) reopens the window on the next request instead of holding it for
    the whole `_DIAGNOSTICS_REFRESH_WINDOW_SECONDS`.

    Unreadable answers hold the window: not knowing must not become a restart
    per request. A `oneshot` reads `activating` while it runs.
    """
    try:
        proc = _run_unit_systemctl(
            "show", "--property=ActiveState", "--value", "jasper-doctor-json.service",
        )
    except (subprocess.SubprocessError, OSError):
        return True
    if proc.returncode != 0:
        return True
    return (proc.stdout or "").strip() in ("activating", "active", "deactivating")


def _start_diagnostics_refresh(
    *,
    snapshot_age_seconds: float | None,
) -> tuple[bool, str]:
    """Start the root doctor oneshot unless one this process started is
    still running. Returns ``(refreshing, error)``, where `refreshing` is
    true only when a start is genuinely in flight."""
    global _diagnostics_refresh_started_at
    now = time.monotonic()
    with _diagnostics_refresh_lock:
        started_at = _diagnostics_refresh_started_at
    # A snapshot younger than the elapsed run is that run's own output: it
    # landed, so the window is over. Systemd is asked only when the cheap
    # checks still allow a run to be in flight, and outside the lock — the
    # probe is a subprocess.
    if started_at is not None:
        elapsed = now - started_at
        if (
            elapsed < _DIAGNOSTICS_REFRESH_WINDOW_SECONDS
            and (snapshot_age_seconds is None or snapshot_age_seconds > elapsed)
            and _diagnostics_unit_in_flight()
        ):
            return True, ""
    with _diagnostics_refresh_lock:
        _diagnostics_refresh_started_at = now
    try:
        proc = _run_unit_systemctl(
            "--no-block", "start", "jasper-doctor-json.service",
        )
        error = "" if proc.returncode == 0 else (
            "diagnostics refresh unavailable: "
            + (proc.stderr or "").strip()[:300]
        )
    except (subprocess.SubprocessError, OSError) as e:
        error = f"diagnostics refresh failed: {e}"
    if error:
        with _diagnostics_refresh_lock:
            _diagnostics_refresh_started_at = None
    return not error, error


def _diagnostics_placeholder_result(
    *,
    detail: str,
    status: str,
    reason: str,
    refreshing: bool,
) -> dict[str, Any]:
    result = CheckResult("jasper-doctor", status, detail, reason=reason)
    counts = summarize([result])
    return {
        "fails": counts["fails"],
        "warns": counts["warns"],
        "generated_at_epoch": None,
        "duration_sec": None,
        "cache_age_seconds": None,
        "stale": True,
        "refreshing": refreshing,
        "results": [check_row(result)],
    }


def _append_diagnostics_refresh_failure(
    body: dict[str, Any],
    refresh_error: str,
) -> None:
    row = check_row(CheckResult(
        "jasper-doctor refresh",
        "fail",
        refresh_error,
        reason=REASON_REFRESH_FAILED,
    ))
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
    generated_at = body.get("generated_at_epoch")
    if not isinstance(generated_at, (int, float)):
        generated_at = stat.st_mtime
        body["generated_at_epoch"] = generated_at
    age = max(0.0, time.time() - generated_at)
    body["cache_age_seconds"] = round(age, 3)
    body["stale"] = age > ttl_seconds
    body.setdefault("refreshing", False)
    return body, ""


# Streambox is the restricted profile: these are the management + audio
# actions every streambox owns. Capability-granted routes are added on
# top by _control_route_allowed_for_install_profile, not listed here.
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
    "/usb-forensics",
    "/system/reboot",
    "/system/poweroff",
    "/source/select",
    "/system/audio-quality",
    "/system/usb-latency",
    "/system/restart/audio",
    "/transport/next",
    "/transport/previous",
    "/transport/toggle",
})
# Routes a restricted profile earns from its CAPABILITY grant rather than
# from its tier name. The local-mic/wake/AEC routes are deliberately
# absent — they need Capability.WAKE_DETECTION, which a streambox is not
# granted. See ADR-0217.
_ASSISTANT_POST_ROUTES = frozenset({
    "/session/start",
    "/session/end",
    "/cue/play",
    "/system/restart/voice",
})


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
        # Back-compat alias for the landing page's field name.
        "safety_muted": not bool(setup.get("volume_allowed")),
        "level_match_provisional": _active_speaker_level_match_provisional(setup),
        "source": "active_speaker.setup_status",
    }


def _active_speaker_volume_block() -> dict[str, Any] | None:
    setup = read_active_speaker_setup_status()
    if setup.get("volume_allowed") is not True:
        return setup
    return None


def _active_speaker_grouping_evaluation(
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the public grouping-readiness verdict and any blocking setup.

    Both GET /grouping's preflight projection and POST /grouping/set's final
    mutation guard call this one policy seam, so the advisory read can never
    drift from the target-side fail-closed decision.
    """
    setup = read_active_speaker_setup_status()
    if setup.get("grouping_allowed") is not True:
        detail = str(
            setup.get("detail")
            or "active speaker setup is not ready for grouping"
        )
        return {"allowed": False, "detail": detail}, setup
    return {"allowed": True, "detail": "ready"}, None


def _active_speaker_grouping_block() -> dict[str, Any] | None:
    return _active_speaker_grouping_evaluation()[1]

# The high-impact mutations the control token gates (SECURITY.md).
# The primitive remains fail-safe-open when no /var/lib/jasper/control_token file
# exists, but jasper-control ensures one at startup so production installs are
# gated automatically.
# Deliberately NOT including /volume*, /transport*, /source* — routine
# low-impact accessory and automation controls stay open. poweroff/reboot =
# power loop; mic/mute = defeats the privacy-mic
# promise; grouping/set = hijacks output routing; restart/voice|audio =
# disrupt playback + the assistant; usb-forensics can restart the composite
# gadget; aec/firmware/update downloads and flashes microphone firmware;
# aec/usb-mic = starts or stops live room-audio export; aec/usb-mic-leg =
# changes which live room-audio stream reaches the computer; aec/commission =
# stops voice/AEC for minutes and plays audible measurement sweeps.
_TOKEN_GATED_ROUTES = frozenset({
    "/system/poweroff",
    "/system/reboot",
    "/system/restart/voice",
    "/system/restart/audio",
    "/usb-forensics",
    "/mic/mute",
    "/aec/usb-mic",
    "/aec/usb-mic-leg",
    "/grouping/set",
    "/aec/firmware/update",
    "/aec/enhanced-aec/install",
    "/aec/commission",
    # measurement/hold|release own the cross-process measurement mutex: a hold
    # gates household volume observations and, once taken, refuses every other
    # measurement. A drive-by acquire would silently wedge the host slider for
    # two minutes at a time; a drive-by release would un-gate somebody's live
    # capture mid-sweep.
    "/measurement/hold",
    "/measurement/release",
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
    if role != STREAMBOX_INSTALL_PROFILE:
        # Full speakers allow every route.
        return True
    if method == "GET":
        return path in _STREAMBOX_ALLOWED_GET_ROUTES
    if method != "POST":
        return False
    if path in _STREAMBOX_ALLOWED_POST_ROUTES:
        return True
    return (
        path in _ASSISTANT_POST_ROUTES
        and install_profile_allows_voice_brain(profile)
    )


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

        `wait()` is intentionally un-timed: a waiter blocks only for as long
        as the single in-flight `compute()` runs, so `compute` MUST be
        self-bounding (the /state aggregate enforces its own liveness
        budget). An unbounded compute parks every waiter and, on the bounded
        request pool, the whole control plane.
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
_read_volume_state = _volume_ops.read_volume_state


def _safe_audio_quality_state() -> dict[str, Any]:
    return _state_aggregate._safe_audio_quality_state()


_USB_LATENCY_APPLY_GRACE_SEC = 30.0
_usb_latency_applying: tuple[str, float] | None = None


def _mark_usb_latency_applying(mode: str) -> None:
    global _usb_latency_applying
    _usb_latency_applying = (mode, time.monotonic() + _USB_LATENCY_APPLY_GRACE_SEC)


def _usb_latency_applying_mode() -> str | None:
    global _usb_latency_applying
    current = _usb_latency_applying
    if current is None:
        return None
    if current[1] <= time.monotonic():
        _usb_latency_applying = None
        return None
    return current[0]


def _safe_usb_latency_state(airplay_health: Any = None) -> dict[str, Any]:
    global _usb_latency_applying
    try:
        applying_mode = _usb_latency_applying_mode()
        state = _read_usb_latency_state(
            airplay_health,
            applying_mode=applying_mode,
        )
        if applying_mode is not None and state.get("state") != "applying":
            if (
                _usb_latency_applying is not None
                and _usb_latency_applying[0] == applying_mode
            ):
                _usb_latency_applying = None
        return state
    except Exception as e:  # noqa: BLE001
        logger.exception("USB latency state read failed")
        return {
            "selected_mode": "low",
            "applied_mode": None,
            "effective_mode": None,
            "state": "error",
            "detail": "USB latency state could not be read.",
            "error": str(e),
            "live_buffer_frames": None,
            "live_buffer_ms": None,
            "options": _usb_latency_options(),
        }


def _run_unit_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )


def _reset_oneshot_unit(unit: str, *, event: str) -> None:
    """Fail-soft and best-effort: a reset-failed failure must never block
    the start/restart it precedes.  Both callers' units are bare oneshots
    with no RemainAfterExit, so systemd normally GCs them between runs, and
    reset-failed against an already-unloaded unit routinely exits nonzero
    (#3237)."""
    try:
        result = _run_unit_systemctl("reset-failed", unit)
    except (OSError, subprocess.SubprocessError) as exc:
        log_event(
            logger,
            event,
            unit=unit,
            error=str(exc),
            level=logging.WARNING,
        )
        return
    if result.returncode != 0:
        log_event(
            logger,
            event,
            unit=unit,
            returncode=result.returncode,
            detail=(result.stderr or result.stdout).strip().replace(
                "\n", " | ",
            ),
            level=logging.WARNING,
        )


def _run_oneshot_start(
    unit: str,
    verb: str,
    *,
    event_prefix: str,
    extra_fields: dict[str, Any] | None = None,
) -> bool:
    """Reset then no-block start/restart one maintenance oneshot, observably.

    ``event_prefix`` is ``<owner>.<action>``: the failure/scheduled events are
    ``<event_prefix>_failed`` / ``<event_prefix>_scheduled`` and the
    best-effort reset logs ``<owner>.reset_failed_skipped``. ``extra_fields``
    ride on the scheduled event only. The reset clears systemd's
    failure/start-rate state so each explicit user action gets a fresh,
    bounded retry budget.
    """
    owner = event_prefix.rsplit(".", 1)[0]
    _reset_oneshot_unit(unit, event=f"{owner}.reset_failed_skipped")
    try:
        result = _run_unit_systemctl(verb, "--no-block", unit)
    except (OSError, subprocess.SubprocessError) as exc:
        log_event(
            logger,
            f"{event_prefix}_failed",
            unit=unit,
            phase="enqueue",
            error=str(exc),
            level=logging.ERROR,
        )
        return False
    if result.returncode != 0:
        log_event(
            logger,
            f"{event_prefix}_failed",
            unit=unit,
            phase="enqueue",
            returncode=result.returncode,
            detail=(result.stderr or result.stdout).strip().replace(
                "\n", " | ",
            ),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        f"{event_prefix}_scheduled",
        unit=unit,
        **(extra_fields or {}),
    )
    return True


def _schedule_usb_gadget_recompose() -> bool:
    """Hand delayed, debounced apply to systemd before returning to the client.

    Restarting an already-running oneshot cancels its 350 ms grace sleep and
    begins it again, so rapid switch changes naturally debounce.  Unlike an
    in-process Timer, the durable intent's apply job survives jasper-control
    exiting after this request.
    """

    return _run_oneshot_start(
        _USB_MIC_APPLY_UNIT,
        "restart",
        event_prefix="usb_mic.recompose",
        extra_fields={"grace_ms": 350, "max_attempts": 4},
    )


def _aec_commission_running() -> bool:
    return _aec_endpoints._unit_active(_aec_endpoints._AEC_COMMISSION_SERVICE)


def _start_aec_commission() -> bool:
    """Hand the audible re-commissioning run to systemd before returning.

    ``--no-block``: the run takes minutes and the browser only needs the job
    accepted — the /aec poll's ``commission.running`` probe tracks the rest.
    """
    return _run_oneshot_start(
        _aec_endpoints._AEC_COMMISSION_SERVICE,
        "start",
        event_prefix="aec_commission.start",
    )


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _state_aggregate._augment_source_payload(payload)


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    ha_status_snapshot: Callable[[], dict[str, Any]] | None = None,
    transport_park_snapshot: Callable[[], dict[str, Any]] | None = None,
    service_states_snapshot: (
        Callable[[], dict[str, dict[str, Any]]] | None
    ) = None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if transport_park_snapshot is not None:
        extra["transport_park_snapshot"] = transport_park_snapshot
    return await _state_aggregate._get_state(
        service_states_snapshot=service_states_snapshot,
        camilla_host=camilla_host,
        camilla_port=camilla_port,
        voice_socket_path=voice_socket_path,
        voice_socket_command=_voice_socket_command,
        mux_socket_command=_mux_socket_command,
        local_status_json=_local_status_json,
        aec_full_status=_aec_endpoints._aec_full_status,
        read_transit_state_func=read_transit_state,
        ha_status_snapshot=ha_status_snapshot,
        **extra,
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

# The peering daemon runs an asyncio event loop; jasper-control is stdlib
# threaded HTTP. One background daemon thread owns that loop. When peering
# is OFF (the default) the thread is never created — zero cost on a
# single-Pi household.
_peering_thread: threading.Thread | None = None


def _run_peering_loop() -> None:
    """Background thread target: own an asyncio loop and run the PeeringDaemon."""
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
    """Start the peering daemon in a background thread iff peering is enabled
    in /var/lib/jasper/peering.env. Idempotent.

    The enabled check runs in the worker thread, not here, so an OFF
    household never pays the zeroconf import.
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

# Patch seam scoping a test double to the forward's ONE network call;
# patching stdlib urllib.request.urlopen would also intercept the test
# driver's own HTTP client.
_pair_urlopen = urllib.request.urlopen


def _pair_follower_leader_addr() -> str | None:
    """The leader's handle when THIS speaker is an active bonded follower,
    else None. One tiny env-file read per call (multiroom.config.load_config
    — never the runtime derive with its systemctl/RPC probes: this gates
    every /volume request). The predicate is the shared effective-role
    reader, so a refused bond that safely landed solo does not forward local
    controls to the requested leader."""
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

    The voice daemon creates its UDS socket late in startup, so during a
    restart/provider switch/unbond a missing socket means "not ready yet",
    not "offline". The distinction is drawn here so the landing page stays a
    dumb renderer of /mic state.
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

    The HTTP handler writes grouping.env before calling this, and the oneshot
    reconciler re-reads grouping.env when it finally runs, so the last write
    wins without restarting outputd for every trim/delay sweep step.
    The packaged trailing service survives a jasper-control restart.
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
    Type=oneshot pass drains before one fresh pass launches. Rapid
    /grouping/set bursts coalesce, and a skipped kick always arms one trailing
    retry, so the final grouping.env write is always applied.
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


def _parse_grouping_optional_fields(
    body: dict[str, Any],
) -> tuple[_GroupingOptionalFields | None, str | None]:
    """Parse optional ``/grouping/set`` scalars without HTTP side effects.

    Fields intentionally retain Python ``int``/``float`` coercion.
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
    ):
        if key not in body:
            continue
        try:
            parsed[key] = caster(body[key])
        except (TypeError, ValueError):
            return None, error

    return _GroupingOptionalFields(
        trim_db=parsed.get("trim_db"),
        client_latency_ms=parsed.get("client_latency_ms"),
        left_delay_ms=parsed.get("left_delay_ms"),
        right_delay_ms=parsed.get("right_delay_ms"),
    ), None


def _write_grouping(
    *, enabled: bool, role: str, channel: str, bond_id: str, leader_addr: str,
    trim_db: "float | None" = None,
    client_latency_ms: "int | None" = None,
    left_delay_ms: "float | None" = None,
    right_delay_ms: "float | None" = None,
    peer_addr: "str | None" = None,
    peer_name: "str | None" = None,
    roster: "str | None" = None,
) -> None:
    """Persist a grouping role into the wizard-owned grouping.env.

    Read-modify-write (via locked_update_env_file) so operator-tuned
    JASPER_GROUPING_BUFFER_MS / _CODEC survive a role change. This is the
    single control-plane WRITER of grouping.env; jasper-grouping-reconcile is
    the single READER->action. The endpoint that calls this (/grouping/set) is
    token-gated; the cross-device bond-forming flow — one speaker POSTing to
    another's :PORT/grouping/set — authenticates with the household
    credential.
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
    # Peer and roster (leader only): same preserved-when-omitted contract as
    # trim, and an EXPLICIT empty string clears — the bond flow clears both on
    # non-leader members so a role flip can't leave a stale roster behind.
    if peer_addr is not None:
        updates["JASPER_GROUPING_PEER_ADDR"] = peer_addr
    if peer_name is not None:
        updates["JASPER_GROUPING_PEER_NAME"] = peer_name
    # `roster` is the already SERIALIZED env string (callers build it via
    # config.format_roster).
    if roster is not None:
        updates["JASPER_GROUPING_ROSTER"] = roster
    locked_update_env_file(GROUPING_ENV_FILE, updates, mode=0o644)



def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
    airplay_health_sampler: Any = None,
    audio_health_sampler: Any = None,
    ha_status_cache: Any = None,
) -> type[BaseHTTPRequestHandler]:

    # Route-body imports stay factory-local so importing this module stays
    # cheap: the concern mixins arrive only when a concrete server is built.
    from .handlers import (
        AecRoutes,
        GroupingRoutes,
        MeasurementRoutes,
        SystemRoutes,
        VoiceRoutes,
        VolumeRoutes,
    )

    # One probe instance per handler — stateless (it only closes over
    # voice_socket_path), so all mutating volume ops share it. Read-only
    # `_get_op` bypasses coordinator/actuator construction.
    duck_active_probe = _make_duck_active_probe(voice_socket_path)
    state_response_cache = _SingleFlightTTLCache(STATE_RESPONSE_CACHE_TTL_SEC)
    if ha_status_cache is None:
        from .ha_status_cache import HomeAssistantStatusCache

        ha_status_cache = HomeAssistantStatusCache()

    async def _set_op(percent: int) -> VolumeState:
        async def _op(coord):
            await coord.set_listening_level(percent)
            return coord.get_volume_state()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _observe_op(
        source_name: str,
        percent: int,
        *,
        initial: bool = False,
    ) -> tuple[VolumeState, bool]:
        """Route a source-observed volume change (e.g. host slider on the USB
        gadget) through the coordinator's echo-prevented observe path. Unknown
        source names fall back to the authoritative set path so a client
        posting a fresh source name doesn't silently no-op.

        Returns the level the coordinator ended up at plus whether the
        observation was accepted. That explicit acknowledgement lets a
        long-lived observer retry an initial value that arrived before its
        source became active, instead of reading HTTP 200 as applied.
        """
        # Lazy import to keep the full volume_coordinator graph out of
        # server.py's module load.
        from ..volume_coordinator import Source
        try:
            source_enum = Source(source_name)
        except ValueError:
            return await _set_op(percent), True

        async def _op(coord):
            applied = await coord.observe_source_volume(
                source_enum,
                percent,
                initial=initial,
            )
            # Return the one canonical state projection rather than asking
            # this boundary to reinterpret mute.
            return coord.get_volume_state(), bool(applied)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _adjust_op(delta_percent: int) -> VolumeState:
        async def _op(coord):
            await coord.adjust_listening_level(delta_percent)
            return coord.get_volume_state()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _get_op() -> VolumeState:
        return _read_volume_state()

    async def _mute_set_op(want_muted: bool) -> VolumeState:
        async def _op(coord):
            return await coord.set_muted(want_muted)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _mute_toggle_op() -> VolumeState:
        async def _op(coord):
            return await coord.toggle_mute()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    # A class body does not close over a same-named function local when the
    # class also assigns that name, so the aliases below are required.
    handler_adjust_op = _adjust_op
    handler_get_op = _get_op
    handler_mute_set_op = _mute_set_op
    handler_mute_toggle_op = _mute_toggle_op
    handler_observe_op = _observe_op
    handler_set_op = _set_op

    class Handler(
        VolumeRoutes,
        VoiceRoutes,
        AecRoutes,
        GroupingRoutes,
        MeasurementRoutes,
        SystemRoutes,
    ):
        _airplay_health_sampler = airplay_health_sampler
        _adjust_op = staticmethod(handler_adjust_op)
        _audio_health_sampler = audio_health_sampler
        _camilla_host = camilla_host
        _camilla_port = camilla_port
        _get_op = staticmethod(handler_get_op)
        _ha_status_cache = ha_status_cache
        _mute_set_op = staticmethod(handler_mute_set_op)
        _mute_toggle_op = staticmethod(handler_mute_toggle_op)
        _observe_op = staticmethod(handler_observe_op)
        _sampler = sampler
        _set_op = staticmethod(handler_set_op)
        _state_response_cache = state_response_cache
        _voice_socket_path = voice_socket_path

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
            refusal_event: str | None = None,
        ) -> dict[str, Any] | None:
            try:
                kwargs = {} if timeout is None else {"timeout": timeout}
                return asyncio.run(
                    _voice_socket_command(voice_socket_path, cmd, **kwargs),
                )
            except (OSError, asyncio.TimeoutError) as e:
                # FileNotFoundError is an OSError subtype; it gets the
                # caller's friendlier missing_error text where one is given,
                # everything else (ConnectionRefusedError, read timeout, ...)
                # the generic message. Both mean the same thing to a
                # caller: the daemon could not be reached right now.
                error = (
                    missing_error
                    if isinstance(e, FileNotFoundError) and missing_error is not None
                    else f"voice_daemon unreachable: {e}"
                )
                if refusal_event:
                    log_event(
                        logger, refusal_event,
                        reason="voice_daemon_unreachable", cmd=cmd,
                    )
                self._send_json(
                    {"error": error, "reason": "voice_daemon_unreachable"},
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

        def _volume_payload(self, state: VolumeState) -> dict[str, Any]:
            """Serialize the coordinator's one canonical volume projection.

            ``percent`` and ``db`` are always the currently effective values,
            so a temporary mute reports 0 while ``restore_percent`` preserves
            its separate restore target — a client reading only ``percent``
            stays correct, and no client has to infer mute.
            """
            percent = int(state.effective_percent)
            return {
                "db": round(_volume_ops._percent_to_db(percent), 3),
                "percent": percent,
                "muted": bool(state.muted),
                "restore_percent": state.restore_percent,
            }

        def _maybe_forward_pair_action_to_leader(self) -> bool:
            """Bonded-follower pair-action proxy. Returns True when the request
            was handled (forwarded or rejected) and the caller must stop.

            Used by the four /volume* handlers, /transport/*, and
            /source/select — every surface where a bonded follower's local
            action must target the PAIR. While this speaker is an ACTIVE
            bonded follower its local volume knobs are INERT: bonded content
            bypasses the local CamillaDSP entirely (the leader's one Camilla
            bakes the program). So those requests are forwarded verbatim to
            the leader's control API and its answer relayed, and every
            member's volume surface controls the PAIR volume. Solo and leader
            requests never enter this path; the grouping read is one tiny
            env-file parse (load_config), NOT the heavy runtime derive — this
            sits on every volume call.
            """
            leader = _pair_follower_leader_addr()
            if leader is None:
                return False
            # Loop breaker: a forwarded request never re-forwards. Two
            # speakers misconfigured as each other's follower would
            # otherwise ping-pong until a timeout stack built up.
            if self.headers.get(_PAIR_FORWARD_HEADER):
                # Drain any body before responding so connection state stays
                # sane if keep-alive is ever enabled (HTTP/1.0 today).
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
                # The leader ANSWERED — relay its status + JSON body verbatim.
                # Collapsing a 400 invalid-body reject into "unreachable"
                # would report a responding speaker as offline.
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
                # Additive marker so UIs can label the slider "pair volume".
                payload.setdefault("pair_leader", leader)
            self._send_json(payload)
            return True

        # --- routes ---
        #
        # do_GET / do_POST own the dispatch via the _GET_ROUTES /
        # _POST_ROUTES tables (path -> handler-method name) at the bottom of
        # this class.
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
            # fan-out (rooms_setup) or an autonomous re-group presents the
            # household credential (X-JTS-Household), which each member verifies
            # against its own persisted copy — NOT the per-device CSRF token a
            # leader can't hold for a follower. Accept EITHER on this route only;
            # the other gated routes (poweroff/reboot/restart/mic-mute/firmware
            # update) are browser->own-speaker and stay control-token-only.
            # household_credential is fail-safe (absent => accept) so the first
            # bond, which DISTRIBUTES the secret over this very route, isn't
            # rejected by the gate it installs.
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

        # --- route tables (path -> handler-method name) ---
        # Keyed by exact path; method dispatch (do_GET vs do_POST)
        # disambiguates the two '/debug' handlers. Several paths share one
        # method that re-discriminates self.path internally (transport
        # action, system action). The string keys keep the route literals
        # greppable for the client/server contract test
        # (tests/test_control_client.py).
        _GET_ROUTES = {
            "/healthz": "_get_healthz",
            "/volume": "_get_volume",
            "/mic": "_get_mic",
            "/source/state": "_get_source_state",
            "/aec": "_get_aec",
            "/aec/enhanced-aec": "_get_enhanced_aec",
            "/debug": "_get_debug",
            "/state": "_get_state",
            "/measurement": "_get_measurement",
            "/grouping": "_get_grouping",
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
            "/aec/leg": "_post_aec_leg",
            "/aec/profile": "_post_aec_profile",
            "/aec/usb-mic": "_post_aec_usb_mic",
            "/aec/usb-mic-leg": "_post_aec_usb_mic_leg",
            "/aec/threshold": "_post_aec_threshold",
            "/aec/firmware/update": "_post_aec_firmware_update",
            "/aec/enhanced-aec/install": "_post_enhanced_aec_install",
            "/aec/commission": "_post_aec_commission",
            "/debug": "_post_debug",
            "/usb-forensics": "_post_usb_forensics",
            "/system/audio-quality": "_post_system_audio_quality",
            "/system/usb-latency": "_post_system_usb_latency",
            "/measurement/hold": "_post_measurement_hold",
            "/measurement/release": "_post_measurement_release",
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
    heartbeat here ties `WATCHDOG=1` to the loop actually spinning: if the
    accept loop wedges (blocked selector, interpreter deadlock), the bumps
    stop, `jasper.watchdog.Heartbeat`'s progress sentinel goes stale, pats
    stop, and systemd's `WatchdogSec=` revives us with a fresh process.
    Request handlers run on worker threads and intentionally don't gate the
    heartbeat — a slow probe must not look like a dead daemon.

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
        description="HTTP control surface for the JTS speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_CONTROL_HOST", "0.0.0.0"),
        help="bind host (default 0.0.0.0 — LAN-reachable)",
    )
    parser.add_argument(
        "--port", type=int, default=CONTROL_PORT,
    )
    parser.add_argument(
        "--camilla-host",
        default=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--camilla-port", type=int,
        default=int(os.environ.get("JASPER_CAMILLA_PORT", DEFAULT_CAMILLA_PORT)),
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
    # install() holds the jasper logger at DEBUG for the in-RAM ring, keeps
    # the journal at INFO, applies the /system Debug card's toggle, and wires
    # SIGUSR1 -> dump. See jasper/flight_recorder.py.
    from .. import flight_recorder
    flight_recorder.install("control")

    # The live pair-balance trim patches the graph from this process, so its
    # swap duck needs a canonical target to release to.
    from ..volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    # 5 s ring buffer for the /system dashboard; daemon thread.
    from .system_metrics import SystemSampler
    sampler = SystemSampler()
    sampler.start()
    # The ONE resident audio-monitor thread: it composes the AirPlay probes
    # with cheap outputd state and slow route-certification reads.
    from .audio_health import AudioHealthSampler
    from .audio_incidents import IncidentStore
    audio_health_sampler = AudioHealthSampler(
        camilla_host=args.camilla_host,
        camilla_port=args.camilla_port,
        service_probe=sampler.service_states_snapshot,
        incident_store=IncidentStore(),
    )
    audio_health_sampler.start()

    server = build_server(
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
        sampler=sampler,
        audio_health_sampler=audio_health_sampler,
    )
    # Arm the control-token gate before serving. ensure_token()
    # auto-generates the token (0640 group jasper) if absent, so the
    # destructive routes are always gated with no operator action;
    # canonical_page auto-delivers it to the dashboard, invisible to the
    # household. Idempotent — never rotates an existing token. Failure is
    # non-fatal (the gate fail-safes to off) so a transient write error can't
    # keep the recovery surface from starting.
    try:
        control_token.ensure_token()
    except OSError as exc:
        log_event(logger, "control_token.ensure_failed", error=str(exc),
                  level=logging.WARNING)
    # The privileged restart broker: jasper-control is the single mediated
    # systemctl boundary. jasper-web's wizard restarts, jasper-mux's librespot
    # recovery, and the room-correction renderer pause ask it to run an
    # allowlisted, closed-vocabulary restart over a SO_PEERCRED'd UNIX socket,
    # so those daemons need no privilege of their own. Bind failure is
    # non-fatal (logged): callers fall back to their fail-soft "restart didn't
    # happen, logged" behaviour.
    restart_broker_server = restart_broker.start_broker()
    # Multi-device peering daemon. No-op (no thread, no asyncio loop, no
    # zeroconf import) when /var/lib/jasper/peering.env has JASPER_PEERING=off
    # — the default. The /rooms/ Speakers page writes that env file and
    # restarts jasper-control to pick up the new mode.
    start_peering_daemon_if_enabled()
    # Protocol-level liveness probe so a wedged shairport-sync AP2 control
    # plane recovers without manual intervention. Off via
    # JASPER_SHAIRPORT_SUPERVISOR=disabled in /etc/jasper/jasper.env.
    shairport_supervisor.start_supervisor()
    # Userspace-liveness supervisor for the case where PID 1 still pats the
    # kernel watchdog but userspace is dead. Probes the sshd banner, our own
    # HTTP /healthz, and /proc/loadavg; clean `systemctl reboot` after 3
    # consecutive failures, rate-limited to 1 reboot per 24 hours.
    # Off via JASPER_SYSTEM_SUPERVISOR=disabled.
    system_supervisor.start_supervisor()
    # Bonded-member runtime liveness between grouping reconciles: sustained
    # dac_content starvation kicks the reconciler (rate-limited), and the
    # leader's snapcast group→stream bindings are read-repaired every poll.
    # Costs one grouping.env read per 30 s when solo. Off via
    # JASPER_GROUPING_SUPERVISOR=disabled.
    grouping_supervisor.start_supervisor()
    # Multiroom cascade timeline: scans structured journal events into a small
    # /state ring so restart chains are reconstructable without fetching raw
    # logs first. Solo-gated (no journalctl scan when no bond is configured)
    # and off via JASPER_MULTIROOM_CASCADE_TIMELINE=disabled.
    from ..multiroom import cascade_timeline
    cascade_timeline.start_sampler()
    # Runtime debug toggle: clear an expired session left on disk, or re-arm
    # the auto-quiet timer if a debug session is still active across this
    # restart. See jasper/control/debug_control.py.
    debug_control.reconcile_on_startup()
    logger.info(
        "jasper-control listening on http://%s:%d "
        "(camilla=%s:%d, voice=%s)",
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
    )
    # systemd watchdog (Type=notify + WatchdogSec in the unit). READY=1 goes
    # out here; serve_forever()'s poll loop bumps the progress sentinel via
    # ControlHTTPServer.service_actions, so a wedged accept loop stops the
    # WATCHDOG=1 pats and systemd restarts us. No-ops outside systemd
    # (NOTIFY_SOCKET unset). See jasper/watchdog.py.
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
        # None when the broker failed to bind (non-fatal, logged above).
        if restart_broker_server is not None:
            restart_broker_server.shutdown()
            restart_broker_server.server_close()
        heartbeat.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
