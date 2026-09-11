# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the measurement daemon's pages and dispatch.

The measurement pages are HARDWARE/BROWSER-CRITICAL — the real flow
(getUserMedia, the sweep, the CamillaDSP apply) only runs on the Pi. These
tests cover the parts that can be checked off-device: the pages render the
canonical document shell, their behaviour ships as ES modules (no inline
IIFE), the routes still resolve, and the CSRF guard still fires. Network /
CamillaDSP imports are lazy inside the handlers, so a static render needs no
hardware.
"""
from __future__ import annotations

import io
import json
import logging
import threading
from contextlib import nullcontext
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.web import (
    _common,
    correction_capture,
    correction_handlers,
    correction_runtime,
    correction_setup,
)
from tests._log_events import event_fields, event_records
from tests.conftest import bare_root_logger, seat_process_volume_owner
from tests.test_web_wizard_cli import (
    wizard_harness_fixture as _wizard_harness_fixture,
)

_IMPORTED_FIXTURES = (_wizard_harness_fixture,)


@pytest.fixture(autouse=True)
def _saved_passive_layout(tmp_path, monkeypatch):
    """HTTP tests that drive correction apply declare flat-graph authority."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    save_output_topology(_full_range_stereo(), path)


def test_run_async_timeout_cancels_loop_task():
    import asyncio
    import concurrent.futures

    cancelled = threading.Event()

    async def never_finishes():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(concurrent.futures.TimeoutError):
        correction_runtime.run_async(never_finishes(), timeout=0.01)
    assert cancelled.wait(timeout=2)


def _render() -> str:
    return correction_setup._render_page(
        "jts.local",
        csrf_token="tok-correction-123456789012345678901234",
    ).decode("utf-8")


def _drive(path: str, method: str = "GET", *, headers=None, body: bytes = b""):
    """Construct the wizard's Handler without binding a socket and drive a
    single request through it. Returns the raw response bytes."""
    # Same shape make_server builds: a route that spawns background work reads
    # the idle-exit hold off the handler class. nullcontext is the no-tracker
    # default, so driving a handler here behaves exactly as before.
    Handler = correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=nullcontext,
    )

    request_line = f"{method} {path} HTTP/1.1\r\n".encode()
    header_lines = b"Host: jts.local\r\n"
    if body:
        header_lines += f"Content-Length: {len(body)}\r\n".encode()
    for k, v in (headers or {}).items():
        header_lines += f"{k}: {v}\r\n".encode()
    raw = request_line + header_lines + b"\r\n" + body

    rfile = io.BytesIO(raw)
    wfile = io.BytesIO()

    handler = Handler.__new__(Handler)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = rfile.readline()
    handler.parse_request()
    handler.protocol_version = "HTTP/1.1"
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return wfile.getvalue()


