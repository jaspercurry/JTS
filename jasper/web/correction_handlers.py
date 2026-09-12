# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement daemon's route bodies.

One ``_handle_*`` function per route the daemon serves, plus the helpers only
they use. Each takes the live ``BaseHTTPRequestHandler`` and returns the JSON
payload (or ``(payload, status)``); the routes table and the handler class
that dispatch them live in :mod:`jasper.web.correction_setup`, and the
capture state they act on lives in :mod:`jasper.web.correction_capture`.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from ..active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from ..platform.systemd import no_hold

from . import correction_capture, correction_runtime
from .correction_capture import (
    CaptureKind,
    _session_lock,
)
from .correction_runtime import BadRequest


def _handle_crossover_capture_cancel() -> dict[str, Any]:
    """Stop Crossover capture work and keep its slot until cleanup completes.

    The Stop button is already hidden once the rendered status turns terminal
    (crossover/main.js's ``CAPTURE_STOPPABLE`` gate), but a poll-cycle race can
    still let a click reach the server after the capture finished on its own
    (it completed, or another tab already stopped it). ``_request_capture_stop``
    raises a diagnostic message for that case; map it to a plain-language
    sentence here rather than leaking it to the page.
    """

    try:
        capture = correction_capture._request_capture_stop("crossover_v2:")
    except ValueError:
        raise ValueError(
            "This measurement already stopped — nothing more to do here."
        ) from None
    return {"capture": capture}


def _handle_crossover_v2_position_ready(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    raw = correction_runtime.read_json_body(handler)
    for key in ("index", "attempt"):
        if key not in raw:
            raise BadRequest(f"{key} is required")
        if isinstance(raw[key], bool) or not isinstance(raw[key], int):
            raise BadRequest(f"{key} must be an integer")
    joined = correction_capture._join_capture(raw["index"], raw["attempt"], raw.get("run_id"))
    if joined is not None:
        return {"ok": True, "capture": joined}
    with _session_lock:
        if raw.get("run_id") is not None and raw["run_id"] != (correction_capture._capture_slot or {}).get("session_id"):
            raise CrossoverV2Refused("The named run is not current", code="capture_slot_busy")
        gate = correction_capture._capture_position_gate
    if gate is None:
        raise CrossoverV2Refused(
            "no remote measurement is waiting for the microphone right now", code="capture_slot_busy",
        )
    released = gate.release(raw["index"], raw["attempt"])
    return {"ok": True, "released": released}


def _handle_crossover_v2_complete(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """Tell the executor to finish the run with the captures already banked."""
    correction_runtime.read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_complete = correction_capture._capture_complete_request
    if request_complete is None:
        raise ValueError(
            "no wired measurement is waiting for an all-spots-measured "
            "confirmation right now"
        )
    request_complete()
    return {"ok": True}


def _handle_crossover_v2_retake(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """No index, ever: the executor owns which capture to re-take (ADR-0296)."""
    correction_runtime.read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_retake = correction_capture._capture_retake_request
    if request_retake is None:
        raise ValueError(
            "no wired measurement is waiting to re-take a spot right now"
        )
    request_retake()
    return {"ok": True}


def _handle_crossover_v2_capture(
    handler: BaseHTTPRequestHandler,
    *,
    verify_only: bool,
    idle_hold: Callable[[str], AbstractContextManager[Any]] = no_hold,
) -> dict[str, Any]:
    """Stage an inline session, or start the existing verification route."""
    raw = correction_runtime.read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    blocking = correction_capture._crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before starting a crossover measurement session"
        )
    status = correction_crossover_backend.status_payload()
    prepared = v2host.prepare_v2_session(
        raw,
        status=status,
        run_async=correction_runtime.run_async,
        camilla_factory=correction_runtime.camilla_controller,
        verify_only=verify_only,
    )
    kind = CaptureKind(
        label=prepared.label,
        open=prepared.open,
        run_and_consume=prepared.run_and_consume,
        request_stop=prepared.request_stop,
        position_gate=prepared.position_gate,
        request_complete=prepared.request_complete,
        request_retake=prepared.request_retake,
        session_id=prepared.session_id,
        join_entry=prepared.join_spec.capture_plan.entries[0] if prepared.join_spec is not None else None,
    )
    start = correction_capture._run_capture if verify_only else correction_capture._stage_capture
    return {"capture": start(kind, idle_hold=idle_hold)}


def _handle_crossover_v2_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /crossover/v2/apply: apply the reviewed v2 measured candidate.

    Reads the same ``status_payload()`` the session preparers do, because the
    apply now runs the stage-2 openability preflight server-side (two-stage
    commission work order D3): a speaker that cannot open its post-apply check
    must not be corrected and left ungraded.
    """
    raw = correction_runtime.read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    return v2host.handle_v2_apply(
        raw,
        correction_runtime.run_async,
        correction_runtime.camilla_controller,
        status=correction_crossover_backend.status_payload(),
    )


def _handle_crossover_v2_decline(
    handler: BaseHTTPRequestHandler,
) -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/v2/decline: the review screen's "Keep current sound".

    Touches no DSP and holds no capture, so unlike its apply/restore siblings it
    needs neither ``run_async`` nor ``camilla_controller`` — it records a decision and
    re-renders. The capture snapshot rides the response for the same reason
    ``/crossover/reset``'s does: the page renders one envelope per round trip.
    """
    raw = correction_runtime.read_json_body(handler)

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_v2_decline(
        raw,
        capture=correction_capture._get_capture_slot_for("crossover_v2:"),
    )


def _handle_crossover_reset() -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/reset: in-flow "start over" for the crossover flow.

    Unlike ``_handle_crossover_capture_cancel``, an unstarted capture is the
    COMMON case here (most Start-over clicks happen between measurements,
    not mid-capture), so a "nothing to stop" ``ValueError`` is swallowed
    rather than surfaced. Any crossover-owned capture or level-match ramp is
    requested to stop first; the actual state clear
    (``correction_crossover_flow.handle_reset``) fails closed if that stop
    has not finished draining yet, rather than racing it.
    """

    try:
        correction_capture._request_capture_stop("crossover_v2:")
    except ValueError:
        pass

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_reset(
        capture=correction_capture._get_capture_slot_for("crossover_v2:"),
    )
