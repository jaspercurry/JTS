# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS measurement daemon behind the /sound/ measurement pages.

The user opens the hub on a phone and chooses the measurement job:
room correction, active-crossover acoustic checks, or bass tuning. Room
correction captures pre-sweep room noise plus one or more measurement
positions, reviews confidence/visualization evidence, and optionally
applies a bounded room-correction profile through the shared CamillaDSP
apply path.

Architecture:
  - stdlib `ThreadingHTTPServer` — same pattern as voice_setup,
    spotify_setup, bluetooth_setup. No FastAPI / ASGI dependency.
  - Single in-memory `MeasurementSession` (jasper.correction.session)
    drives the multi-step state machine.
  - Browser polls GET /status every 500 ms while work is active, the
    presentation envelope every 900 ms on active screens, and lightweight
    entry facts every 10 s while idle — simpler than SSE in stdlib and bounded
    for state transitions that take seconds.
  - Background asyncio loop in a daemon thread bridges the sync HTTP
    handlers to the async session methods.
  - HTTP routes (after nginx strips the /sound/room/ prefix): this
    module now serves far more routes than fit a comment table.

Module layout: this file owns the page render, the `_GET_ROUTES` /
`_POST_ROUTES` tables and the request handler that dispatches them.
The route bodies live in `correction_handlers`, the session /
capture / microphone state both of them act on lives in
`correction_capture`, and the loop bridge, CamillaController factory,
JSON body reader and request exceptions all three use live in
`correction_runtime`.

Why a separate service from jasper-web (Spotify + voice settings):
the correction flow eventually imports numpy/scipy through
`jasper.correction.*` while handling measurements. Keeping this
socket-activated service separate from lightweight setup pages keeps
the idle management UI cheap on a 1 GB Pi.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


from ..log_event import log_event
from . import correction_room_flow
from ..platform.systemd import no_hold

from ._common import (
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    guard_mutating_request,
    guard_read_request,
    reject_csrf,
    route_path,
    send_html_response,
    send_json_response,
)
from . import correction_capture, correction_handlers, correction_runtime
from .correction_capture import (
    REQUIRED_SAMPLE_RATE,
    _FOLLOWER_DELEGATED_PAGE_PATHS,
)
from .correction_runtime import (
    BadRequest,
    CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S,
    MAX_SYNC_WAV_BODY_BYTES,
    RequestConflict,
    logger,
)


_PAGE_BODY = correction_room_flow._PAGE_BODY


def _render_follower_page(hostname: str, csrf_token: str = "") -> bytes:
    return correction_room_flow.render_follower_page(
        hostname,
        csrf_token,
        leader_url=bonded_follower_leader_web_url("/sound/room/"),
    )


def _render_page(hostname: str, csrf_token: str = "", flash: str = "") -> bytes:
    if bonded_follower_active():
        return _render_follower_page(hostname, csrf_token)
    return correction_room_flow.render_page(
        hostname,
        csrf_token,
        required_sample_rate=REQUIRED_SAMPLE_RATE,
        household_mic_prefill_payload=correction_capture._household_mic_prefill_payload(),
    )