def test_get_crossover_subpath_renders_secure_capture_ui():
    resp = _drive("/crossover/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"/assets/correction/js/crossover/main.js" in resp
    assert b'id="crossover-verdict"' in resp
    assert b'id="crossover-steps"' in resp
    assert b'id="crossover-review"' in resp
    assert b'id="crossover-review-body"' in resp
    assert b'id="crossover-action"' in resp
    assert b'id="mic-support"' not in resp


def test_get_measurements_renders_independent_history_page():
    resp = _drive("/measurements/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"/assets/correction/js/measurements.js" in resp
    assert b'id="measurement-run-a"' in resp


def test_get_measurement_data_dispatches_a_and_b(monkeypatch):
    from jasper.web import correction_measurements

    monkeypatch.setattr(
        correction_measurements,
        "build_data",
        lambda **kwargs: {
            "a": kwargs["run_a_id"],
            "b": kwargs["run_b_id"],
        },
    )
    resp = _drive("/measurements/data?a=aaa&b=bbb")

    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert json.loads(resp.split(b"\r\n\r\n", 1)[1]) == {
        "a": "aaa", "b": "bbb",
    }


def test_follower_keeps_local_crossover_measurement_post(monkeypatch):
    monkeypatch.setattr(correction_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(_common, "guard_mutating_request", lambda _handler: True)
    monkeypatch.setattr(
        correction_handlers,
        "_handle_crossover_reset",
        lambda: ({"route": "crossover-reset"}, HTTPStatus.OK),
    )

    resp = _drive("/crossover/reset", "POST", body=b"{}")

    assert b" 200 " in resp.split(b"\r\n", 1)[0]
    assert json.loads(resp.split(b"\r\n\r\n", 1)[1]) == {
        "route": "crossover-reset"
    }


def test_get_bass_subpath_renders_display_page():
    resp = _drive("/bass/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"Bass management" in resp  # P5: read-only display, not a placeholder
    assert b"/assets/correction/js/bass/main.js" in resp


def test_get_bass_status_returns_display_json():
    import json

    resp = _drive("/bass/status")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
    assert "configured" in body and "corner_hz" in body


def test_crossover_status_contains_unexpected_failures(monkeypatch):
    from jasper.web import correction_crossover_flow

    def fail(**_kwargs):
        raise LookupError("surprise")

    monkeypatch.setattr(correction_crossover_flow, "handle_status", fail)
    monkeypatch.setattr(
        correction_capture,
        "_enforce_session_volume_ceiling",
        lambda _: None,
    )

    resp = _drive("/crossover/status")

    assert b"500" in resp.split(b"\r\n", 1)[0]
    assert json.loads(resp.split(b"\r\n\r\n", 1)[1]) == {"error": "surprise"}


def test_unknown_get_route_404():
    resp = _drive("/nope")
    assert b"404" in resp.split(b"\r\n", 1)[0]


def test_post_without_csrf_is_rejected():
    """Every state-changing POST must fail CSRF before doing any work — the
    resilience guard must survive the restyle."""
    resp = _drive("/crossover/reset", method="POST", body=b"{}")
    assert b"403" in resp.split(b"\r\n", 1)[0]


def test_unknown_post_route_404_before_csrf():
    """Unknown POST paths 404 without revealing CSRF state (route-check
    precedes the CSRF check)."""
    resp = _drive("/bogus", method="POST", body=b"{}")
    assert b"404" in resp.split(b"\r\n", 1)[0]


def test_known_post_routes_reach_csrf_guard():
    """Lock the full POST surface so the migration can't silently drop a
    route: each known route reaches the CSRF guard (403 without a token),
    proving it is still registered."""
    known = {
        "/sync/start", "/sync/play", "/sync/analyze",
        "/sync/apply", "/sync/stop", "/sync/reset",
        "/crossover/capture-cancel",
        "/crossover/reset", "/crossover/recover-volume",
        # v2 conductor flow (Wave 5a) — the only crossover-measurement flow
        # since W5b retired the legacy per-driver flow and the
        # JASPER_CROSSOVER_FLOW selector.
        "/crossover/v2/session", "/crossover/v2/verify", "/crossover/v2/apply",
        # Make a banked candidate live again so the apply route can reach it.
        # It writes durable state, so it is CSRF-guarded like every other
        # mutating route even though it touches no DSP.
        "/crossover/v2/republish",
        # The review screen's "Keep current sound" (#2641) — a decision the
        # household takes, so CSRF-guarded like every other mutating route
        # even though it changes nothing on the speaker.
        "/crossover/v2/decline",
        # A gated session's position release — an external driver's POST, or a
        # person's on a hand-walked wired round, and CSRF-guarded exactly like
        # every other mutating route here.
        "/crossover/v2/position-ready",
        # The wired session's all-spots-measured confirmation (#2662 W2b) —
        # same driver-facing shape as position-ready, same CSRF guard.
        "/crossover/v2/complete",
        # The wired session's per-take retake (#2879) — same shape again.
        "/crossover/v2/retake",
    }
    assert known == set(correction_setup._POST_ROUTES)
    for route in sorted(known):
        resp = _drive(route, method="POST", body=b"{}")
        assert b"403" in resp.split(b"\r\n", 1)[0], (
            f"{route} should reach the CSRF guard (403)"
        )

    # Driver evidence requires the capture's signal-bounded quiet crop + repeat
    # state machine. The former raw-WAV single-shot route had no product caller
    # and is deliberately absent rather than implicitly accepting null SNR.
    response = _drive("/crossover/driver-capture", method="POST", body=b"wav")
    assert b"404" in response.split(b"\r\n", 1)[0]


def test_crossover_v2_refusal_is_logged_not_silent(monkeypatch, caplog):
    """W6 finding: a refused v2 session/verify start (CrossoverV2Refused or any
    other precondition ValueError) mapped straight to a 400 with NO journal
    signal — the failed session-start was invisible in journalctl. The 400
    response is correct for the browser; the gap was purely observability.

    WHICH gate this bare box hits first is not the subject and is free to move.
    The subject is that whichever gate refuses, the refusal is journaled, never
    silent, and it carries the code the household's screen renders from."""
    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    caplog.set_level(logging.WARNING, logger=correction_capture.logger.name)

    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"400" in resp.split(b"\r\n", 1)[0]
    fields = event_fields(caplog, "correction.crossover_v2_refused")
    assert fields["route"] == "/crossover/v2/session"
    assert "code" in fields
    assert " " in fields["reason"]


def test_flow_error_reaching_the_500_arm_is_copy_not_a_programmer_string(
    monkeypatch,
):
    """#1833 leak 1. ``CrossoverV2FlowError`` subclasses ``RuntimeError``, not
    ``ValueError`` — so one raised SYNCHRONOUSLY inside ``prepare_v2_session``'s
    ``_open`` (the spec / index-phase-map builders, both of which validate) skips
    the 400 arm entirely and lands in this 500 arm, which echoed ``str(e)``
    straight into the wizard's DOM.

    ``test_whole_program_family_is_mapped_at_the_wizard_boundary`` asserts the
    mapper handles this exception class, which reads like end-to-end containment
    and is not: nothing on this route called the mapper.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_PROGRAM_UNPLAYABLE,
        REASON_REGISTRY,
    )
    from jasper.active_speaker.crossover_v2_flow import CrossoverV2FlowError

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    raw_text = "cloud_measure_positions must be 6..12, got 14"

    def _raise_flow_error(*_a, **_k):
        raise CrossoverV2FlowError(raw_text)

    monkeypatch.setattr(
        correction_handlers, "_handle_crossover_v2_capture", _raise_flow_error
    )
    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"500" in resp.split(b"\r\n", 1)[0]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    assert body["error"] == REASON_REGISTRY[REASON_PROGRAM_UNPLAYABLE].message
    assert "cloud_measure_positions" not in body["error"]
    assert raw_text not in body["error"]


def test_the_500_arm_still_reports_unmapped_failures_verbatim(monkeypatch):
    """Scope guard for the fix above: the mapper is the identity outside the
    families it knows, so a plain transport failure must keep saying what it
    said. Containing every 500 behind one sentence would hide real breakage."""
    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )

    def _raise_oserror(*_a, **_k):
        raise OSError("capture socket vanished")

    monkeypatch.setattr(
        correction_handlers, "_handle_crossover_v2_capture", _raise_oserror
    )
    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"500" in resp.split(b"\r\n", 1)[0]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    assert body["error"] == "capture socket vanished"


def test_coded_refusal_carries_its_resolution_action_in_the_400_body(
    monkeypatch, caplog,
):
    """Issues #1820/#1821 review, S1. The session-open pre-flight is now the
    PRIMARY path for a profile-not-confirmed refusal — and a pre-flight refusal
    can never reach the envelope's hard-stop screen, because that screen renders
    from a PERSISTED failure and the pre-flight deliberately refuses before any
    state is written. So the reason's own ``next_action`` has to ride the 400
    body, or the household reads the exact remedy as flat text and has to go
    find the control themselves.

    An UNCODED refusal must still answer with a bare message: most refusals'
    only honest answer is prose, and inventing a button for them would be worse
    than none.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
        REASON_REGISTRY,
    )
    from jasper.web import correction_crossover_v2 as v2host_mod

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    caplog.set_level(logging.WARNING, logger=correction_capture.logger.name)

    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]

    def _refuse_coded(*_a, **_k):
        raise v2host_mod.CrossoverV2Refused(
            spec.message, code=REASON_PROGRAM_PROFILE_NOT_CONFIRMED
        )

    monkeypatch.setattr(
        correction_handlers, "_handle_crossover_v2_capture", _refuse_coded
    )
    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"400" in resp.split(b"\r\n", 1)[0]
    body = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    assert body["error"] == spec.message
    # Same registry entry the hard-stop screen would have rendered.
    assert body["next_action"] == dict(spec.next_action)
    assert body["next_action"]["href"] == "/sound/speaker/#confirm-safety-limits"
    # And the code is on the journal line beside the reason.
    assert (
        event_fields(caplog, "correction.crossover_v2_refused")["code"]
        == REASON_PROGRAM_PROFILE_NOT_CONFIRMED
    )

    def _refuse_uncoded(*_a, **_k):
        raise v2host_mod.CrossoverV2Refused(
            "the woofer and tweeter measurement targets are not both active"
        )

    monkeypatch.setattr(
        correction_handlers, "_handle_crossover_v2_capture", _refuse_uncoded
    )
    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")
    plain = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    assert "next_action" not in plain


def test_a_start_time_refusal_is_a_clean_400_not_a_500(monkeypatch, caplog):
    """A ``ValueError`` raised inside ``register_session`` before a session
    exists — the position every precondition refusal occupies — pins the
    shape the dispatcher answers with: a clean 400 carrying the operator's
    remedy, not a 500 with a traceback.
    """
    def _refuse(*_a, **_k):
        raise ValueError(
            "the measurement mic is not ready — reconnect it and try again."
        )

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    monkeypatch.setattr(correction_handlers, "_handle_crossover_v2_capture", _refuse)
    caplog.set_level(logging.WARNING, logger=correction_capture.logger.name)

    resp = _drive("/crossover/v2/session", method="POST", body=b"{}")

    assert b"400" in resp.split(b"\r\n", 1)[0]
    assert b"reconnect it and try again" in resp
    # And it is on the journal like every other refused start.
    assert event_records(caplog, "correction.crossover_v2_refused")


def test_apply_blocked_status_maps_to_409_with_named_issue(monkeypatch):
    """Finding N (a): a blocked apply must not read as success. Before this
    fix, /crossover/v2/apply always answered 200 regardless of payload
    contents — a household's browser had no signal that tapping Apply
    silently did nothing (run6-apply-blocked.log: 200 OK on every attempt)."""
    from jasper.web import correction_crossover_v2 as v2host_mod

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    monkeypatch.setattr(
        v2host_mod,
        "handle_v2_apply",
        # ``status`` is keyword-only and REQUIRED since PR-T3 (the apply runs
        # the stage-2 openability preflight before committing), so a stub that
        # does not accept it would no longer stand in for the real handler.
        lambda raw, run_async, camilla_factory, *, status: {
            "status": "blocked",
            "profile": {"status": "blocked"},
            "apply": None,
            "issues": [{
                "severity": "blocker",
                "code": "measured_candidate_preset_mismatch",
                "message": (
                    "the reviewed measured candidate no longer equals the "
                    "saved crossover"
                ),
            }],
            "issue": {
                "id": "measured_candidate_preset_mismatch",
                "message": (
                    "the reviewed measured candidate no longer equals the "
                    "saved crossover"
                ),
            },
        },
    )

    resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")

    assert b"409" in resp.split(b"\r\n", 1)[0]
    body = resp.split(b"\r\n\r\n", 1)[1]
    assert b"measured_candidate_preset_mismatch" in body


def test_apply_applied_status_still_maps_to_200(monkeypatch):
    """The 409 mapping is status-content-driven, not blanket — a successful
    apply must still read 200."""
    from jasper.web import correction_crossover_v2 as v2host_mod

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    monkeypatch.setattr(
        v2host_mod,
        "handle_v2_apply",
        lambda raw, run_async, camilla_factory, *, status: {
            "status": "applied", "profile": {},
        },
    )

    resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")

    assert b"200" in resp.split(b"\r\n", 1)[0]


def test_an_apply_400_is_always_recorded_fault_as_error_refusal_as_warning(
    monkeypatch, caplog,
):
    """#2839 gate round: the apply 400 arm answered with a raw string and
    journaled nothing.

    ``test_crossover_v2_refusal_is_logged_not_silent`` had already ruled that
    shape a defect on the session/verify arm — "the 400 response is correct for
    the browser; the gap was purely observability" — so NOTHING leaving this
    arm is silent either. What the two halves differ in is severity, because
    they are different events:

    * a ``CrossoverV2Refused`` or a ``BadRequest`` is the caller being told no,
      and is journaled at WARNING under ``correction.crossover_v2_refused`` —
      the sibling's own vocabulary and field set, and the sibling exempts a
      malformed body no more than this does;
    * anything else is the speaker faulting on its own apply path, which
      ``save_v2_state``'s ``allow_nan=False`` refusal made reachable, and is
      journaled at ERROR under ``correction.crossover_v2_apply_fault``.

    Both directions asserted, and each asserted NOT to carry the other's event:
    one arm emitting both names would make the severity split meaningless.
    """
    from jasper.web import correction_crossover_v2 as v2host_mod

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )

    def _raise(exc):
        def _handler(raw, run_async, camilla_factory, *, status):
            raise exc
        return _handler

    def _levels(event: str) -> list[str]:
        """Levels of THIS arm's records for ``event``.

        Filtered by event name rather than read off ``caplog.records`` whole:
        an unrelated backend probe logs its own ERROR during the drive, so a
        bare level list would assert something other than what it reads.
        """
        return [r.levelname for r in event_records(caplog, event)]

    # The fault half: a bare ValueError, exactly what json's non-finite
    # refusal is.
    monkeypatch.setattr(v2host_mod, "handle_v2_apply", _raise(
        ValueError("Out of range float values are not JSON compliant: nan")
    ))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=correction_capture.logger.name):
        resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")
    assert b"400" in resp.split(b"\r\n", 1)[0]
    fault_fields = event_fields(caplog, "correction.crossover_v2_apply_fault")
    assert "not JSON compliant" in fault_fields["error"]
    assert not event_records(caplog, "correction.crossover_v2_refused")
    assert _levels("correction.crossover_v2_apply_fault") == ["ERROR"]

    # The refusal half: same 400, recorded, one level down and under the
    # vocabulary that already answers "a v2 route refused".
    caplog.clear()
    monkeypatch.setattr(v2host_mod, "handle_v2_apply", _raise(
        v2host_mod.CrossoverV2Refused("nothing to apply")
    ))
    with caplog.at_level(logging.WARNING, logger=correction_capture.logger.name):
        resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")
    assert b"400" in resp.split(b"\r\n", 1)[0]
    refused_fields = event_fields(caplog, "correction.crossover_v2_refused")
    assert refused_fields["route"] == "/crossover/v2/apply"
    assert "nothing to apply" in refused_fields["reason"]
    assert not event_records(caplog, "correction.crossover_v2_apply_fault")
    assert _levels("correction.crossover_v2_refused") == ["WARNING"]

    # A malformed body takes the refusal arm too, because the sibling this
    # borrows from exempts it no more than a typed refusal.
    caplog.clear()
    monkeypatch.setattr(v2host_mod, "handle_v2_apply", _raise(
        correction_runtime.BadRequest("apply body must be an object")
    ))
    with caplog.at_level(logging.WARNING, logger=correction_capture.logger.name):
        resp = _drive("/crossover/v2/apply", method="POST", body=b"{}")
    assert b"400" in resp.split(b"\r\n", 1)[0]
    assert event_records(caplog, "correction.crossover_v2_refused")
    assert _levels("correction.crossover_v2_refused") == ["WARNING"]


