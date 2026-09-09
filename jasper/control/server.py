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
import logging
import os
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from jasper.log_event import log_event
from ..logging_setup import configure_logging

if TYPE_CHECKING:
    from ..volume_coordinator import VolumeState

from ..camilla_config_contract import DEFAULT_CAMILLA_PORT
from ..identity.identity_state import management_read_allowed, mutating_request_allowed
from ..platform.control_client import CONTROL_PORT
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
from ..music_sources import MUSIC_SOURCE_SPECS
from ..local_sources import local_source_audio_refresh_units
from ..active_speaker.setup_status import read_active_speaker_setup_status
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
from ..volume_curve import percent_to_db
from .single_flight import SingleFlightTTLCache
from ..platform.uds import (
    local_status_json as _local_status_json,
    mux_socket_command as _mux_socket_command,
    voice_socket_command as _voice_socket_command,
)

logger = logging.getLogger(__name__)
SOURCE_SELECT_IDS = {spec.id.value for spec in MUSIC_SOURCE_SPECS}
CORE_AUDIO_RESTART_UNITS = ["jasper-camilla.service"]
LOCAL_SOURCE_AUDIO_REFRESH_UNITS = list(local_source_audio_refresh_units())
_USB_MIC_APPLY_UNIT = "jasper-usbmic-apply.service"
_AEC_BRIDGE_UNIT = "jasper-aec-bridge.service"
_USB_MIC_LEG_APPLY_COALESCE_SECONDS = 5.0
_usb_mic_leg_apply_lock = threading.Lock()
_usb_mic_leg_apply_pending: tuple[str, float] | None = None
# Serializes POST /aec/commission's check-then-start across
# ThreadingHTTPServer workers, so two clicks cannot both pass the is-active
# probe before either start lands.
_aec_commission_start_lock = threading.Lock()


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
# stops voice/AEC for minutes and plays audible measurement sweeps;
# system/audio-quality re-renders asound.conf and restarts every renderer;
# system/usb-latency reconciles the CamillaDSP coupling under the DSP-writer
# lock. Both interrupt playback for every listener, so they are mutations, not
# tuning knobs.
_TOKEN_GATED_ROUTES = frozenset({
    "/system/poweroff",
    "/system/reboot",
    "/system/restart/voice",
    "/system/restart/audio",
    "/system/audio-quality",
    "/system/usb-latency",
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
# Listen socket refused (address in use, unreachable bind host, privileged
# port). Listed in jasper-control.service's SuccessExitStatus +
# RestartPreventExitStatus so the daemon parks instead of climbing
# StartLimitBurst into StartLimitAction=reboot. See ADR-0251.
CONTROL_BIND_FAILED_EXIT = os.EX_CONFIG
CONTROL_MAX_WORKERS = 8
CONTROL_REQUEST_QUEUE_SIZE = 16
CONTROL_REQUEST_TIMEOUT_SEC = 5.0
CONTROL_OVERLOAD_LOG_INTERVAL_SEC = 5.0
STATE_RESPONSE_CACHE_TTL_SEC = 1.0
# How long a /state caller waits on someone else's in-flight aggregate before
# it is served the last value instead. Admission is non-blocking and there are
# eight request workers, so a longer wait starves every other route on one slow
# compute. Retire if the cache stops bounding waiters, or the request pool
# stops being bounded.
STATE_RESPONSE_WAIT_SEC = 2.0


_read_volume_state = _volume_ops.read_volume_state

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


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    airplay_playing_snapshot: Callable[[], bool | None] | None = None,
    audio_health_snapshot: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    return await _state_aggregate._get_state(
        airplay_playing_snapshot=airplay_playing_snapshot,
        audio_health_snapshot=audio_health_snapshot,
        camilla_host=camilla_host,
        camilla_port=camilla_port,
        voice_socket_path=voice_socket_path,
        voice_socket_command=_voice_socket_command,
        mux_socket_command=_mux_socket_command,
        local_status_json=_local_status_json,
    )


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
        spotify_router_factory=_volume_ops._build_spotify_router_or_none,
    )


def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
    audio_health_sampler: Any = None,
    ha_status_cache: Any = None,
) -> type[BaseHTTPRequestHandler]:

    # Route-body imports stay factory-local so importing this module stays
    # cheap: the concern mixins arrive only when a concrete server is built.
    from .handlers import (
        AecRoutes,
        GroupingRoutes,
        MeasurementRoutes,
        PeeringRoutes,
        SystemRoutes,
        VoiceRoutes,
        VolumeRoutes,
    )

    # One probe instance per handler — stateless (it only closes over
    # voice_socket_path), so all mutating volume ops share it. Read-only
    # `_get_op` bypasses coordinator/actuator construction.
    duck_active_probe = _make_duck_active_probe(voice_socket_path)
    state_response_cache = SingleFlightTTLCache(
        STATE_RESPONSE_CACHE_TTL_SEC, STATE_RESPONSE_WAIT_SEC,
    )
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

    def _get_op() -> VolumeState:
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
        PeeringRoutes,
        SystemRoutes,
    ):
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

        def log_request(  # noqa: A003
            self, code: int | str = "-", size: int | str = "-",
        ) -> None:
            # The supervisor polls its own /healthz every few seconds
            # (system_supervisor.py); a 200 there is a liveness no-op, not
            # an event, and was ~45% of this daemon's idle journal volume.
            # /system/snapshot gets the same treatment: the dashboard polls
            # it every 5s per open tab (main.js POLL_MS), pure read, no
            # state change. Every other response, and any non-200 on
            # either path, still logs.
            if code == 200 and self.path in ("/healthz", "/system/snapshot"):
                return
            super().log_request(code, size)

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
                ok, reason = management_read_allowed(
                    {"Host": self.headers.get("Host") or ""},
                )
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
                "db": round(percent_to_db(percent), 3),
                "percent": percent,
                "muted": bool(state.muted),
                "restore_percent": state.restore_percent,
            }

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
        # (tests/test_platform_control_client.py).
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
    audio_health_sampler: Any = None,
) -> ControlHTTPServer:
    return ControlHTTPServer(
        (host, port),
        _make_handler(
            camilla_host,
            camilla_port,
            voice_socket_path,
            sampler,
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

    configure_logging()
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
        system_probe=sampler.pressure_snapshot,
        incident_store=IncidentStore(),
    )
    audio_health_sampler.start()

    try:
        server = build_server(
            args.host, args.port,
            args.camilla_host, args.camilla_port,
            args.voice_socket,
            sampler=sampler,
            audio_health_sampler=audio_health_sampler,
        )
    except OSError as exc:
        # A refused listen socket does not heal on a restart, so park rather
        # than spend the burst that ends in StartLimitAction=reboot.
        log_event(
            logger, "control.bind_failed",
            level=logging.ERROR,
            host=args.host, port=args.port,
            errno=exc.errno, error=exc.strerror or str(exc),
        )
        return CONTROL_BIND_FAILED_EXIT
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
    # Multi-device peering daemon. The coroutine always starts; it reads
    # /var/lib/jasper/peering.env and returns immediately (no multicast
    # socket) when JASPER_PEERING=off — the default. The /sound/pair/
    # Speakers page writes that env file and restarts jasper-control to
    # pick up the new mode.
    from .handlers.peering import start_peering_daemon_if_enabled, stop_peering_daemon

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
    # Runtime debug toggle: clear an expired session left on disk, or re-arm
    # the auto-quiet timer if a debug session is still active across this
    # restart. See jasper/control/debug_control.py.
    debug_control.reconcile_on_startup()
    # systemd watchdog (Type=notify + WatchdogSec in the unit). READY=1 goes
    # out here; serve_forever()'s poll loop bumps the progress sentinel via
    # ControlHTTPServer.service_actions, so a wedged accept loop stops the
    # WATCHDOG=1 pats and systemd restarts us. No-ops outside systemd
    # (NOTIFY_SOCKET unset). See jasper/watchdog.py.
    from ..watchdog import Heartbeat
    heartbeat = Heartbeat()
    server.heartbeat = heartbeat
    heartbeat.start()
    log_event(
        logger,
        "control.ready",
        host=args.host,
        port=args.port,
        camilla_host=args.camilla_host,
        camilla_port=args.camilla_port,
        voice_socket=args.voice_socket,
    )
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
