# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS measurement daemon behind the /sound/ measurement pages.

It serves the active-crossover commissioning walk, the stereo-pair
timing wizard, the read-only measurements browser and the bass display
page.

Architecture:
  - stdlib `ThreadingHTTPServer` — same pattern as voice_setup,
    spotify_setup, bluetooth_setup. No FastAPI / ASGI dependency.
  - Background asyncio loop in a daemon thread bridges the sync HTTP
    handlers to the async measurement methods.
  - Browsers poll the status/envelope routes rather than holding a
    stream — simpler than SSE in stdlib and bounded for state
    transitions that take seconds.

Module layout: this file owns the `_GET_ROUTES` / `_POST_ROUTES` tables
and the request handler that dispatches them. The route bodies live in
`correction_handlers`, the capture / microphone state both of them act
on lives in `correction_capture`, and the loop bridge, CamillaController
factory, body readers and request exceptions all three use live in
`correction_runtime`.

Why a separate service from jasper-web (Spotify + voice settings): the
measurement routes eventually import numpy/scipy while handling
captures. Keeping this socket-activated service separate from
lightweight setup pages keeps the idle management UI cheap on a 1 GB Pi.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


from ..log_event import log_event
from ..platform.systemd import no_hold

from ._common import (
    RouteFn,
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    dispatch_get,
    dispatch_post,
    refusal_envelope,
    route_path,
    send_html_response,
    send_json_response,
)
from . import correction_capture, correction_handlers, correction_runtime, sync_flow
from .correction_runtime import (
    BadRequest,
    MAX_SYNC_WAV_BODY_BYTES,
    logger,
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
            self._send_json(refusal_envelope(e), status=500)

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
        self, exc: BaseException, *, status: int = 400,
    ) -> None:
        self._send_json(refusal_envelope(exc), status=status)

    # --- routes ---

    def do_GET(self) -> None:  # noqa: N802
        dispatch_get(self, _GET_ROUTES)

    def do_POST(self) -> None:  # noqa: N802
        dispatch_post(self, _POST_ROUTES, guard="header", run=_run_post_route)


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
    path = route_path(handler.path)

    def _schedule(coro):
        return asyncio.run_coroutine_threadsafe(
            coro, correction_runtime.ensure_loop())

    try:
        if path == "/sync/start":
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
                    refusal_envelope(e),
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
        handler._send_json(refusal_envelope(e), status=500)


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
            log_event(
                logger,
                "correction.crossover_v2_refused",
                level=logging.WARNING,
                route=path,
                reason=str(e),
                code=str(getattr(e, "code", "") or ""),
            )
            handler._send_json(
                refusal_envelope(e),
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(
                refusal_envelope(e),
                status=500,
            )
        return

    if path == "/crossover/v2/republish":
        handler._send_json({"ok": False, "code": "route_retired"}, status=HTTPStatus.GONE)
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
            handler._send_client_error(e)
        except ValueError as e:
            handler._send_json(
                refusal_envelope(e),
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(refusal_envelope(e), status=500)
        return

    if path == "/crossover/v2/complete":
        # Same shape as position-ready: a malformed body is a 400
        # (BadRequest subclasses ValueError, so it must be claimed
        # first), while a signal with no wired session waiting is a
        # CONFLICT — a stale caller, not a malformed request.
        try:
            handler._send_json(correction_handlers._handle_crossover_v2_complete(handler))
        except BadRequest as e:
            handler._send_client_error(e)
        except ValueError as e:
            handler._send_json(
                refusal_envelope(e),
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(refusal_envelope(e), status=500)
        return

    if path == "/crossover/v2/retake":
        # The completion signal's shape exactly, and for the same
        # reasons: 400 for a malformed body, 409 for a signal no wired
        # session is waiting for.
        try:
            handler._send_json(correction_handlers._handle_crossover_v2_retake(handler))
        except BadRequest as e:
            handler._send_client_error(e)
        except ValueError as e:
            handler._send_json(
                refusal_envelope(e),
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(refusal_envelope(e), status=500)
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
                refusal_envelope(e),
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(refusal_envelope(e), status=500)
        return


    if path == "/crossover/v2/decline":
        try:
            payload, status = correction_handlers._handle_crossover_v2_decline(handler)
            handler._send_json(payload, status=int(status))
        except ValueError as e:
            handler._send_json(
                refusal_envelope(e),
                status=HTTPStatus.BAD_REQUEST,
            )
        except (OSError, RuntimeError, TypeError) as e:
            logger.exception("%s failed", path)
            handler._send_json(refusal_envelope(e), status=500)
        return

    try:
        if path == "/crossover/recover-volume":
            # The v2 session plan is the only source of an unresolved (or
            # crash-hydrated active) session volume in production; the
            # legacy per-step lease has no path left that ever latches one.
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

            handler._send_json(
                {
                    "status": "refused",
                    "reason": "crossover_volume_recovery_not_required",
                    "next_step": "Refresh the crossover page.",
                },
                status=HTTPStatus.CONFLICT,
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
            refusal_envelope(e),
            status=HTTPStatus.BAD_REQUEST,
        )
    except ValueError as e:
        handler._send_json(
            refusal_envelope(e),
            status=HTTPStatus.BAD_REQUEST,
        )
    except (OSError, RuntimeError, TypeError) as e:
        logger.exception("%s failed", path)
        handler._send_json(refusal_envelope(e), status=500)


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
        handler._send_client_error(exc)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.exception("/measurements/data failed")
        handler._send_json(refusal_envelope(exc), status=500)


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
    ctx = begin_request(handler)
    handler._send_html(sync_flow.render_page(ctx["csrf_token"]))


def _get_sync_status(handler: _Handler) -> None:
    try:
        handler._send_json(sync_flow.handle_status())
    except Exception as e:  # noqa: BLE001
        logger.exception("/sync/status failed")
        handler._send_json(refusal_envelope(e), status=500)


def _follower_delegated(fn: RouteFn) -> RouteFn:
    """A page a bonded follower does not own: it renders the "controlled on
    the leader" page instead of its own. Pair timing is the one left — it is
    a measurement of the paired playback image, so it runs on the leader."""
    @functools.wraps(fn)
    def route(handler: Any) -> None:
        if bonded_follower_active():
            ctx = begin_request(handler)
            handler._send_html(sync_flow.render_follower_page(
                ctx["csrf_token"],
                leader_url=bonded_follower_leader_web_url("/sound/pair/sync/"),
            ))
            return
        fn(handler)
    return route


def _run_post_route(handler: Any, route: RouteFn, path: str) -> None:
    """The seam's guarded-call hook (`run=`). Content DSP is the pair
    leader's on a bonded follower; only /crossover/* stays local. The
    /sync/* and /crossover/* families answer their own failures, so they
    run outside the blanket 500 net."""
    if bonded_follower_active() and not path.startswith("/crossover/"):
        log_event(
            logger,
            "correction.follower_content_dsp_blocked",
            path=path,
        )
        handler._send_json(
            refusal_envelope(code=None, message=(
                "sound measurement is controlled on the pair "
                "leader while this speaker is a follower"
            )),
            status=HTTPStatus.CONFLICT,
        )
        return
    if path.startswith(("/sync/", "/crossover/")):
        route(handler)
        return
    try:
        route(handler)
    except BadRequest as e:
        handler._send_client_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("POST %s failed", path)
        handler._send_json(refusal_envelope(e), status=500)


# do_GET / do_POST dispatch through these exact-path tables
# (path -> callable taking the handler). Mirrors the table in
# jasper/web/wake_corpus_setup.py.

_GET_ROUTES = {
    "/crossover": _get_crossover,
    "/measurements": _get_measurements,
    "/measurements/data": _get_measurements_data,
    "/crossover/status": _get_crossover_status,
    "/crossover/envelope": _get_crossover_envelope,
    "/bass": _get_bass,
    "/bass/status": _get_bass_status,
    "/sync": _follower_delegated(_get_sync),
    "/sync/status": _get_sync_status,
}

# Mutating routes this handler accepts. Membership gates the 404 above;
# deleting a line would otherwise 404 a route silently.
_POST_ROUTES = {
    "/crossover/capture-cancel": _dispatch_crossover,
    "/crossover/reset": _dispatch_crossover,
    "/crossover/recover-volume": _dispatch_crossover,
    # v2 session flow — the only crossover-measurement flow. There is no
    # per-driver flow and no JASPER_CROSSOVER_FLOW selector to branch on.
    "/crossover/v2/session": _dispatch_crossover,
    "/crossover/v2/verify": _dispatch_crossover,
    "/crossover/v2/republish": _dispatch_crossover,
    "/crossover/v2/apply": _dispatch_crossover,
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

    claims = (
        (
            "correction.crossover_repeat_admission_unavailable",
            repeat_admission.claim_owner,
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

    # Crossover applies swap the live graph from this process, so their
    # swap duck needs a canonical target to release to.
    install_env_canonical_target_provider()

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
    )


if __name__ == "__main__":
    raise SystemExit(main())