# ---------------------------------------------------------------------------
# Public surface unchanged.
# ---------------------------------------------------------------------------


def test_make_server_smoke():
    srv = correction_setup.make_server(("127.0.0.1", 0), hostname="jts.local")
    try:
        assert srv is not None
    finally:
        srv.server_close()


def test_public_surface_present():
    assert callable(correction_setup.make_server)
    assert callable(correction_setup.main)
    assert callable(correction_setup._make_handler_class)


def test_service_start_claims_all_crossover_state_owners(monkeypatch):
    from jasper.active_speaker import repeat_admission, web_commissioning

    claims = []
    monkeypatch.setattr(
        repeat_admission, "claim_owner", lambda: claims.append("repeat")
    )

    # The abandoned-sequence convergence hook: a capture sequence the previous
    # process left on the all-muted staged anchor must be offered its
    # production restore at this same single-owner lifecycle boundary.
    async def restore_capture_entry(*, camilla_factory):
        del camilla_factory
        claims.append("capture_entry")
        return {"status": "idle"}

    monkeypatch.setattr(
        web_commissioning,
        "restore_pending_capture_entry_config",
        restore_capture_entry,
    )
    async def recover_program():
        claims.append("program")

    monkeypatch.setattr(
        correction_setup, "_restore_protected_neutral_program_graph", recover_program,
    )
    correction_setup._claim_crossover_state_owners()

    assert claims == ["repeat", "capture_entry", "program"]


