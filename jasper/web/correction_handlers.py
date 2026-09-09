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

from jasper.audio_measurement.calibration import configured_calibration_root

from ..platform.systemd import no_hold

from . import correction_capture, correction_runtime
from .correction_capture import (
    CaptureKind,
    _session_lock,
)
from .correction_runtime import (
    BadRequest,
    MAX_CALIBRATION_UPLOAD_JSON_BYTES,
)


def _handle_test_tone(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /test-tone: play a 5-second 1 kHz sine through the music
    chain so the user can adjust their amp's volume by watching the
    live mic level meter. Pauses renderers + voice loop for the tone
    duration via the same measurement_window the sweep uses.

    Synchronous-feeling from the browser's POV (it returns once the
    tone has finished playing) so the polling state machine doesn't
    have to track a "test tone in progress" sub-state.
    """
    from jasper.audio_measurement.correction_lane import (
        CORRECTION_TONE_DIR,
        correction_play_device,
    )
    from jasper.audio_measurement.playback import ensure_sine_wav, play_wav
    from jasper.measurement_window import measurement_window

    body = correction_runtime.read_json_body(handler)
    duration_s = max(1.0, min(15.0, float(body.get("duration_s", 5.0))))

    async def _run_test_tone() -> None:
        async with measurement_window():
            wav_path = ensure_sine_wav(
                freq_hz=1000.0,
                duration_s=duration_s,
                dbfs=-18.0,
                sample_rate=48000,
                cache_dir=CORRECTION_TONE_DIR,
            )
            # `correction_play_device()` resolves this box's armed-vs-unarmed
            # lane transport per call, so it must not be hoisted to import time.
            await play_wav(
                wav_path,
                alsa_device=correction_play_device(),
                timeout_s=duration_s + 5.0,
            )

    correction_runtime.run_async(_run_test_tone(), timeout=duration_s + 30.0)
    return {"played": True, "duration_s": duration_s}


def _handle_calibration_models(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    return {
        "models": [
            {"key": key, **value}
            for key, value in SUPPORTED_MODELS.items()
        ]
    }


def _handle_calibration_fetch(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import fetch_vendor_calibration

    body = correction_runtime.read_json_body(handler)
    model = str(body.get("model") or "").strip()
    serial = str(body.get("serial") or "").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    record = fetch_vendor_calibration(
        model_key=model,
        serial=serial,
        orientation=orientation,
        root=configured_calibration_root(),
    )
    correction_capture._save_household_mic(record, serial=serial)
    return correction_capture._calibration_payload(record)


def _handle_calibration_upload(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        store_calibration,
    )

    body = correction_runtime.read_json_body(
        handler,
        max_bytes=MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    )
    text = str(body.get("content") or "")
    filename = str(body.get("filename") or "uploaded-calibration.txt")
    model = str(body.get("model") or "other").strip() or "other"
    label = str(body.get("label") or "Other calibrated mic").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    # The page's own control defaults to "response" because that is what a
    # measurement-mic calibration file states (see the upload card's help
    # copy and jasper.audio_measurement.calibration.SUPPORTED_MODELS); a
    # caller that omits the field gets the same answer, not the opposite one.
    sign_convention = (
        str(body.get("sign_convention") or DEFAULT_SIGN_CONVENTION).strip()
        or DEFAULT_SIGN_CONVENTION
    )
    record = store_calibration(
        text=text,
        provider="manual_upload",
        model=model,
        label=label,
        source=f"uploaded:{filename}",
        orientation=orientation,
        sign_convention=sign_convention,
        root=configured_calibration_root(),
    )
    correction_capture._save_household_mic(record)
    return correction_capture._calibration_payload(record)


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
    """Release only the capture attempt whose pose the mover confirmed."""
    raw = correction_runtime.read_json_body(handler)
    for key in ("index", "attempt"):
        if key not in raw:
            raise BadRequest(f"{key} is required")
        if isinstance(raw[key], bool) or not isinstance(raw[key], int):
            raise BadRequest(f"{key} must be an integer")
    with _session_lock:
        gate = correction_capture._capture_position_gate
    if gate is None:
        raise ValueError(
            "no remote measurement is waiting for the microphone right now"
        )
    released = gate.release(raw["index"], raw["attempt"])
    return {"ok": True, "released": released}


def _handle_crossover_v2_complete(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/complete — the wired all-spots-measured signal (D1).

    The wired session's stand-in for the phone's authenticated
    complete-capture-set event (#2662 W2b): the driver (or the W3 wizard
    surface) says the household is done measuring, the held pre-apply group
    closes, and the fit runs. Only a live WIRED session holds the signal — a
    a finished session drops it with the slot — so "nothing waiting" is a conflict
    (stale caller), the position-ready shape.
    """
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
    """POST /crossover/v2/retake — the wired session's per-take retake.

    The local stand-in for the phone's ``begin_capture {retake: true}``: the
    household (or the W3 wizard surface) says the take that just completed
    should be measured again. The walk re-opens THAT slot the next time it is
    waiting on a person — a held begin, or the held-set window — on the
    same terms.

    **No ``index``, and that is the contract rather than a shortcut.** The
    rule is that a retake names the slot which JUST COMPLETED
    (``retakes_the_just_accepted_slot``: ``index == accepted_count``), and the
    walk is the only thing that knows that number — it is a worker-thread
    local, not a published one. Accepting an index here would mint a second
    answer to "which slot", and the only thing a caller could do with it is
    disagree. The signal says WHAT the household wants; WHICH slot stays the
    walk's own fact.

    Only a live session holds the signal, and a finished session drops it
    with the slot, so "nothing waiting" is a conflict (stale caller), the
    position-ready shape. Whether the retake is then ADMISSIBLE (a take exists
    to replace, the plan's attempts are not spent, the slot's extras ledger
    still has room) is the walk's decision, journalled as
    ``event=correction.crossover_v2_wired_retake_refused``: a refused retake
    leaves the household with the take they already had, which is why it is
    never a session death.
    """
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
    """POST /crossover/v2/session | /crossover/v2/verify (Wave 5a).

    Thin dispatch over :mod:`jasper.web.correction_crossover_v2` — the v2 host
    module owns gating, conductor construction, seam bindings, and the plan
    runner; this bridges it into the shared capture slot/lifecycle machinery
    (``_run_capture``) exactly as the other hosted crossover
    captures do.

    ``idle_hold`` covers the one background lifetime a v2 session still owns:
    the capture runner (through ``_run_capture``). It serves no HTTP
    request, and it is the flow the 600 s idle exit actually killed (issue
    #1854). It used to reach a SECOND lifetime — the auto-apply worker thread
    the runner spawned — which the two-stage split removed: the apply is now a
    household POST served in-request, so the tracker's ordinary
    in-flight-request accounting holds the process for it.
    """
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
    )
    return {"capture": correction_capture._run_capture(kind, idle_hold=idle_hold)}


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


def _handle_crossover_v2_republish(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/republish: re-publish a banked candidate by fingerprint.

    Touches no DSP and holds no capture — it replaces the durable session
    document around the published-candidate slot (host-owned apply keys
    carried forward) and moves no graph — so unlike its apply sibling it
    needs neither ``run_async`` nor ``camilla_controller`` nor the stage-2
    ``status_payload()``. The apply door still runs every gate it always did,
    on the next request.
    """
    raw = correction_runtime.read_json_body(handler)

    from . import correction_crossover_v2_republish as republish

    return republish.handle_v2_republish(raw)


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