class _Handler(BaseHTTPRequestHandler):
    #: Bound per server by :func:`_make_handler_class`.
    hostname: str
    idle_hold: Callable[[str], AbstractContextManager[Any]]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(
        self, payload: dict[str, Any], *, status: int = 200,
    ) -> None:
        send_json_response(self, payload, status=status)

    def _serve_json_route(
        self, label: str,
        handler_fn: Callable[
            [BaseHTTPRequestHandler],
            dict[str, Any] | tuple[dict[str, Any], int],
        ],
    ) -> None:
        """Shared JSON GET-route wrapper: any handler failure surfaces
        as a 500 JSON error instead of a stack-trace page or a dead
        request thread — the poll posture /status, /envelope, and
        /sessions share (one wrapper so the blanket net isn't
        re-declared per route)."""
        try:
            result = handler_fn(self)
            payload, status = result if isinstance(result, tuple) else (result, 200)
            self._send_json(payload, status=int(status))
        except Exception as e:  # noqa: BLE001 — route-level 500 net
            logger.exception("%s failed", label)
            self._send_json({"error": str(e)}, status=500)

    def _send_html(self, body: bytes, *, status: int = 200) -> None:
        send_html_response(self, body, status=status)

    def _send_text(self, text: str, *, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_client_error(
        self, message: str, *, status: int = 400,
    ) -> None:
        self._send_json({"error": message}, status=status)

    def _send_room_failure(
        self,
        failure: Mapping[str, Any],
        *,
        diagnostic: str,
        status: int,
    ) -> None:
        public = dict(failure)
        log_event(
            logger,
            "correction.homeowner_failure",
            code=str(public.get("code") or "unknown_failure"),
            retryable=bool(public.get("retryable")),
            status=int(status),
            diagnostic=diagnostic,
            level=logging.WARNING,
        )
        self._send_json(
            {"failure": public},
            status=status,
        )

    # --- routes ---

    def do_GET(self) -> None:  # noqa: N802
        path = route_path(self.path)
        handler_fn = _GET_ROUTES.get(path)
        if handler_fn is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not guard_read_request(self):
            return
        if bonded_follower_active() and path in _FOLLOWER_DELEGATED_PAGE_PATHS:
            ctx = begin_request(self)
            self._send_html(_render_follower_page(
                self.hostname, ctx["csrf_token"],
            ))
            return
        handler_fn(self)

    def do_POST(self) -> None:  # noqa: N802
        path = route_path(self.path)
        handler_fn = _POST_ROUTES.get(path)
        if handler_fn is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not guard_mutating_request(self):
            reject_csrf(self)
            return
        if bonded_follower_active() and not path.startswith("/crossover/"):
            log_event(
                logger,
                "correction.follower_content_dsp_blocked",
                path=path,
            )
            self._send_json(
                {
                    "error": (
                        "room correction is controlled on the pair "
                        "leader while this speaker is a follower"
                    ),
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        # The prefix families answer their own failures, so they run
        # outside the blanket 500 net below.
        if path.startswith(("/sync/", "/crossover/")):
            handler_fn(self)
            return
        try:
            handler_fn(self)
        except BadRequest as e:
            self._send_client_error(str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("POST %s failed", path)
            self._send_json({"error": str(e)}, status=500)





















# ---------------------------------------------------------------------------
# Route bodies + routing
# ---------------------------------------------------------------------------
#
# The bodies are module-level functions taking the handler, not methods:
# the tables below hold them directly, so a body bound to the class would
# be dispatched past any override on the per-server subclass
# `_make_handler_class` builds.

def _dispatch_sync(handler: _Handler) -> None:
    """POST /sync/* — stereo-pair acoustic timing walkthrough."""
    from . import sync_flow

    path = route_path(handler.path)

    def _schedule(coro):
        return asyncio.run_coroutine_threadsafe(
            coro, correction_runtime.ensure_loop())

    try:
        if path == "/sync/start":
            blocked = correction_capture._correction_start_blocker()
            if blocked is not None:
                handler._send_json(
                    {"ok": False, "error": (
                        "a room-correction session is active "
                        f"({blocked})"
                    )},
                    status=HTTPStatus.CONFLICT,
                )
                return
            payload, status = sync_flow.handle_start(
                handler.hostname, _schedule)
        elif path == "/sync/play":
            payload, status = sync_flow.handle_play(
                correction_runtime.run_async, _schedule)
        elif path == "/sync/analyze":
            try:
                body = correction_runtime.read_wav_body(
                    handler,
                    max_bytes=MAX_SYNC_WAV_BODY_BYTES,
                )
            except BadRequest as e:
                handler._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            payload, status = sync_flow.handle_analyze(body)
        elif path == "/sync/apply":
            payload, status = sync_flow.handle_apply(handler)
        else:
            payload, status = sync_flow.handle_stop()
        handler._send_json(payload, status=int(status))
    except Exception as e:  # noqa: BLE001
        logger.exception("%s failed", path)
        handler._send_json({"ok": False, "error": str(e)}, status=500)


def _dispatch_crossover(handler: _Handler) -> None:
    """POST /crossover/* — secure active-crossover measurement."""
    path = route_path(handler.path)

    if path in {"/crossover/v2/session", "/crossover/v2/verify"}:
        # v2 commission sessions (Wave 5a). ValueError covers both the
        # host's typed CrossoverV2Refused (a subclass) and shared
        # precondition refusals — same contract as the capture routes.
        try:
            handler._send_json(
                correction_handlers._handle_crossover_v2_capture(
                    handler,
                    verify_only=(path == "/crossover/v2/verify"),
                    idle_hold=handler.idle_hold,
                )
            )
        except ValueError as e:
            # Log the refusal so it is debuggable from the journal,
            # not just visible as a 400 in the browser. A session-open
            # refusal never reaches the envelope, because the envelope
            # renders from a PERSISTED failure and the pre-flight
            # deliberately refuses before any state is written. So the
            # reason's own action rides the 400 body instead — the
            # wizard renders it as a button beside the message, and the
            # household is one click from the fix rather than one
            # navigation plus one click. Same registry entry the
            # hard-stop screen would have read.
            from jasper.web.correction_crossover_v2 import (
                refusal_next_action,
            )

            refusal_body: dict[str, Any] = {"ok": False, "error": str(e)}
            action = refusal_next_action(e)
            if action is not None:
                refusal_body["next_action"] = action
            log_event(
                logger,
                "correction.crossover_v2_refused",
                level=logging.WARNING,
                route=path,
                reason=str(e),
                code=str(getattr(e, "code", "") or ""),
            )
            handler._send_json(
                refusal_body,
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            # Issue #1833: a CrossoverV2FlowError raised SYNCHRONOUSLY
            # inside prepare_v2_session's `_open` (the spec/index-map
            # builders) reaches here, not the 400 arm above -- it is a
            # RuntimeError subclass, so `except ValueError` misses it.
            # `str(e)` then put a programmer string
            # ("cloud_measure_positions must be 6..12, got 14") straight
            # into the wizard's DOM. Route it through the ONE mapper the
            # rest of this module already uses; it is the identity for
            # everything outside the mapped families, so nothing else on
            # this arm changes. The raw string still reaches the journal
            # via logger.exception above.
            logger.exception("%s failed", path)
            handler._send_json(
                {"ok": False, "error": correction_capture._capture_failure_message(e)},
                status=500,
            )
        return

    if path == "/crossover/v2/position-ready":
        # A release that names the wrong (or no) pending capture is a
        # CONFLICT, not a malformed request: the driver's view of the
        # session is simply stale, which is the ordinary outcome of a
        # retry that crossed a capture starting — so it answers 409,
        # the same status a refused transition maps to elsewhere here.
        try:
            handler._send_json(correction_handlers._handle_crossover_v2_position_ready(handler))
        except BadRequest as e:
            # A malformed body is a 400, and BadRequest subclasses
            # ValueError — so it has to be claimed BEFORE the 409 arm
            # below, which would otherwise report a parse failure as a
            # stale-release conflict.
            #
            # It must also be ANSWERED here, not re-raised: this arm
            # sits above ``_dispatch_crossover``'s own
            # ``except BadRequest`` (that one guards the later routes
            # inside its own ``try``), and ``do_POST`` calls this
            # dispatcher bare — so a re-raise escapes into
            # ``socketserver.BaseServer.handle_error``, which logs a
            # traceback and drops the connection with NO response at
            # all. The driver sees a closed socket instead of the
            # reason its body was rejected.
            handler._send_client_error(str(e))
        except ValueError as e:
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    if path == "/crossover/v2/complete":
        # Same shape as position-ready: a malformed body is a 400
        # (BadRequest subclasses ValueError, so it must be claimed
        # first), while a signal with no wired session waiting is a
        # CONFLICT — a stale caller, not a malformed request.
        try:
            handler._send_json(correction_handlers._handle_crossover_v2_complete(handler))
        except BadRequest as e:
            handler._send_client_error(str(e))
        except ValueError as e:
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    if path == "/crossover/v2/retake":
        # The completion signal's shape exactly, and for the same
        # reasons: 400 for a malformed body, 409 for a signal no wired
        # session is waiting for.
        try:
            handler._send_json(correction_handlers._handle_crossover_v2_retake(handler))
        except BadRequest as e:
            handler._send_client_error(str(e))
        except ValueError as e:
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    if path == "/crossover/v2/apply":
        try:
            payload = correction_handlers._handle_crossover_v2_apply(handler)
            # Finding N: a blocked apply must not read as success — the
            # same "compute status from payload contents" shape
            # the capture routes already use above.
            handler._send_json(
                payload,
                status=(
                    HTTPStatus.CONFLICT
                    if payload.get("status") == "blocked"
                    else HTTPStatus.OK
                ),
            )
        except ValueError as e:
            # This arm answered 400 with the raw string and journaled
            # NOTHING, which the session/verify arm above had already
            # ruled a defect ("the 400 response is correct for the
            # browser; the gap was purely observability" --
            # test_crossover_v2_refusal_is_logged_not_silent). Every
            # ValueError leaving here is now recorded; what differs is
            # the SEVERITY, because the two halves are different events.
            #
            # A refusal (CrossoverV2Refused) or a malformed body
            # (BadRequest) is the caller being told no -- WARNING, under
            # the vocabulary the sibling already owns for "a v2 route
            # refused", which likewise exempts neither. Anything else is
            # the speaker faulting on its own apply path, which #2839's
            # `allow_nan=False` refusal in `save_v2_state` made
            # reachable -- ERROR, and named for what it is.
            from jasper.web.correction_crossover_v2 import (
                CrossoverV2Refused,
            )
            if isinstance(e, (BadRequest, CrossoverV2Refused)):
                log_event(
                    logger,
                    "correction.crossover_v2_refused",
                    level=logging.WARNING,
                    route=path,
                    reason=str(e),
                    code=str(getattr(e, "code", "") or ""),
                )
            else:
                log_event(
                    logger,
                    "correction.crossover_v2_apply_fault",
                    level=logging.ERROR,
                    error_type=type(e).__name__,
                    error=str(e),
                )
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    if path == "/crossover/v2/republish":
        try:
            # No payload-derived status: every refusal is a
            # CrossoverV2Refused (a ValueError -> 400 below), and a
            # success only moves a pointer, so there is no third
            # "blocked" outcome to classify like apply/restore have.
            handler._send_json(correction_handlers._handle_crossover_v2_republish(handler))
        except ValueError as e:
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    if path == "/crossover/v2/decline":
        try:
            payload, status = correction_handlers._handle_crossover_v2_decline(handler)
            handler._send_json(payload, status=int(status))
        except ValueError as e:
            handler._send_json(
                {"ok": False, "error": str(e)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json({"ok": False, "error": str(e)}, status=500)
        return

    from . import correction_crossover_backend as crossover_backend

    volume_sensitive_routes = {
        "/crossover/reset",
    }
    lease = crossover_backend.level_lease()
    if (
        path in volume_sensitive_routes
        and lease.unresolved_volume_safety is not None
    ):
        handler._send_json(
            correction_capture._crossover_volume_safety_refusal(),
            status=HTTPStatus.CONFLICT,
        )
        return

    try:
        if path == "/crossover/recover-volume":
            from jasper.camilla import CamillaUnavailable

            # When the v2 session owns the unresolved (or
            # crash-hydrated active) session volume, route to its
            # plan's recover_unresolved — the legacy lease holds no
            # unresolved state for a v2 session, so routing there
            # instead would 409 crossover_volume_recovery_not_required
            # and leave the volume_recovery screen's own button dead.
            from . import correction_crossover_v2 as v2host

            if v2host.v2_volume_recovery_active():
                succeeded, recovery = v2host.recover_session_volume(
                    correction_runtime.run_async, correction_runtime.camilla_controller
                )
                # A deferral is not a failure to recover, so it must
                # not send the household after CamillaDSP: a live
                # measurement session holds the fader and the restore
                # lands when that session finishes.
                if succeeded:
                    next_step = (
                        "Refresh and continue crossover commissioning."
                    )
                elif recovery == v2host.RECOVERY_DEFERRED:
                    next_step = (
                        "A measurement session still holds the volume. "
                        "It is restored when that session finishes."
                    )
                else:
                    next_step = (
                        "Stop playback and retry recovery when "
                        "CamillaDSP is available."
                    )
                handler._send_json(
                    {
                        "status": "recovered" if succeeded else "refused",
                        "recovery": recovery,
                        "next_step": next_step,
                    },
                    status=(
                        HTTPStatus.OK if succeeded else HTTPStatus.CONFLICT
                    ),
                )
                return

            if lease.unresolved_volume_safety is None:
                handler._send_json(
                    {
                        "status": "refused",
                        "reason": "crossover_volume_recovery_not_required",
                        "next_step": "Refresh the crossover page.",
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            cam = correction_runtime.camilla_controller()
            from jasper.volume_owner import volume_owner

            recovery_owner = volume_owner()
            if recovery_owner is None:
                log_event(
                    logger,
                    "correction.crossover_level_volume_recovery_owner_absent",
                    level=logging.CRITICAL,
                )
                handler._send_json(
                    {
                        "status": "refused",
                        "reason": "crossover_volume_recovery_unavailable",
                        "next_step": "Restart the speaker, then retry.",
                    },
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return

            # Routed: the recovery DECLARES the household level rather
            # than writing the fader itself. The lease keeps its
            # exact-then-emergency ladder and still proves each rung
            # through its own readback below, which is what makes a
            # declaration that was merely RECORDED under a higher-ranked
            # claim read as "not yet safe" instead of clearing the
            # durable intent early.
            async def _set_recovery_volume(db: float) -> bool:
                return await recovery_owner.declare_household_level_db(db)

            async def _get_recovery_volume() -> float:
                try:
                    value = await cam.get_volume_db(best_effort=False)
                except CamillaUnavailable as exc:
                    raise RuntimeError(
                        "CamillaDSP is unavailable during volume recovery"
                    ) from exc
                if value is None:
                    raise RuntimeError(
                        "CamillaDSP did not report the recovered volume"
                    )
                return float(value)

            try:
                recovery = correction_runtime.run_async(
                    lease.recover_unresolved_volume_safety(
                        _set_recovery_volume,
                        _get_recovery_volume,
                    ),
                    timeout=CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S,
                )
            except concurrent.futures.TimeoutError:
                log_event(
                    logger,
                    "correction.crossover_level_volume_safety_recovery_timeout",
                    level=logging.ERROR,
                    timeout_s=CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S,
                )
                recovery = (
                    crossover_backend.UnresolvedVolumeRecoveryResult.FAILED
                )
            succeeded = recovery is not (
                crossover_backend.UnresolvedVolumeRecoveryResult.FAILED
            )
            handler._send_json(
                {
                    "status": "recovered" if succeeded else "refused",
                    "recovery": recovery.value,
                    "next_step": (
                        "Refresh and continue crossover commissioning."
                        if succeeded
                        else "Stop playback and retry recovery when CamillaDSP is available."
                    ),
                },
                status=(HTTPStatus.OK if succeeded else HTTPStatus.CONFLICT),
            )
            return

        if path == "/crossover/capture-cancel":
            handler._send_json(correction_handlers._handle_crossover_capture_cancel())
            return

        if path == "/crossover/reset":
            payload, status = correction_handlers._handle_crossover_reset()
            handler._send_json(payload, status=int(status))
            return

        raise ValueError(f"unknown crossover route: {path}")
    except BadRequest as e:
        handler._send_json(
            {"ok": False, "error": str(e)},
            status=HTTPStatus.BAD_REQUEST,
        )
    except ValueError as e:
        handler._send_json(
            {"ok": False, "error": str(e)},
            status=HTTPStatus.BAD_REQUEST,
        )
    except (OSError, RuntimeError, TypeError) as e:
        logger.exception("%s failed", path)
        handler._send_json({"ok": False, "error": str(e)}, status=500)


def _get_index(handler: _Handler) -> None:
    ctx = begin_request(handler)
    handler._send_html(_render_page(
        handler.hostname, ctx["csrf_token"], ctx["flash"],
    ))


def _get_crossover(handler: _Handler) -> None:
    from . import correction_crossover_flow
    ctx = begin_request(handler)
    handler._send_html(
        correction_crossover_flow.render_page(
            handler.hostname, ctx["csrf_token"],
        )
    )


def _get_measurements(handler: _Handler) -> None:
    from . import correction_measurements
    ctx = begin_request(handler)
    handler._send_html(
        correction_measurements.render_page(
            handler.hostname, ctx["csrf_token"],
        )
    )


def _get_measurements_data(handler: _Handler) -> None:
    from jasper.active_speaker import bundles as active_bundles
    from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT
    from . import correction_measurements

    query = parse_qs(urlparse(handler.path).query)
    run_a_id = (query.get("a") or [""])[0] or None
    run_b_id = (query.get("b") or [""])[0] or None
    try:
        handler._send_json(correction_measurements.build_data(
            sessions_dir=active_bundles.sessions_dir(),
            campaign_root=DEFAULT_CAMPAIGN_ROOT,
            run_a_id=run_a_id,
            run_b_id=run_b_id,
        ))
    except correction_measurements.MeasurementViewRequestError as exc:
        handler._send_client_error(str(exc))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.exception("/measurements/data failed")
        handler._send_json({"error": str(exc)}, status=500)


def _get_crossover_status(handler: _Handler) -> None:
    from . import correction_crossover_flow
    from . import correction_crossover_v2 as v2host

    def _crossover_status(_handler):
        # W6.1 E3: lazy wall-clock-ceiling enforcement on read —
        # a session volume that outlived its 1800 s ceiling is
        # force-drained here (cheap in-memory stale check first).
        correction_capture._enforce_session_volume_ceiling(v2host)
        return correction_crossover_flow.handle_status(
            capture=correction_capture._get_capture_slot_for("crossover_v2:"),
        )

    handler._serve_json_route("/crossover/status", _crossover_status)


def _get_crossover_envelope(handler: _Handler) -> None:
    from . import correction_crossover_flow
    from . import correction_crossover_v2 as v2host

    def _crossover_envelope(_handler):
        # W6.1 E3: the wizard and remote driver both poll this route,
        # so it promptly drains a walked-away or slow-driver session.
        correction_capture._enforce_session_volume_ceiling(v2host)
        return correction_crossover_flow.handle_envelope(
            capture=correction_capture._get_capture_slot_for("crossover_v2:"),
        )

    handler._serve_json_route("/crossover/envelope", _crossover_envelope)


def _get_bass(handler: _Handler) -> None:
    from . import correction_bass_flow
    ctx = begin_request(handler)
    handler._send_html(
        correction_bass_flow.render_page(
            handler.hostname, ctx["csrf_token"],
        )
    )


def _get_bass_status(handler: _Handler) -> None:
    from . import correction_bass_flow
    handler._serve_json_route(
        "/bass/status",
        lambda _handler: correction_bass_flow.handle_status(),
    )


def _get_sync(handler: _Handler) -> None:
    from . import sync_flow
    ctx = begin_request(handler)
    handler._send_html(sync_flow.render_page(ctx["csrf_token"]))


def _get_sync_status(handler: _Handler) -> None:
    from . import sync_flow
    try:
        handler._send_json(sync_flow.handle_status())
    except Exception as e:  # noqa: BLE001
        logger.exception("/sync/status failed")
        handler._send_json({"error": str(e)}, status=500)


def _get_healthz(handler: _Handler) -> None:
    body = b"ok\n"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_status(handler: _Handler) -> None:
    handler._serve_json_route("/status", correction_handlers._handle_status)


def _get_entry_status(handler: _Handler) -> None:
    handler._serve_json_route("/entry-status", correction_handlers._handle_entry_status)


def _get_envelope(handler: _Handler) -> None:
    handler._serve_json_route("/envelope", correction_handlers._handle_envelope)


def _get_sessions(handler: _Handler) -> None:
    handler._serve_json_route("/sessions", correction_handlers._handle_sessions)


def _get_session_report(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_session_report(handler))
    except BadRequest as e:
        handler._send_client_error(str(e))
    except FileNotFoundError as e:
        handler._send_client_error(str(e), status=404)
    except Exception as e:  # noqa: BLE001
        from jasper.correction.bundles import BundleError
        if isinstance(e, BundleError):
            handler._send_client_error(str(e), status=422)
            return
        logger.exception("/session-report failed")
        handler._send_json({"error": str(e)}, status=500)


def _get_calibration_models(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_calibration_models(handler))
    except Exception as e:  # noqa: BLE001
        logger.exception("/calibration/models failed")
        handler._send_json({"error": str(e)}, status=500)


def _post_start(handler: _Handler) -> None:
    from jasper.correction import failures
    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
    )
    from jasper.sound.graph_carrier import CarrierCannotHostEq
    try:
        handler._send_json(correction_handlers._handle_start(handler))
    except (CorrectionRuntimeSafetyError, CarrierCannotHostEq) as e:
        handler._send_room_failure(
            failures.public_failure(
                failures.SPEAKER_MEASUREMENT_UNSAFE,
                # The reachable cause of this refusal is now an
                # unready speaker, so send the household where
                # they can act on it rather than to a retry
                # that will refuse again.
                recovery_action={
                    "label": "Open speaker setup",
                    "href": "/sound/speaker/",
                },
            ),
            diagnostic=str(e),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    except FileNotFoundError as e:
        handler._send_room_failure(
            failures.public_failure(
                failures.MICROPHONE_SETUP_UNAVAILABLE,
            ),
            diagnostic=str(e),
            status=HTTPStatus.BAD_REQUEST,
        )
    except ValueError as e:
        handler._send_room_failure(
            failures.public_failure(
                failures.MEASUREMENT_SETUP_INVALID,
            ),
            diagnostic=str(e),
            status=HTTPStatus.BAD_REQUEST,
        )
    except RequestConflict as e:
        handler._send_room_failure(
            failures.public_failure(
                failures.MEASUREMENT_IN_PROGRESS,
            ),
            diagnostic=str(e),
            status=HTTPStatus.CONFLICT,
        )


def _post_next_position(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_next_position(handler))


def _post_repeat_position(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_repeat_position(handler))


def _post_verify(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_verify(handler))


def _post_test_tone(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_test_tone(handler))


def _post_autolevel_start(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_autolevel_start(handler))
    except RequestConflict as e:
        handler._send_client_error(str(e), status=409)


def _post_autolevel_lock(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_autolevel_lock(handler))


def _post_autolevel_cancel(handler: _Handler) -> None:
    handler._send_json(correction_handlers._handle_autolevel_cancel(handler))


def _post_local_capture_setup(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_local_capture_setup(handler))
    except (FileNotFoundError, ValueError) as e:
        handler._send_client_error(str(e))
    except RequestConflict as e:
        handler._send_client_error(str(e), status=409)


def _post_upload_capture(handler: _Handler) -> None:
    from jasper.audio_measurement import quality

    try:
        handler._send_json(correction_handlers._handle_upload_capture(handler))
    except quality.CaptureQualityError as e:
        sess = correction_capture._get_or_create_session()
        handler._send_json({
            "error": str(e),
            "session_id": sess.session_id,
            "state": sess.state.value,
            "current_position": sess.current_position,
            "total_positions": sess.total_positions,
            "capture_quality": sess.capture_quality,
            "verify_quality": sess.verify_quality,
            "browser_audio_report": getattr(
                sess, "browser_audio_report", None,
            ),
            "runtime_integrity": correction_capture._runtime_integrity_summary(sess),
        }, status=422)
    except ValueError as e:
        handler._send_client_error(str(e))


def _post_upload_noise(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_upload_noise(handler))
    except ValueError as e:
        handler._send_client_error(str(e))
    except RequestConflict as e:
        handler._send_client_error(str(e), status=409)


def _post_calibration_fetch(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_calibration_fetch(handler))
    except ValueError as e:
        handler._send_client_error(str(e))
    except Exception as e:  # noqa: BLE001
        from jasper.audio_measurement.calibration import (
            CalibrationNotFoundError,
            CalibrationUpstreamError,
        )
        if isinstance(e, CalibrationNotFoundError):
            handler._send_client_error(str(e), status=404)
        elif isinstance(e, CalibrationUpstreamError):
            handler._send_client_error(str(e), status=502)
        else:
            raise


def _post_calibration_upload(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_calibration_upload(handler))
    except ValueError as e:
        handler._send_client_error(str(e))


def _post_apply(handler: _Handler) -> None:
    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
    )
    from jasper.sound.graph_carrier import CarrierCannotHostEq
    try:
        handler._send_json(correction_handlers._handle_apply(handler))
    except (CarrierCannotHostEq, CorrectionRuntimeSafetyError) as e:
        handler._send_client_error(
            str(e),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )


def _post_reset(handler: _Handler) -> None:
    # Local import keeps session/numpy off the socket-activated
    # process's import path (mirrors the other handlers).
    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
    )
    from jasper.correction.session import SessionBusyError
    try:
        handler._send_json(correction_handlers._handle_reset(handler))
    except CorrectionRuntimeSafetyError as e:
        handler._send_client_error(
            str(e),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    except SessionBusyError as e:
        # Rejected because a sweep/analysis is mid-flight — a
        # state conflict (409), not a server error (500).
        handler._send_client_error(str(e), status=409)


def _post_session_delete(handler: _Handler) -> None:
    try:
        handler._send_json(correction_handlers._handle_session_delete(handler))
    except BadRequest as e:
        handler._send_client_error(str(e))
    except FileNotFoundError as e:
        handler._send_client_error(str(e), status=404)
    except RequestConflict as e:
        handler._send_client_error(str(e), status=409)


# do_GET / do_POST dispatch through these exact-path tables
# (path -> callable taking the handler). Mirrors the table in
# jasper/web/wake_corpus_setup.py.
#
# ORDERING IS LOAD-BEARING: an unlisted path 404s before the read guard
# (GET) or the CSRF check (POST) runs, so a bogus path never reveals
# either. The /sync/* and /crossover/* families are dispatched by prefix
# through their own functions, which answer their own failures and so run
# outside do_POST's blanket 500 net.

_GET_ROUTES = {
    "/": _get_index,
    "/crossover": _get_crossover,
    "/measurements": _get_measurements,
    "/measurements/data": _get_measurements_data,
    "/crossover/status": _get_crossover_status,
    "/crossover/envelope": _get_crossover_envelope,
    "/bass": _get_bass,
    "/bass/status": _get_bass_status,
    "/sync": _get_sync,
    "/sync/status": _get_sync_status,
    "/healthz": _get_healthz,
    "/status": _get_status,
    "/entry-status": _get_entry_status,
    "/envelope": _get_envelope,
    "/sessions": _get_sessions,
    "/session-report": _get_session_report,
    "/calibration/models": _get_calibration_models,
}

# Mutating routes this handler accepts. Membership gates the 404 above;
# deleting a line would otherwise 404 a route silently.
_POST_ROUTES = {
    "/start": _post_start,
    "/next-position": _post_next_position,
    "/repeat-position": _post_repeat_position,
    "/verify": _post_verify,
    "/test-tone": _post_test_tone,
    "/autolevel/start": _post_autolevel_start,
    "/autolevel/lock": _post_autolevel_lock,
    "/autolevel/cancel": _post_autolevel_cancel,
    "/upload-noise": _post_upload_noise,
    "/upload-capture": _post_upload_capture,
    "/local-capture/setup": _post_local_capture_setup,
    "/calibration/fetch": _post_calibration_fetch,
    "/calibration/upload": _post_calibration_upload,
    "/apply": _post_apply,
    "/reset": _post_reset,
    "/session/delete": _post_session_delete,
    "/crossover/capture-cancel": _dispatch_crossover,
    "/crossover/reset": _dispatch_crossover,
    "/crossover/recover-volume": _dispatch_crossover,
    # v2 session flow — the only crossover-measurement flow. There is no
    # per-driver flow and no JASPER_CROSSOVER_FLOW selector to branch on.
    "/crossover/v2/session": _dispatch_crossover,
    "/crossover/v2/verify": _dispatch_crossover,
    "/crossover/v2/apply": _dispatch_crossover,
    # Make a PREVIOUSLY-MINTED, banked candidate the live published one again,
    # so the apply door above can reach it by fingerprint. The apply slot is
    # single-valued and every measure session overwrites it; this is the lookup
    # it never had.
    "/crossover/v2/republish": _dispatch_crossover,
    # The review screen's "Keep current sound", which #2641 found inert.
    "/crossover/v2/decline": _dispatch_crossover,
    # A GATED session's position release — the report that the microphone has
    # reached the angle the envelope named, from an EXTERNAL driver on the
    # remote tier or from the person holding the tape on a hand-walked wired
    # round (#2879).
    "/crossover/v2/position-ready": _dispatch_crossover,
    # The WIRED session's all-spots-measured confirmation (#2662 W2b) — the
    # local stand-in for the phone's authenticated completion event.
    "/crossover/v2/complete": _dispatch_crossover,
    # The WIRED session's per-take retake — the local stand-in for the phone's
    # ``begin_capture {retake: true}``, re-opening the slot that just
    # completed while the walk is still waiting on a person.
    "/crossover/v2/retake": _dispatch_crossover,
    "/sync/start": _dispatch_sync,
    "/sync/play": _dispatch_sync,
    "/sync/analyze": _dispatch_sync,
    "/sync/apply": _dispatch_sync,
    "/sync/stop": _dispatch_sync,
    "/sync/reset": _dispatch_sync,
}


def _make_handler_class(
    *,
    hostname: str,
    idle_hold: Callable[[str], AbstractContextManager[Any]],
) -> type[_Handler]:
    """Bind the per-server values the routes read off `self`.

    `idle_hold` is wrapped in `staticmethod` because a plain function
    stored as a class attribute binds as a method and would otherwise
    receive the handler as its first argument.
    """

    class _BoundHandler(_Handler):
        pass

    _BoundHandler.hostname = hostname
    _BoundHandler.idle_hold = staticmethod(idle_hold)
    return _BoundHandler


def make_server(
    target,
    *,
    hostname: str = "jts.local",
    idle_hold: Callable[[str], AbstractContextManager[Any]] = no_hold,
) -> ThreadingHTTPServer:
    """Build the wizard server. `target` is socket/tuple/int per
    systemd.make_http_server's contract.

    ``idle_hold`` is ``main``'s ``IdleShutdownTracker.hold`` — the seam that
    lets a route keep the socket-activated process alive across background work
    it starts but does not await. Defaulting to ``systemd.no_hold`` keeps a
    server built without an idle tracker (tests, direct invocation) behaving
    exactly as before."""
    from ..platform import systemd
    return systemd.make_http_server(
        target, _make_handler_class(hostname=hostname, idle_hold=idle_hold),
    )


def _restore_capture_entry() -> None:
    """Converge an abandoned automatic capture sequence back to production.

    An automatic capture sequence leaves the persisted CamillaDSP path on the
    all-muted staged anchor between attempts; the production path is stashed
    durably (capture_entry_anchor). This runs at both in-process lifecycle
    exits — service start (`_claim_crossover_state_owners`, covering a
    previous process that crashed/restarted mid-sequence) and this process's
    own idle shutdown (`main`'s IdleShutdownTracker hook, covering the common
    abandon: the user closes the tab, correction-web idles out minutes later).
    Fail direction if it cannot run (CamillaDSP unreachable): the speaker
    stays on the all-muted anchor — muted, never loud — and the stash is
    retained for the next opportunity.
    """

    from jasper.active_speaker import web_commissioning

    correction_runtime.run_async(
        web_commissioning.restore_pending_capture_entry_config(
            camilla_factory=correction_runtime.camilla_controller,
        ),
        timeout=15.0,
    )


def _idle_exit_restore_capture_entry() -> None:
    """Fail-soft idle-shutdown wrapper for :func:`_restore_capture_entry`."""

    try:
        _restore_capture_entry()
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "correction.capture_entry_restore_unavailable",
            level=logging.WARNING,
            boundary="idle_exit",
            reason=type(exc).__name__,
        )


async def _restore_protected_neutral_program_graph() -> None:
    """Restore an owned temporary graph while its retained anchor still matches."""

    from jasper.active_speaker.camilla_yaml import protected_neutral_program_origin
    from jasper.active_speaker.crossover_v2.composition import confirm_graph_is_live
    from jasper.active_speaker.crossover_v2.session_graph import temporary_graph_anchor
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.dsp_apply import dsp_writer_lock

    cam = correction_runtime.camilla_controller()
    async with dsp_writer_lock(
        DEFAULT_CAMILLA_CONFIG_DIR,
        source="crossover_v2_program_startup_recovery",
    ):
        live = await cam.get_active_config_raw(best_effort=True)
        origin = protected_neutral_program_origin(live)
        scoped_anchor = await temporary_graph_anchor(cam, live) if origin is None else None
        if origin is None and scoped_anchor is None:
            return
        config_path = (
            str(scoped_anchor) if scoped_anchor
            else await cam.get_config_file_path(best_effort=False)
        )
        # "None" is the STRING that reader returns for a null path.
        if not isinstance(config_path, str) or config_path in ("", "None"):
            raise RuntimeError("protected-neutral recovery anchor is unavailable")
        expected = Path(config_path).read_text(encoding="utf-8")
        await cam.set_active_config_raw(expected, best_effort=False)
        await confirm_graph_is_live(cam, expected)
        log_event(
            logger,
            "correction.crossover_v2_program_recovered" if origin is not False
            else "correction.crossover_v2_program_mutated_recovered",
            config_path=config_path,
        )


def _claim_crossover_state_owners() -> None:
    """Retire prior-process Active work before this service accepts requests."""

    from jasper.active_speaker import repeat_admission
    from . import correction_crossover_backend

    claims = (
        (
            "correction.crossover_repeat_admission_unavailable",
            repeat_admission.claim_owner,
        ),
        (
            "correction.active_commissioning_run_unavailable",
            correction_crossover_backend.claim_commissioning_run_owner,
        ),
        (
            "correction.capture_entry_restore_unavailable",
            _restore_capture_entry,
        ),
        (
            "correction.room_startup_recovery_unavailable",
            lambda: correction_runtime.run_async(
                correction_handlers.recover_room_startup_state(
                    correction_capture._get_or_create_session(), correction_runtime.camilla_controller(),
                ), timeout=15.0,
            ),
        ),
    )
    for event, claim in claims:
        try:
            claim()
        except (OSError, RuntimeError, ValueError) as exc:
            log_event(
                logger,
                event,
                level=logging.ERROR,
                reason=type(exc).__name__,
            )
    from jasper.camilla import CamillaUnavailable

    try:
        correction_runtime.run_async(_restore_protected_neutral_program_graph(), timeout=15.0)
    except (OSError, RuntimeError, ValueError, CamillaUnavailable) as exc:
        log_event(
            logger,
            "correction.crossover_v2_program_recovery_failed",
            level=logging.ERROR,
            reason=type(exc).__name__,
        )
        raise


def _configure_logging() -> None:
    """This wizard's own journal bootstrap — unredacted, one of the listed
    gaps in ``tests/test_logging_setup.py``'s ``_ALLOWLIST``: the measurement
    program's files adopt ``configure_logging`` together, not one at a time.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _start(args, tracker) -> dict[str, Any]:
    # Socket Accept=no + one service ExecStart make this the sole lifecycle
    # boundary that may retire unfinished work from a previous process.
    _claim_crossover_state_owners()
    return {"hostname": args.hostname, "idle_hold": tracker.hold}


def main(argv: list[str] | None = None) -> int:
    from . import _wizard_cli
    from jasper.volume_coordinator import install_env_canonical_target_provider

    # Correction and crossover applies swap the live graph from this process,
    # so their swap duck needs a canonical target to release to.
    install_env_canonical_target_provider()

    # The idle exit is exactly the abandoned-sequence moment (user closed the
    # tab, no requests for the threshold AND no work in flight) — the daemon's
    # last in-process chance to converge a capture sequence parked on the
    # all-muted anchor back to production before the process goes away. The
    # hook is bounded (run_async timeout) and exception-guarded by the
    # tracker; on a deferred/failed restore the durable stash survives for the
    # next service-start claim boundary.
    return _wizard_cli.run_wizard_cli(
        "jasper-correction-web",
        "HTTPS measurement daemon for the JTS speaker's /sound/ pages",
        8770,
        argv,
        make_server=make_server,
        extra=lambda parser: parser.add_argument(
            "--hostname",
            default=os.environ.get("JASPER_HOSTNAME", "jts.local"),
            help="speaker hostname used in the cert-download fallback link",
        ),
        start=_start,
        detail=lambda args: f"hostname={args.hostname}",
        configure=_configure_logging,
        on_idle_exit=_idle_exit_restore_capture_entry,
    )


if __name__ == "__main__":
    raise SystemExit(main())