def test_program_graph_startup_recovery_is_exact_and_fail_closed(
    monkeypatch, tmp_path, caplog,
):
    """A fresh process converges a REAL abandoned program graph to its anchor.

    No monkeypatched classifier: the graph fed in is what
    ``emit_active_speaker_program_config`` actually emits, so the test proves
    the shipped classifier recognises the shipped emitter rather than proving a
    lambda returns True. Three shapes, all three consumed:

    * the exact emitted graph (origin True) restores and logs `_recovered`;
    * the same graph with one filter MUTATED (origin False — our namespace,
      wrong shape) also restores, because a mutated commissioning graph is
      still this path's mess and the persisted config is the SSOT, and logs
      the DISTINCT `_mutated_recovered` event so the drift stays visible;
    * an unrelated graph (origin None) is left alone.

    Fail-closed is pinned last: a load that does not take raises rather than
    letting the service accept requests over measurement wiring.
    """
    import asyncio
    from contextlib import asynccontextmanager
    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_program_config, protected_neutral_program_origin,
    )
    from jasper import dsp_apply
    from tests.test_active_speaker_program_config import (
        ACTIVE_PCM, ROLE_CHANNELS, _confirmed_protection, _preset,
    )

    program_yaml = emit_active_speaker_program_config(
        _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    )
    mutated = program_yaml.replace("gain: 0.0", "gain: -3.0", 1)
    # The fixture is only honest if the real classifier really sees the two
    # states this function is being asked to tell apart.
    assert protected_neutral_program_origin(program_yaml) is True
    assert protected_neutral_program_origin(mutated) is False
    assert protected_neutral_program_origin("devices: {}\n") is None

    anchor = tmp_path / "production.yml"
    anchor.write_text("devices: {}\n", encoding="utf-8")

    class Cam:
        loaded = True

        def __init__(self, active):
            self.active, self.calls = active, []

        async def get_active_config_raw(self, *, best_effort):
            self.calls.append("raw")
            return self.active

        async def get_config_file_path(self, *, best_effort):
            self.calls.append("path")
            return str(anchor)

        async def set_active_config_raw(self, text, *, best_effort):
            self.calls.append("set")
            if self.loaded:
                self.active = text
            return self.loaded

        async def normalize_config_raw(self, text, *, best_effort):
            return text

    @asynccontextmanager
    async def lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", lock)

    for active, event in (
        (program_yaml, "correction.crossover_v2_program_recovered"),
        (mutated, "correction.crossover_v2_program_mutated_recovered"),
    ):
        cam = Cam(active)
        monkeypatch.setattr(correction_runtime, "camilla_controller", lambda cam=cam: cam)
        with caplog.at_level(logging.INFO):
            caplog.clear()
            asyncio.run(correction_setup._restore_protected_neutral_program_graph())
        # The speaker is left running the persisted anchor's EXACT content.
        assert cam.active == anchor.read_text(encoding="utf-8")
        assert event_records(caplog, event), [
            r.getMessage() for r in caplog.records
        ]

    unrelated = Cam("devices: {}\n")
    monkeypatch.setattr(correction_runtime, "camilla_controller", lambda: unrelated)
    asyncio.run(correction_setup._restore_protected_neutral_program_graph())
    assert unrelated.calls == ["raw"]

    stuck = Cam(program_yaml)
    stuck.loaded = False
    monkeypatch.setattr(correction_runtime, "camilla_controller", lambda: stuck)
    with pytest.raises(RuntimeError, match="was not confirmed"):
        asyncio.run(correction_setup._restore_protected_neutral_program_graph())


def test_idle_shutdown_invokes_capture_entry_restore(monkeypatch):
    """The idle exit converges an abandoned capture sequence to production.

    The common abandon is the user closing the tab mid-sequence:
    correction-web idles out minutes later, and (being socket-activated) will
    not run again until someone revisits a measurement page. Without this hook the
    speaker would stay parked on the all-muted staged anchor until then.
    """

    from jasper.active_speaker import web_commissioning

    calls = []

    async def restore(*, camilla_factory):
        del camilla_factory
        calls.append("restore")
        return {"status": "restored", "config_path": "/tmp/prod.yml"}

    monkeypatch.setattr(
        web_commissioning, "restore_pending_capture_entry_config", restore
    )

    correction_setup._idle_exit_restore_capture_entry()
    assert calls == ["restore"]

    # A failing restore is swallowed (the process is about to exit; the
    # durable stash survives for the service-start claim boundary).
    async def broken(*, camilla_factory):
        del camilla_factory
        calls.append("broken")
        raise RuntimeError("camilla went away")

    monkeypatch.setattr(
        web_commissioning, "restore_pending_capture_entry_config", broken
    )
    correction_setup._idle_exit_restore_capture_entry()
    assert calls == ["restore", "broken"]


def test_main_configures_root_logging_at_info(wizard_harness):
    """``event=dsp.baseline_base_trim_banked`` (and every other INFO event this
    process logs) needs a root handler at INFO, or Python's ``lastResort``
    floors at WARNING and drops it silently — a trim could replace another
    with nothing anywhere saying so. ``tests/test_cli_driver_trim.py`` pinned
    this same dependency for the now-deleted ``jasper-driver-trim`` verb
    (#3388); this process is the only one left that reaches the apply seam,
    so it is the one that must configure it now.

    Unredacted by design: this file is a listed entry in
    ``tests/test_logging_setup.py``'s ``_ALLOWLIST``, which is why it hands
    the shared runner its own ``configure``.
    """

    wizard_harness(correction_setup, [])
    with bare_root_logger() as root:
        assert correction_setup.main(["--host", "127.0.0.1", "--port", "0"]) == 0
        assert root.handlers, "main() must configure a root handler (basicConfig)"
        assert root.getEffectiveLevel() <= logging.INFO


def test_failed_owner_claim_does_not_skip_later_claims(monkeypatch):
    from unittest.mock import AsyncMock
    from jasper.active_speaker import repeat_admission

    claims = []

    def fail_repeat():
        raise OSError("repeat state unavailable")

    monkeypatch.setattr(repeat_admission, "claim_owner", fail_repeat)
    monkeypatch.setattr(
        correction_setup,
        "_restore_capture_entry",
        lambda: claims.append("capture_entry"),
    )
    monkeypatch.setattr(correction_setup, "_restore_protected_neutral_program_graph", AsyncMock())

    correction_setup._claim_crossover_state_owners()

    assert claims == ["capture_entry"]


# ---------------------------------------------------------------------------
# P4 auto-revert wiring (the verify-upload handler → session.auto_revert).
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for the auto-revert helper: it exposes just the
    verdict accessor and an async auto_revert that records the target."""

    def __init__(self, verdict: str | None, config_dir: Path) -> None:
        self._verdict = verdict
        self.cfg = SimpleNamespace(config_dir=config_dir)
        self.revert_calls: list[str | None] = []

    @property
    def acceptance_verdict(self) -> str | None:
        return self._verdict

    async def auto_revert(self, camilla_set_config, *, target_config_path=None):
        self.revert_calls.append(target_config_path)
        # A real revert flips to IDLE; the fake just reports it acted.
        return True


def _patch_no_op_camilla(monkeypatch) -> None:
    class _FakeCam:
        async def set_config_file_path(self, path, *, best_effort=False):
            return True

        async def get_config_file_path(self, *, best_effort=False):
            return "/etc/camilladsp/outputd-cutover.yml"

    monkeypatch.setattr(correction_runtime, "camilla_controller", lambda: _FakeCam())
    # Resolve target without touching the topology-aware carrier.
    async def resolve(_sess, _cam):
        return Path("/etc/camilladsp/no-room.yml")

    monkeypatch.setattr(correction_handlers, "_resolve_reset_target_async", resolve)


class _RecordingCam:
    def __init__(self) -> None:
        self.loads: list[str] = []

    async def set_config_file_path(self, path, *, best_effort=False):
        self.loads.append(str(path))
        return True

    async def get_config_file_path(self, *, best_effort=False):
        return "/etc/camilladsp/outputd-cutover.yml"


class _CleanSessionVolumePlan:
    """A benign session-volume plan for GET-envelope drives (no drain, no
    recovery) so the lazy-ceiling read + status block stay no-ops."""

    needs_recovery = False

    def stale_active(self, now=None) -> bool:
        return False


def test_crossover_envelope_surfaces_the_v2_capture_slot(monkeypatch):
    """Finding D: /crossover/envelope's capture lookup must match crossover_v2:*
    — it filtered only crossover_sweep:/level_ramp:crossover, so ``capture`` came
    back null during a live v2 session and a page reload lost the session's
    status (and the failure copy never reached the household)."""
    import json

    from jasper.web import correction_crossover_v2 as v2host

    v2host.set_volume_plan_for_tests(_CleanSessionVolumePlan())
    correction_capture._set_capture_slot({
        "status": "waiting",
        "kind": v2host.V2_CAPTURE_KIND_SESSION,
    })
    try:
        resp = _drive("/crossover/envelope")
        assert b"200" in resp.split(b"\r\n", 1)[0]
        body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
        assert body["capture"] is not None
        assert body["capture"]["status"] == "waiting"
        assert body["capture"]["kind"] == "crossover_v2:session"
    finally:
        correction_capture._set_capture_slot(None)
        v2host.set_volume_plan_for_tests(None)


def test_recover_volume_routes_to_the_v2_plan(monkeypatch):
    """Finding E2: when the v2 conductor owns the unresolved session volume, the
    recover-volume endpoint must drive SessionVolumePlan.recover_unresolved — the
    legacy-lease path 409'd crossover_volume_recovery_not_required (the volume
    holds no lease-unresolved state), leaving the recovery button dead."""
    import json

    from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        _common, "guard_mutating_request", lambda handler: True
    )
    drained: list = []

    class _V2Plan:
        needs_recovery = True

        async def recover_unresolved(self, door):
            await door.restore_household_level_db(-15.0)
            await door.read_household_level_db()
            drained.append(True)
            return SessionVolumeRestoreResult.EXACT_RESTORED

    v2host.set_volume_plan_for_tests(_V2Plan())

    class _Cam:
        async def set_volume_db(self, db, best_effort=False):
            return True

        async def get_volume_db(self, best_effort=False):
            return -15.0

    monkeypatch.setattr(correction_runtime, "camilla_controller", lambda: _Cam())
    # Production installs a fader owner before serving, and the v2 drains now
    # refuse without one rather than falling back to a second authority.
    _owned = _Cam()
    seat_process_volume_owner(
        monkeypatch,
        lambda db: _owned.set_volume_db(db, best_effort=True),
        lambda: _owned.get_volume_db(best_effort=True),
    )
    try:
        resp = _drive("/crossover/recover-volume", method="POST", body=b"{}")
        assert b"200" in resp.split(b"\r\n", 1)[0]
        body = json.loads(resp.split(b"\r\n\r\n", 1)[1])
        assert body["status"] == "recovered"
        assert body["recovery"] == "exact_restored"
        assert drained == [True]
    finally:
        v2host.set_volume_plan_for_tests(None)


def test_crossover_reset_and_recover_volume_ignore_a_legacy_volume_safety_file(
    monkeypatch, tmp_path,
) -> None:
    """CrossoverLevelLease's read side used to hydrate
    active_speaker_crossover_volume_safety.json as an unresolved latch even
    after its writer was deleted, so a box that had not re-run install (the
    file's retirement is a deploy/lib/install/retirements.sh row) got
    /crossover/reset refused forever. The lease no longer reads any such
    file at all -- writing one here only reproduces the on-disk scenario,
    neither route below consults it -- and /crossover/recover-volume stays
    decided by the v2 session-volume plan alone."""
    import json

    from jasper.web import correction_crossover_v2 as v2host

    (tmp_path / "active_speaker_crossover_volume_safety.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "jts_crossover_volume_safety",
            "status": "active",
            "reason": None,
            "source": "driver_sweep",
            "speaker_group_id": "mono",
            "role": "woofer",
            "original_main_volume_db": -27.0,
            "emergency_volume_db": -60.0,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(_common, "guard_mutating_request", lambda handler: True)
    monkeypatch.setattr(
        correction_handlers,
        "_handle_crossover_reset",
        lambda: ({"status": "cleared"}, HTTPStatus.OK),
    )

    reset_resp = _drive("/crossover/reset", "POST", body=b"{}")

    assert b"200" in reset_resp.split(b"\r\n", 1)[0]
    assert json.loads(reset_resp.split(b"\r\n\r\n", 1)[1])["status"] == "cleared"

    v2host.set_volume_plan_for_tests(_CleanSessionVolumePlan())
    try:
        recover_resp = _drive(
            "/crossover/recover-volume", method="POST", body=b"{}"
        )
    finally:
        v2host.set_volume_plan_for_tests(None)

    assert b"409" in recover_resp.split(b"\r\n", 1)[0]
    recover_body = json.loads(recover_resp.split(b"\r\n\r\n", 1)[1])
    assert recover_body["status"] == "refused"
    assert recover_body["reason"] == "crossover_volume_recovery_not_required"
